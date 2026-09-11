"""Тесты объединения записей одной компании."""

from __future__ import annotations

from datetime import datetime, timezone

from src.deduplication import deduplicate


def test_merges_by_domain_and_keeps_all_sources(make_supplier):
    first = make_supplier(
        id="a",
        domain="morozko.ru",
        website="https://morozko.ru",
        source_urls=["https://morozko.ru"],
        phone="+73431234567",
    )
    second = make_supplier(
        id="b",
        name="Морозко, оптовый склад",
        domain="morozko.ru",
        website="https://morozko.ru",
        source_urls=["https://morozko.ru/dostavka"],
        delivery="доставка по Екатеринбургу",
    )

    result = deduplicate([first, second])

    assert result.merged_count == 1
    assert len(result.suppliers) == 1
    merged = result.suppliers[0]
    assert merged.delivery == "доставка по Екатеринбургу"
    assert merged.phone == "+73431234567"
    assert merged.source_urls == ["https://morozko.ru", "https://morozko.ru/dostavka"]


def test_name_from_company_site_wins_over_catalog_name(make_supplier):
    site = make_supplier(
        id="a",
        name="Морозко",
        website="https://morozko.ru",
        domain="morozko.ru",
        phone="+73431234567",
    )
    catalog = make_supplier(
        id="b",
        name="Морозко, оптовый склад замороженных продуктов",
        website=None,
        phone="+73431234567",
        source_urls=["https://catalog.example.ru/company/morozko"],
    )

    merged = deduplicate([catalog, site]).suppliers[0]

    assert merged.name == "Морозко"
    assert "https://catalog.example.ru/company/morozko" in merged.source_urls


def test_merges_by_inn_across_different_domains(make_supplier):
    first = make_supplier(id="a", inn="6658012345", domain="site-one.ru")
    second = make_supplier(id="b", inn="6658012345", domain="site-two.ru")

    result = deduplicate([first, second])

    assert len(result.suppliers) == 1


def test_merges_by_phone(make_supplier):
    first = make_supplier(id="a", phone="+73431234567", domain="site-one.ru")
    second = make_supplier(id="b", phone="+73431234567", domain="site-two.ru")

    assert len(deduplicate([first, second]).suppliers) == 1


def test_similar_names_without_weak_signal_are_kept_apart(make_supplier):
    first = make_supplier(id="a", name="Морозко", domain="site-one.ru")
    second = make_supplier(id="b", name="Морозко", domain="site-two.ru")

    assert len(deduplicate([first, second]).suppliers) == 2


def test_similar_names_with_same_address_are_merged(make_supplier):
    first = make_supplier(
        id="a",
        name="ООО «Морозко»",
        domain="site-one.ru",
        address="Екатеринбург, ул. Промышленная, 5",
    )
    second = make_supplier(
        id="b",
        name="Морозко",
        domain="site-two.ru",
        address="г. Екатеринбург, улица Промышленная, 5",
    )

    assert len(deduplicate([first, second]).suppliers) == 1


def test_confirmed_value_wins_over_unconfirmed(make_supplier, make_evidence):
    stale = make_supplier(
        id="a",
        domain="morozko.ru",
        minimum_order="от 100 кг",
        evidence=[make_evidence("minimum_order", "от 100 кг", confidence=0.95)],
        checked_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    )
    fresh_guess = make_supplier(
        id="b",
        domain="morozko.ru",
        minimum_order="от 5 кг",
        checked_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    )

    merged = deduplicate([stale, fresh_guess]).suppliers[0]

    assert merged.minimum_order == "от 100 кг"
    assert merged.checked_at == datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def test_empty_input():
    result = deduplicate([])

    assert result.suppliers == []
    assert result.merged_count == 0
