"""Конфигурация приложения: пути, настройки окружения и веса рейтинга."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EXPORT_DIR = DATA_DIR / "exports"


class ScoringWeights(BaseModel):
    """Веса критериев рейтинга.

    Значения вынесены в конфигурацию, чтобы менять правила оценки
    без изменения логики расчёта в ``src/scoring.py``.
    """

    category_match: float = 30
    region_match: float = 20
    delivery_available: float = 15
    minimum_order_found: float = 10
    minimum_order_fits: float = 5
    certificates_found: float = 10
    direct_contacts: float = 5
    price_published: float = 5

    penalty_region_unconfirmed: float = -10
    penalty_secondary_source_only: float = -5
    penalty_page_unavailable: float = -20

    @property
    def max_score(self) -> float:
        """Сумма положительных весов, используется для приведения к 0-100."""
        return (
            self.category_match
            + self.region_match
            + self.delivery_available
            + self.minimum_order_found
            + self.minimum_order_fits
            + self.certificates_found
            + self.direct_contacts
            + self.price_published
        )


class Settings(BaseSettings):
    """Настройки окружения. Читаются из ``.env`` и переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout_seconds: int = 15
    llm_max_suppliers: int = 6
    llm_max_pages_per_supplier: int = 2
    llm_max_chars_per_supplier: int = 18_000
    llm_max_concurrent_requests: int = 2

    two_gis_enabled: bool = False
    two_gis_api_key: str | None = None
    two_gis_base_url: str = "https://catalog.api.2gis.com/3.0"
    yandex_search_api_key: str | None = None
    yandex_folder_id: str | None = None
    brave_search_api_key: str | None = None
    clear_spending_enabled: bool = True
    clear_spending_base_url: str = (
        "https://openapi.clearspending.ru/restapi/v3/contracts/search/"
    )
    clear_spending_timeout_seconds: int = 12
    clear_spending_max_contracts: int = 30
    clear_spending_max_suppliers: int = 12
    productcenter_enabled: bool = True
    eis_enabled: bool = False
    eis_base_url: str = "https://zakupki.gov.ru"
    eis_timeout_seconds: int = 8
    eis_deadline_seconds: int = 12
    eis_max_queries: int = 6
    eis_max_contracts: int = 8
    eis_ca_bundle: Path | None = None
    live_search_deadline_seconds: int = 45
    page_timeout_seconds: int = 8
    max_live_domains: int = 12
    max_response_bytes: int = 2_000_000
    duckduckgo_fallback_enabled: bool = True

    cache_ttl_hours: int = 168
    max_pages_per_domain: int = 5
    max_concurrent_requests: int = 4
    request_timeout_seconds: int = 15

    log_level: str = "INFO"

    database_path: Path = DATA_DIR / "supplier_cache.db"
    export_dir: Path = EXPORT_DIR

    scoring: ScoringWeights = Field(default_factory=ScoringWeights)

    @field_validator("eis_ca_bundle", mode="before")
    @classmethod
    def _empty_ca_bundle_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def web_search_provider(self) -> str:
        """Выбирает официальный веб-поиск, иначе резерв без ключа."""
        if self.yandex_search_api_key and self.yandex_folder_id:
            return "yandex"
        if self.brave_search_api_key:
            return "brave"
        return "duckduckgo"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек."""
    return Settings()
