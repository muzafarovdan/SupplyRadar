"""Последовательность этапов обработки запроса.

Пайплайн связывает источник данных, нормализацию, дедупликацию и рейтинг.
Сбой отдельного источника или отдельной карточки не прерывает работу:
проблема попадает в предупреждения, а остальные результаты доходят до пользователя.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter

from src.config import Settings, get_settings
from src.deduplication import deduplicate
from src.models import (
    RunStats,
    ScoredSupplier,
    SearchRequest,
    SearchRunResult,
    SourceRecord,
    SourceStatus,
    Supplier,
    VerificationStatus,
)
from src.query_builder import build_queries
from src.scoring import SupplierScorer
from src.search import (
    ProviderResult,
    ProviderUnavailableError,
    SupplierProvider,
    create_supplier_provider,
)
from src.storage import Storage

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

STAGES = (
    "Формируем поисковые запросы",
    "Получаем ссылки",
    "Проверяем сайты",
    "Извлекаем сведения",
    "Удаляем дубли и рассчитываем рейтинг",
    "Формируем результат",
)

ProviderFactory = Callable[[SearchRequest, Settings], SupplierProvider]


def _noop(_: str) -> None:
    """Заглушка, когда вызывающая сторона не следит за прогрессом."""


class SupplierSearchPipeline:
    """Выполняет запрос пользователя и возвращает сравнимый список поставщиков."""

    def __init__(
        self,
        settings: Settings | None = None,
        provider_factory: ProviderFactory = create_supplier_provider,
        scorer: SupplierScorer | None = None,
        storage: Storage | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider_factory = provider_factory
        self._scorer = scorer or SupplierScorer(self._settings.scoring)
        self._storage = storage

    def run(
        self, request: SearchRequest, progress: ProgressCallback = _noop
    ) -> SearchRunResult:
        """Проходит все этапы и собирает итоговый результат."""
        started_at = datetime.now(timezone.utc)
        timer = perf_counter()
        stats = RunStats(run_id=uuid.uuid4().hex[:12], started_at=started_at)
        warnings: list[str] = []

        report = _StageReporter(progress)

        report.stage(0)
        queries = build_queries(request)
        stats.queries_used = queries
        if not queries:
            warnings.append("Категория не указана, запросы не сформированы.")

        report.stage(1)
        collected = self._collect(request, queries, report, warnings)
        stats.urls_found = len(collected.sources)
        stats.pages_processed = sum(
            source.status in {SourceStatus.OK, SourceStatus.CACHED}
            for source in collected.sources
        )
        stats.pages_failed = collected.pages_failed
        stats.cache_hits = collected.cache_hits
        stats.web_hits_found = collected.web_hits_found
        stats.provider_stats = collected.provider_stats
        stats.extraction_model = collected.extraction_model
        stats.extraction_schema_version = collected.extraction_schema_version
        warnings.extend(collected.warnings)

        report.stage(4)
        deduplicated = deduplicate(collected.suppliers)
        stats.duplicates_merged = deduplicated.merged_count

        scored, candidates, excluded = self._score(
            deduplicated.suppliers, request, collected.sources, warnings
        )
        stats.suppliers_found = len(scored)
        stats.candidates_found = len(candidates)
        stats.suppliers_excluded = len(excluded)

        report.stage(5)
        stats.duration_seconds = round(perf_counter() - timer, 2)
        result = SearchRunResult(
            request=request,
            suppliers=scored[: request.max_results],
            candidates=candidates[: request.max_results],
            excluded=excluded,
            sources=collected.sources,
            stats=stats,
            warnings=warnings,
        )
        self._persist(result, warnings)
        return result

    def _collect(
        self,
        request: SearchRequest,
        queries: list[str],
        report: _StageReporter,
        warnings: list[str],
    ) -> ProviderResult:
        """Получает карточки из источника, не падая при его недоступности."""
        try:
            provider = self._provider_factory(request, self._settings)
        except ProviderUnavailableError as error:
            warnings.append(str(error))
            return ProviderResult()

        bind_storage = getattr(provider, "bind_storage", None)
        if callable(bind_storage):
            bind_storage(self._storage)

        report.stage(2)
        try:
            result = provider.collect(request, queries, report.detail)
        except ProviderUnavailableError as error:
            warnings.append(str(error))
            return ProviderResult()
        except Exception as error:  # источник не должен ломать весь запуск
            logger.exception("Источник %s завершился ошибкой", provider.name)
            warnings.append(
                f"Источник «{provider.name}» недоступен, результат неполный: {error}"
            )
            return ProviderResult()

        report.stage(3)
        return result

    def _score(
        self,
        suppliers: list[Supplier],
        request: SearchRequest,
        sources: list[SourceRecord],
        warnings: list[str],
    ) -> tuple[list[ScoredSupplier], list[ScoredSupplier], list[ScoredSupplier]]:
        """Оценивает поставщиков и отделяет исключённых."""
        scored: list[ScoredSupplier] = []
        candidates: list[ScoredSupplier] = []
        excluded: list[ScoredSupplier] = []

        for supplier in suppliers:
            try:
                page_unavailable = _all_sources_failed(supplier, sources)
                result = self._scorer.score(
                    supplier,
                    request,
                    page_unavailable=page_unavailable,
                )
                if not _has_product_evidence(supplier):
                    if result.excluded and not supplier.discovery_queries:
                        excluded.append(result)
                        continue
                    result = self._scorer.score_candidate(
                        supplier, request, page_unavailable=page_unavailable
                    )
            except Exception as error:  # одна карточка не должна ломать выдачу
                logger.exception("Не удалось оценить поставщика %s", supplier.id)
                warnings.append(
                    f"Поставщик «{supplier.name}» пропущен при расчёте оценки: {error}"
                )
                continue
            if result.excluded:
                excluded.append(result)
            elif result.verification_status is VerificationStatus.CANDIDATE:
                candidates.append(result)
            else:
                scored.append(result)

        scored.sort(key=lambda item: (-item.score, item.supplier.name))
        candidates.sort(key=lambda item: (-item.score, item.supplier.name))
        return scored, candidates, excluded

    def _persist(self, result: SearchRunResult, warnings: list[str]) -> None:
        if self._storage is None:
            return
        try:
            self._storage.save_run(result)
        except Exception as error:  # сохранение не должно ломать выдачу
            logger.exception("Не удалось сохранить запуск %s", result.stats.run_id)
            warnings.append(f"Результат не сохранён в базу: {error}")


class _StageReporter:
    """Сообщает пользователю текущий этап в виде «3/6 Проверяем сайты»."""

    def __init__(self, progress: ProgressCallback) -> None:
        self._progress = progress
        self._current = 0

    def stage(self, index: int) -> None:
        self._current = index
        self._progress(f"{index + 1}/{len(STAGES)} {STAGES[index]}")

    def detail(self, message: str) -> None:
        self._progress(
            f"{self._current + 1}/{len(STAGES)} {STAGES[self._current]}: {message}"
        )


def _all_sources_failed(supplier: Supplier, sources: list[SourceRecord]) -> bool:
    """Проверяет, что ни одна страница поставщика не открылась."""
    related = [source for source in sources if source.url in supplier.source_urls]
    return bool(related) and all(
        source.status is SourceStatus.FAILED for source in related
    )


def _has_product_evidence(supplier: Supplier) -> bool:
    return any(item.field_name == "matched_products" for item in supplier.evidence)
