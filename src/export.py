"""Представление результатов: таблица для интерфейса и выгрузка в CSV."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.models import ScoredSupplier, SourceRecord, SourceStatus, VerificationStatus
from src.scoring import format_breakdown

MISSING = "не найдено"

# Названия полей для интерфейса и выгрузки.
FIELD_LABELS: dict[str, str] = {
    "name": "название",
    "legal_name": "юридическое название",
    "company_type": "тип компании",
    "product_categories": "ассортимент",
    "matched_products": "совпавшие позиции",
    "contract_products": "товары из контрактов ЕИС",
    "website": "сайт",
    "regions": "регион работы",
    "minimum_order": "минимальный заказ",
    "price": "цена",
    "price_access_note": "условия доступа к цене",
    "delivery": "доставка",
    "certificates": "документы",
    "phone": "телефон",
    "email": "email",
    "address": "адрес",
    "inn": "ИНН",
}

TABLE_COLUMNS = (
    "Оценка",
    "Компания",
    "Категории",
    "Регион",
    "Минимальный заказ",
    "Цена",
    "Доставка",
    "Документы",
    "Контакты",
    "Сайт",
    "Проверено",
)

CANDIDATE_COLUMNS = (*TABLE_COLUMNS, "Причина проверки")

_CSV_COLUMNS = (
    *TABLE_COLUMNS,
    "Тип компании",
    "Юридическое название",
    "ИНН",
    "Адрес",
    "Телефон",
    "Email",
    "Незаполненные поля",
    "Расшифровка оценки",
    "Источники",
    "Статус проверки",
    "Причина проверки",
    "Поставки по контрактам ЕИС",
)


def field_label(field_name: str) -> str:
    """Человекочитаемое название поля."""
    return FIELD_LABELS.get(field_name, field_name)


def _shorten(values: list[str], limit: int = 3) -> str:
    if not values:
        return MISSING
    head = ", ".join(values[:limit])
    return head if len(values) <= limit else f"{head} и ещё {len(values) - limit}"


def _contacts(scored: ScoredSupplier) -> str:
    supplier = scored.supplier
    contacts = [value for value in (supplier.display_phone, supplier.email) if value]
    return ", ".join(contacts) if contacts else MISSING


def _row(scored: ScoredSupplier) -> dict[str, object]:
    supplier = scored.supplier
    categories = list(
        dict.fromkeys(
            [
                *supplier.matched_products,
                *supplier.contract_products,
                *supplier.product_categories,
            ]
        )
    )
    return {
        "Оценка": scored.score,
        "Компания": supplier.name,
        "Категории": _shorten(categories),
        "Регион": _shorten(supplier.regions, limit=2),
        "Минимальный заказ": supplier.minimum_order or MISSING,
        "Цена": supplier.display_price or MISSING,
        "Доставка": supplier.delivery or MISSING,
        "Документы": "найдено" if supplier.certificates else MISSING,
        "Контакты": _contacts(scored),
        "Сайт": supplier.domain or MISSING,
        "Проверено": supplier.checked_at.strftime("%d.%m.%Y"),
    }


def to_table(suppliers: list[ScoredSupplier]) -> pd.DataFrame:
    """Таблица поставщиков для интерфейса."""
    if not suppliers:
        return pd.DataFrame(columns=list(TABLE_COLUMNS))
    return pd.DataFrame([_row(scored) for scored in suppliers])


def to_candidate_table(suppliers: list[ScoredSupplier]) -> pd.DataFrame:
    """Таблица компаний, для которых точный товар ещё не подтверждён."""
    if not suppliers:
        return pd.DataFrame(columns=list(CANDIDATE_COLUMNS))
    return pd.DataFrame(
        [
            {
                **_row(scored),
                "Причина проверки": scored.verification_reason or "требует проверки",
            }
            for scored in suppliers
        ],
        columns=list(CANDIDATE_COLUMNS),
    )


def to_comparison_table(suppliers: list[ScoredSupplier]) -> pd.DataFrame:
    """Сравнение выбранных компаний: критерии в строках, компании в столбцах."""
    if not suppliers:
        return pd.DataFrame(columns=["Критерий"])

    rows = [_row(scored) for scored in suppliers]
    criteria = [column for column in TABLE_COLUMNS if column != "Компания"]
    data = {"Критерий": criteria}
    for scored, row in zip(suppliers, rows):
        data[scored.supplier.name] = [row[criterion] for criterion in criteria]
    return pd.DataFrame(data)


_SOURCE_STATUS_LABELS = {
    SourceStatus.OK: "загружено",
    SourceStatus.CACHED: "из кэша",
    SourceStatus.FAILED: "не загрузилось",
    SourceStatus.SKIPPED: "пропущено",
}

_SOURCE_KIND_LABELS = {
    "page": "страница",
    "organization_api": "API организаций",
    "search_result": "результат поиска",
    "official_site": "официальный сайт",
    "catalog": "отраслевой каталог",
    "government_contract": "государственный контракт",
}

SOURCE_COLUMNS = (
    "Компания",
    "Домен",
    "Ссылка",
    "Провайдер",
    "Поисковый запрос",
    "Тип источника",
    "Статус",
    "HTTP",
    "Примечание",
)


def to_sources_table(sources: list[SourceRecord]) -> pd.DataFrame:
    """Все обработанные ссылки со статусом и причиной исключения."""
    if not sources:
        return pd.DataFrame(columns=list(SOURCE_COLUMNS))
    return pd.DataFrame(
        [
            {
                "Компания": source.supplier_name or "—",
                "Домен": source.domain,
                "Ссылка": source.url,
                "Провайдер": source.provider or "—",
                "Поисковый запрос": source.discovery_query or "—",
                "Тип источника": _SOURCE_KIND_LABELS[source.source_kind.value],
                "Статус": _SOURCE_STATUS_LABELS.get(source.status, source.status.value),
                "HTTP": source.http_status or "—",
                "Примечание": source.note or "",
            }
            for source in sources
        ],
        columns=list(SOURCE_COLUMNS),
    )


def to_export_frame(suppliers: list[ScoredSupplier]) -> pd.DataFrame:
    """Полная таблица для выгрузки: значения, оценка, пробелы и источники."""
    records = []
    for scored in suppliers:
        supplier = scored.supplier
        records.append(
            {
                **_row(scored),
                "Тип компании": supplier.company_type.value,
                "Юридическое название": supplier.legal_name or MISSING,
                "ИНН": supplier.inn or MISSING,
                "Адрес": supplier.address or MISSING,
                "Телефон": supplier.display_phone or MISSING,
                "Email": supplier.email or MISSING,
                "Незаполненные поля": ", ".join(
                    field_label(name) for name in scored.missing_fields
                )
                or "нет",
                "Расшифровка оценки": format_breakdown(scored),
                "Источники": " ".join(supplier.source_urls) or MISSING,
                "Статус проверки": (
                    "подтверждён"
                    if scored.verification_status is VerificationStatus.VERIFIED
                    else "кандидат"
                ),
                "Причина проверки": scored.verification_reason or "—",
                "Поставки по контрактам ЕИС": (
                    "; ".join(supplier.contract_products) or MISSING
                ),
            }
        )
    return pd.DataFrame(records, columns=list(_CSV_COLUMNS))


def export_csv(
    suppliers: list[ScoredSupplier],
    export_dir: Path,
    run_id: str | None = None,
) -> Path:
    """Сохраняет CSV и возвращает путь к файлу.

    Кодировка ``utf-8-sig``: файл открывается в Excel без потери кириллицы.
    """
    export_dir.mkdir(parents=True, exist_ok=True)
    suffix = run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = export_dir / f"suppliers-{suffix}.csv"
    to_export_frame(suppliers).to_csv(path, index=False, encoding="utf-8-sig")
    return path
