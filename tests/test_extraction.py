"""Извлечение фактов из сохранённых HTML без LLM."""

from __future__ import annotations

from pathlib import Path

from src.extraction import enrich_supplier
from src.fetching import FetchedPage, clean_html
from src.models import SearchRequest, SourceStatus

FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str) -> FetchedPage:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return FetchedPage(
        url="https://supplier.example",
        final_url="https://supplier.example",
        status=SourceStatus.OK,
        http_status=200,
        mime_type="text/html",
        html=html,
        text=clean_html(html),
    )


def test_extracts_exact_product_and_all_values_have_evidence(make_supplier):
    supplier = make_supplier(
        product_categories=["Мясо птицы"], website="https://supplier.example"
    )
    enriched = enrich_supplier(
        supplier,
        [_page("live_supplier_product.html")],
        SearchRequest(category="филе куриное"),
    )

    assert enriched.matched_products
    assert enriched.phone == "+73431234567"
    assert enriched.email == "opt@morozko.example"
    assert enriched.inn == "6671000000"
    assert enriched.minimum_order_kg == 20
    assert enriched.price
    assert enriched.delivery
    assert enriched.certificates
    for field in ("matched_products", "phone", "email", "inn", "price", "delivery"):
        assert enriched.evidence_for(field)
        assert all(
            item.quote and item.source_url for item in enriched.evidence_for(field)
        )


def test_general_category_does_not_confirm_exact_product(make_supplier):
    enriched = enrich_supplier(
        make_supplier(product_categories=["Мясо птицы"]),
        [_page("live_supplier_general.html")],
        SearchRequest(category="филе куриное"),
    )
    assert enriched.matched_products == []
    assert enriched.evidence_for("matched_products") == []


def test_json_ld_product_name_confirms_product(make_supplier):
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Филе куриное охлаждённое"}'
        "</script>"
    )
    page = FetchedPage(
        url="https://supplier.example/product",
        final_url="https://supplier.example/product",
        status=SourceStatus.OK,
        html=html,
        text=clean_html(html),
    )
    enriched = enrich_supplier(
        make_supplier(product_categories=[]),
        [page],
        SearchRequest(category="филе куриное"),
    )
    assert enriched.matched_products == ["Филе куриное охлаждённое"]


def test_product_modifier_must_also_be_present(make_supplier):
    html = "<h1>Сырники творожные оптом</h1>"
    page = FetchedPage(
        url="https://supplier.example/product",
        final_url="https://supplier.example/product",
        status=SourceStatus.OK,
        html=html,
        text=clean_html(html),
    )
    enriched = enrich_supplier(
        make_supplier(product_categories=[]),
        [page],
        SearchRequest(category="замороженные сырники"),
    )
    assert enriched.matched_products == []


def test_catalog_access_fee_is_explained_but_not_saved_as_product_price(
    make_supplier,
):
    html = """
    <h1>Филе куриное оптом</h1>
    <p>Цена доступа — 250 рублей, на 24 часа откроются все контакты в списке.</p>
    """
    page = FetchedPage(
        url="https://productcenter.ru/products/1/file",
        final_url="https://productcenter.ru/products/1/file",
        status=SourceStatus.OK,
        html=html,
        text=clean_html(html),
    )

    enriched = enrich_supplier(
        make_supplier(product_categories=[]),
        [page],
        SearchRequest(category="филе куриное"),
    )

    assert enriched.price is None
    assert enriched.price_access_note
    assert "250 рублей" in enriched.display_price
    assert enriched.evidence_for("price") == []
    assert enriched.evidence_for("price_access_note")


def test_product_price_wins_over_catalog_access_fee(make_supplier):
    html = """
    <h1>Филе куриное. Цена 300р. за кг.</h1>
    <p>Цена доступа — 250 рублей, на 24 часа откроются все контакты в списке.</p>
    """
    page = FetchedPage(
        url="https://productcenter.ru/products/1/file",
        final_url="https://productcenter.ru/products/1/file",
        status=SourceStatus.OK,
        html=html,
        text=clean_html(html),
    )

    enriched = enrich_supplier(
        make_supplier(product_categories=[]),
        [page],
        SearchRequest(category="филе куриное"),
    )

    assert enriched.price == "300р"
    assert enriched.price_access_note is None
