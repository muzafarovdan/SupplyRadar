"""Источники поставщиков и их выбор по режиму работы."""

from __future__ import annotations

from src.config import Settings
from src.models import SearchRequest
from src.search.base import (
    ProviderResult,
    ProviderUnavailableError,
    SupplierProvider,
)
from src.search.clear_spending import ClearSpendingProvider
from src.search.eis import EisContractProvider
from src.search.live import LiveSupplierProvider
from src.search.two_gis import TwoGisSupplierProvider

__all__ = [
    "ClearSpendingProvider",
    "EisContractProvider",
    "LiveSupplierProvider",
    "ProviderResult",
    "ProviderUnavailableError",
    "SupplierProvider",
    "TwoGisSupplierProvider",
    "create_supplier_provider",
]


def create_supplier_provider(
    request: SearchRequest, settings: Settings
) -> SupplierProvider:
    """Возвращает составной источник исключительно живых данных."""
    return LiveSupplierProvider(settings)
