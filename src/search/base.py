"""Контракт источника поставщиков.

Пайплайн не знает, откуда пришли данные: из сохранённого набора или из сети.
Это позволяет добавлять источники, не меняя обработку результатов.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from src.models import SearchRequest, SourceRecord, Supplier

ProgressCallback = Callable[[str], None]


class ProviderUnavailableError(RuntimeError):
    """Источник нельзя использовать: нет ключа, файла данных или доступа."""


@dataclass
class ProviderResult:
    """Сырые карточки поставщиков и сведения о обработанных источниках."""

    suppliers: list[Supplier] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages_failed: int = 0
    cache_hits: int = 0
    web_hits_found: int = 0
    provider_stats: dict[str, str] = field(default_factory=dict)
    extraction_model: str | None = None
    extraction_schema_version: int | None = None


class SupplierProvider(ABC):
    """Источник карточек поставщиков."""

    name: str

    @abstractmethod
    def collect(
        self,
        request: SearchRequest,
        queries: list[str],
        progress: ProgressCallback,
    ) -> ProviderResult:
        """Собирает карточки поставщиков по запросу.

        Сбой отдельной страницы или отдельного источника не должен прерывать
        работу: он попадает в ``warnings`` и в ``sources`` со статусом ошибки.
        """
