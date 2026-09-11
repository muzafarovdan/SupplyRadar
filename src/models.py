"""Модели данных сервиса.

Ключевое правило: незаполненное поле остаётся ``None`` или пустым списком.
Значения не додумываются, а содержательные факты сопровождаются ``Evidence``
с ссылкой на страницу-источник и дословной цитатой.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, Field


def _validate_http_url(value: str) -> str:
    """Проверяет, что строка — это http(s)-ссылка.

    Используется вместо ``pydantic.HttpUrl``, потому что ``HttpUrl`` добавляет
    завершающий слэш и требует отдельной сериализации. Здесь ссылка нужна
    в исходном виде: она попадает в CSV, в интерфейс и в ключи кэша.
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"ожидается http(s)-ссылка, получено {value!r}")
    return value


Url = Annotated[str, AfterValidator(_validate_http_url)]


class CompanyType(str, Enum):
    """Тип компании. Нужен, чтобы отделять опт от розницы."""

    MANUFACTURER = "manufacturer"
    DISTRIBUTOR = "distributor"
    WHOLESALER = "wholesaler"
    MARKETPLACE = "marketplace"
    RETAILER = "retailer"
    UNKNOWN = "unknown"


class SourceStatus(str, Enum):
    """Итог обработки страницы-источника."""

    OK = "ok"
    CACHED = "cached"
    FAILED = "failed"
    SKIPPED = "skipped"


class SourceKind(str, Enum):
    """Роль источника и уровень его первичности."""

    PAGE = "page"
    ORGANIZATION_API = "organization_api"
    SEARCH_RESULT = "search_result"
    OFFICIAL_SITE = "official_site"
    CATALOG = "catalog"
    GOVERNMENT_CONTRACT = "government_contract"


class VerificationStatus(str, Enum):
    """Степень подтверждения поставщика относительно запроса."""

    VERIFIED = "verified"
    CANDIDATE = "candidate"
    EXCLUDED = "excluded"


class Evidence(BaseModel):
    """Подтверждение конкретного значения цитатой из источника."""

    field_name: str
    value: str
    source_url: Url
    quote: str
    confidence: float = Field(ge=0, le=1)


class Supplier(BaseModel):
    """Карточка поставщика, приведённая к общей структуре."""

    id: str
    name: str
    legal_name: str | None = None
    inn: str | None = None
    company_type: CompanyType = CompanyType.UNKNOWN

    product_categories: list[str] = []
    matched_products: list[str] = []
    contract_products: list[str] = []

    address: str | None = None
    regions: list[str] = []

    minimum_order: str | None = None
    minimum_order_kg: float | None = None
    price: str | None = None
    price_access_note: str | None = None
    delivery: str | None = None
    certificates: list[str] = []

    phone: str | None = None
    phone_raw: str | None = None
    email: str | None = None
    website: Url | None = None
    domain: str | None = None

    source_urls: list[Url] = []
    discovery_queries: list[str] = []
    evidence: list[Evidence] = []

    checked_at: datetime

    @property
    def display_phone(self) -> str | None:
        """Телефон для показа: нормализованный, иначе исходный со страницы."""
        return self.phone or self.phone_raw

    @property
    def display_price(self) -> str | None:
        """Цена товара либо пояснение, почему она скрыта источником."""
        return self.price or self.price_access_note

    def evidence_for(self, field_name: str) -> list[Evidence]:
        """Подтверждения для одного поля."""
        return [item for item in self.evidence if item.field_name == field_name]


class ScoreItem(BaseModel):
    """Одна строка расшифровки рейтинга."""

    points: float
    reason: str


class ScoredSupplier(BaseModel):
    """Поставщик с оценкой относительно конкретного запроса.

    Оценка зависит от запроса, поэтому она не хранится в ``Supplier``:
    одна и та же компания получает разные баллы для разных потребностей.
    """

    supplier: Supplier
    score: float = 0
    breakdown: list[ScoreItem] = []
    missing_fields: list[str] = []
    excluded: bool = False
    exclusion_reason: str | None = None
    verification_status: VerificationStatus = VerificationStatus.VERIFIED
    verification_reason: str | None = None


class SearchRequest(BaseModel):
    """Потребность пользователя."""

    category: str
    region: str | None = None
    required_volume_kg: float | None = None
    delivery_required: bool = False
    documents_required: bool = False
    extra_requirements: str | None = None
    eis_queries: list[str] = []
    max_results: int = 10


class SourceRecord(BaseModel):
    """Обработанный источник для вкладки «Источники»."""

    url: Url
    domain: str
    status: SourceStatus
    http_status: int | None = None
    note: str | None = None
    supplier_name: str | None = None
    discovery_query: str | None = None
    provider: str | None = None
    source_kind: SourceKind = SourceKind.PAGE


class RunStats(BaseModel):
    """Показатели одного запуска поиска."""

    run_id: str
    started_at: datetime
    duration_seconds: float = 0
    queries_used: list[str] = []
    urls_found: int = 0
    pages_processed: int = 0
    pages_failed: int = 0
    cache_hits: int = 0
    suppliers_found: int = 0
    candidates_found: int = 0
    suppliers_excluded: int = 0
    duplicates_merged: int = 0
    web_hits_found: int = 0
    provider_stats: dict[str, str] = {}
    extraction_model: str | None = None
    extraction_schema_version: int | None = None


class SearchRunResult(BaseModel):
    """Полный результат запуска: поставщики, источники, показатели, ошибки."""

    request: SearchRequest
    suppliers: list[ScoredSupplier] = []
    candidates: list[ScoredSupplier] = []
    excluded: list[ScoredSupplier] = []
    sources: list[SourceRecord] = []
    stats: RunStats
    warnings: list[str] = []
