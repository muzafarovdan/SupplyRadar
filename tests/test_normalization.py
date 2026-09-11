"""Тесты нормализации значений."""

from __future__ import annotations

import pytest

from src.normalization import (
    normalize_company_name,
    normalize_domain,
    normalize_email,
    normalize_inn,
    normalize_phone,
    normalize_region,
    normalize_supplier,
    normalize_url,
    parse_volume_l,
    parse_weight_kg,
    region_covers,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.example.ru/", "example.ru"),
        ("http://Example.RU:8080/catalog", "example.ru"),
        ("example.ru", "example.ru"),
        ("https://shop.example.ru/page", "shop.example.ru"),
        (None, None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_normalize_url_removes_tracking_and_trailing_slash():
    url = "https://www.example.ru/catalog/?utm_source=ya&page=2#anchor"
    assert normalize_url(url) == "https://example.ru/catalog?page=2"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (343) 123-45-67", "+73431234567"),
        ("8 343 1234567", "+73431234567"),
        ("343-12-34", None),
        ("нет телефона", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_normalize_email_lowercases_and_validates():
    assert normalize_email("  Sales@Example.RU ") == "sales@example.ru"
    assert normalize_email("почта без собаки") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ИНН 6658012345", "6658012345"),
        ("665801234567", "665801234567"),
        ("12345", None),
    ],
)
def test_normalize_inn(raw, expected):
    assert normalize_inn(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("г. Екатеринбург", "Екатеринбург"),
        ("ЕКБ", "Екатеринбург"),
        ("Свердловская обл.", "Свердловская область"),
        ("РФ", "Россия"),
    ],
)
def test_normalize_region(raw, expected):
    assert normalize_region(raw) == expected


def test_region_covers_respects_hierarchy():
    assert region_covers("Россия", "Екатеринбург")
    assert region_covers("Свердловская область", "Екатеринбург")
    assert not region_covers("Екатеринбург", "Россия")
    assert not region_covers("Москва", "Екатеринбург")


def test_normalize_company_name_drops_legal_form_and_quotes():
    assert normalize_company_name("ООО «Морозко Трейд»") == "морозко трейд"
    assert normalize_company_name("Торговый дом Морозко") == "морозко"
    assert normalize_company_name(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("от 20 кг", 20.0),
        ("500 г", 0.5),
        ("1,5 т", 1500.0),
        ("от 10 000 руб.", None),
        ("от 1 коробки", None),
        (None, None),
    ],
)
def test_parse_weight_kg(raw, expected):
    assert parse_weight_kg(raw) == expected


def test_parse_volume_l():
    assert parse_volume_l("бидон 20 л") == 20.0
    assert parse_volume_l("500 мл") == 0.5


def test_normalize_supplier_keeps_raw_phone_when_not_parsable(make_supplier):
    supplier = make_supplier(
        website="https://www.morozko-test.ru/",
        phone="343-12-34",
        email="Sales@Morozko-Test.RU",
        regions=["г. Екатеринбург", "ЕКБ"],
        minimum_order="от 20 кг",
        source_urls=[
            "https://morozko-test.ru/catalog?utm_source=ya",
            "https://morozko-test.ru/catalog",
        ],
    )

    normalized = normalize_supplier(supplier)

    assert normalized.domain == "morozko-test.ru"
    assert normalized.phone is None
    assert normalized.phone_raw == "343-12-34"
    assert normalized.display_phone == "343-12-34"
    assert normalized.email == "sales@morozko-test.ru"
    assert normalized.regions == ["Екатеринбург"]
    assert normalized.minimum_order_kg == 20.0
    assert normalized.source_urls == ["https://morozko-test.ru/catalog"]


def test_evidence_links_match_source_list(make_supplier, make_evidence):
    evidence = make_evidence("delivery", "доставка по городу")
    evidence.source_url = "https://morozko-test.ru/dostavka/"
    supplier = make_supplier(
        website="https://morozko-test.ru",
        source_urls=["https://morozko-test.ru/catalog"],
        evidence=[evidence],
    )

    normalized = normalize_supplier(supplier)

    assert normalized.evidence[0].source_url == "https://morozko-test.ru/dostavka"
    assert normalized.evidence[0].source_url in normalized.source_urls


def test_normalize_supplier_handles_empty_fields(make_supplier):
    normalized = normalize_supplier(make_supplier(website=None, phone=None))

    assert normalized.domain is None
    assert normalized.display_phone is None
    assert normalized.minimum_order_kg is None
