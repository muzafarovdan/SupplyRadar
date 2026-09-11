"""SQLite-хранилище: история запусков, карточки поставщиков и подтверждения.

Хранилище нужно для воспроизводимости: результат запуска можно открыть повторно
и проверить извлечение без обращения к сети.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.models import SearchRunResult

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_runs (
    id                TEXT PRIMARY KEY,
    request_json      TEXT    NOT NULL,
    started_at        TEXT    NOT NULL,
    duration_seconds  REAL    NOT NULL,
    urls_found        INTEGER NOT NULL,
    pages_processed   INTEGER NOT NULL,
    pages_failed      INTEGER NOT NULL,
    suppliers_found   INTEGER NOT NULL,
    candidates_found  INTEGER NOT NULL DEFAULT 0,
    web_hits_found    INTEGER NOT NULL DEFAULT 0,
    provider_stats_json TEXT NOT NULL DEFAULT '{}',
    extraction_model  TEXT,
    extraction_schema_version INTEGER,
    status            TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    run_id         TEXT    NOT NULL,
    supplier_id    TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    domain         TEXT,
    score          REAL    NOT NULL,
    excluded       INTEGER NOT NULL,
    result_group   TEXT    NOT NULL DEFAULT 'supplier',
    checked_at     TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    payload        TEXT    NOT NULL,
    PRIMARY KEY (run_id, supplier_id),
    FOREIGN KEY (run_id) REFERENCES search_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidence (
    run_id      TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    field_name  TEXT NOT NULL,
    value       TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    quote       TEXT NOT NULL,
    confidence  REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES search_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    url             TEXT NOT NULL,
    domain          TEXT NOT NULL,
    status          TEXT NOT NULL,
    http_status     INTEGER,
    note            TEXT,
    supplier_name   TEXT,
    discovery_query TEXT,
    provider        TEXT,
    source_kind     TEXT NOT NULL DEFAULT 'page',
    FOREIGN KEY (run_id) REFERENCES search_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS page_cache (
    url          TEXT PRIMARY KEY,
    final_url    TEXT NOT NULL,
    http_status  INTEGER,
    mime_type    TEXT,
    fetched_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    content_hash TEXT,
    html         TEXT,
    cleaned_text TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_suppliers_domain ON suppliers(domain);
CREATE INDEX IF NOT EXISTS idx_evidence_supplier ON evidence(run_id, supplier_id);
CREATE INDEX IF NOT EXISTS idx_sources_run ON sources(run_id);
CREATE INDEX IF NOT EXISTS idx_sources_url ON sources(url);
"""


class Storage:
    """Доступ к SQLite. Схема создаётся при первом обращении."""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._ensure_column(
                connection,
                "suppliers",
                "result_group",
                "TEXT NOT NULL DEFAULT 'supplier'",
            )
            self._ensure_column(
                connection, "sources", "source_kind", "TEXT NOT NULL DEFAULT 'page'"
            )
            self._ensure_column(
                connection,
                "search_runs",
                "candidates_found",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "search_runs",
                "web_hits_found",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "search_runs",
                "provider_stats_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(connection, "search_runs", "extraction_model", "TEXT")
            self._ensure_column(
                connection,
                "search_runs",
                "extraction_schema_version",
                "INTEGER",
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def save_run(self, result: SearchRunResult, status: str = "completed") -> None:
        """Сохраняет запуск целиком: параметры, поставщиков и подтверждения."""
        stats = result.stats
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO search_runs
                    (id, request_json, started_at, duration_seconds, urls_found,
                     pages_processed, pages_failed, suppliers_found,
                     candidates_found, web_hits_found, provider_stats_json,
                     extraction_model, extraction_schema_version, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stats.run_id,
                    result.request.model_dump_json(),
                    stats.started_at.isoformat(),
                    stats.duration_seconds,
                    stats.urls_found,
                    stats.pages_processed,
                    stats.pages_failed,
                    stats.suppliers_found,
                    stats.candidates_found,
                    stats.web_hits_found,
                    json.dumps(stats.provider_stats, ensure_ascii=False),
                    stats.extraction_model,
                    stats.extraction_schema_version,
                    status,
                ),
            )
            groups = (
                [(item, "supplier") for item in result.suppliers]
                + [(item, "candidate") for item in result.candidates]
                + [(item, "excluded") for item in result.excluded]
            )
            for scored, result_group in groups:
                supplier = scored.supplier
                connection.execute(
                    """
                    INSERT OR REPLACE INTO suppliers
                        (run_id, supplier_id, name, domain, score, excluded,
                         result_group, checked_at, schema_version, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stats.run_id,
                        supplier.id,
                        supplier.name,
                        supplier.domain,
                        scored.score,
                        int(scored.excluded),
                        result_group,
                        supplier.checked_at.isoformat(),
                        SCHEMA_VERSION,
                        scored.model_dump_json(),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO evidence
                        (run_id, supplier_id, field_name, value, source_url,
                         quote, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            stats.run_id,
                            supplier.id,
                            item.field_name,
                            item.value,
                            item.source_url,
                            item.quote,
                            item.confidence,
                        )
                        for item in supplier.evidence
                    ],
                )

            connection.executemany(
                """
                INSERT INTO sources
                    (run_id, url, domain, status, http_status, note,
                     supplier_name, discovery_query, provider, source_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        stats.run_id,
                        source.url,
                        source.domain,
                        source.status.value,
                        source.http_status,
                        source.note,
                        source.supplier_name,
                        source.discovery_query,
                        source.provider,
                        source.source_kind.value,
                    )
                    for source in result.sources
                ],
            )

    def list_runs(self, limit: int = 20) -> list[dict]:
        """Последние запуски для истории и отладки."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, request_json, started_at, duration_seconds,
                       suppliers_found, candidates_found, web_hits_found,
                       provider_stats_json, extraction_model,
                       extraction_schema_version, status
                FROM search_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "request": json.loads(row["request_json"]),
                "provider_stats": json.loads(row["provider_stats_json"]),
            }
            for row in rows
        ]

    def list_sources(self, run_id: str) -> list[dict]:
        """Возвращает сохранённые источники запуска вместе с происхождением URL."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT url, domain, status, http_status, note, supplier_name,
                       discovery_query, provider, source_kind
                FROM sources
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_suppliers(self, run_id: str) -> list[dict]:
        """Возвращает сохранённые карточки с группой результата."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT supplier_id, name, score, excluded, result_group, payload
                FROM suppliers
                WHERE run_id = ?
                ORDER BY name
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_cached_page(self, url: str) -> dict | None:
        """Возвращает непросроченную страницу или ``None``."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM page_cache WHERE url = ?", (url,)
            ).fetchone()
        if row is None:
            return None
        cached = dict(row)
        try:
            expires_at = datetime.fromisoformat(cached["expires_at"])
        except (TypeError, ValueError):
            return None
        if expires_at <= datetime.now(timezone.utc):
            return None
        return cached

    def save_cached_page(
        self,
        *,
        url: str,
        final_url: str,
        http_status: int | None,
        mime_type: str | None,
        fetched_at: datetime,
        expires_at: datetime,
        content_hash: str | None,
        html: str | None,
        cleaned_text: str | None,
        error: str | None,
    ) -> None:
        """Сохраняет успешную или ошибочную загрузку для повторного запуска."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO page_cache
                    (url, final_url, http_status, mime_type, fetched_at, expires_at,
                     content_hash, html, cleaned_text, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    final_url,
                    http_status,
                    mime_type,
                    fetched_at.isoformat(),
                    expires_at.isoformat(),
                    content_hash,
                    html,
                    cleaned_text,
                    error,
                ),
            )
