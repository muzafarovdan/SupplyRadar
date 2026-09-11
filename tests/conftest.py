"""Общие фикстуры тестов."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import CompanyType, Evidence, SearchRequest, Supplier

CHECKED_AT = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def make_supplier():
    """Создаёт карточку поставщика с заданными полями."""

    def factory(**overrides) -> Supplier:
        defaults = {
            "id": "test-supplier",
            "name": "Тестовый поставщик",
            "company_type": CompanyType.WHOLESALER,
            "product_categories": ["замороженные сырники"],
            "regions": ["Екатеринбург"],
            "checked_at": CHECKED_AT,
        }
        return Supplier(**{**defaults, **overrides})

    return factory


@pytest.fixture
def make_evidence():
    """Создаёт подтверждение значения."""

    def factory(field_name: str, value: str, confidence: float = 0.9) -> Evidence:
        return Evidence(
            field_name=field_name,
            value=value,
            source_url="https://example.ru/page",
            quote=f"цитата про {value}",
            confidence=confidence,
        )

    return factory


@pytest.fixture
def request_frozen() -> SearchRequest:
    """Контрольный запрос демонстрационного сценария."""
    return SearchRequest(
        category="замороженные сырники",
        region="Екатеринбург",
        required_volume_kg=50,
        delivery_required=True,
        documents_required=True,
        max_results=10,
    )
