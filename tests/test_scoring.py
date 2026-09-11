"""Тесты расчёта рейтинга."""

from __future__ import annotations

from src.config import ScoringWeights
from src.models import CompanyType, SearchRequest
from src.scoring import SupplierScorer, format_breakdown


def test_full_match_gets_maximum_score(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники", "блинчики"],
        regions=["Екатеринбург"],
        delivery="доставка по Екатеринбургу и области",
        minimum_order="от 20 кг",
        minimum_order_kg=20,
        certificates=["декларация соответствия ТР ТС 021/2011"],
        phone="+73431234567",
        price="от 210 руб/кг",
        website="https://morozko.ru",
        domain="morozko.ru",
    )

    result = SupplierScorer().score(supplier, request_frozen)

    assert result.score == 100
    assert not result.excluded
    assert result.missing_fields == ["email", "address", "inn"]


def test_missing_data_lowers_score_without_guessing(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        regions=["Екатеринбург"],
        website="https://morozko.ru",
        domain="morozko.ru",
    )

    result = SupplierScorer().score(supplier, request_frozen)

    assert result.score == 50
    assert "minimum_order" in result.missing_fields
    assert "price" in result.missing_fields
    assert "цена не опубликована" in format_breakdown(result)


def test_retailer_is_excluded(make_supplier, request_frozen):
    supplier = make_supplier(company_type=CompanyType.RETAILER)

    result = SupplierScorer().score(supplier, request_frozen)

    assert result.excluded
    assert result.score == 0
    assert "розничная" in (result.exclusion_reason or "")


def test_other_category_is_excluded(make_supplier, request_frozen):
    supplier = make_supplier(product_categories=["упаковка для пищевых продуктов"])

    result = SupplierScorer().score(supplier, request_frozen)

    assert result.excluded
    assert "категорию" in (result.exclusion_reason or "")


def test_prepared_dish_does_not_verify_raw_product(make_supplier):
    supplier = make_supplier(
        product_categories=[],
        matched_products=["Филе куриное запечённое с овощами"],
    )

    result = SupplierScorer().score(supplier, SearchRequest(category="филе куриное"))

    assert result.excluded
    assert "категорию" in (result.exclusion_reason or "")


def test_related_products_give_no_category_points_but_keep_supplier(
    make_supplier, request_frozen
):
    supplier = make_supplier(
        product_categories=["замороженные пельмени", "замороженные вареники"],
        regions=["Екатеринбург"],
        website="https://pelmeni.ru",
        domain="pelmeni.ru",
    )

    result = SupplierScorer().score(supplier, request_frozen)

    assert not result.excluded
    assert result.breakdown[0].points == 0
    assert "найдены смежные товары" in result.breakdown[0].reason


def test_generic_word_alone_does_not_confirm_category(make_supplier, request_frozen):
    with_item = make_supplier(
        id="a",
        product_categories=["замороженные сырники"],
        website="https://a.ru",
        domain="a.ru",
    )
    without_item = make_supplier(
        id="b",
        product_categories=["замороженные куриные полуфабрикаты"],
        website="https://b.ru",
        domain="b.ru",
    )

    scorer = SupplierScorer()

    assert (
        scorer.score(with_item, request_frozen).score
        > scorer.score(without_item, request_frozen).score
    )


def test_marketplace_is_excluded(make_supplier, request_frozen):
    supplier = make_supplier(
        company_type=CompanyType.MARKETPLACE,
        product_categories=["каталог производителей замороженных сырников"],
    )

    result = SupplierScorer().score(supplier, request_frozen)

    assert result.excluded
    assert "каталог" in (result.exclusion_reason or "")


def test_company_page_on_catalog_domain_is_penalized(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        website="https://productcenter.ru/producers/oblaka",
        domain="productcenter.ru",
        source_urls=["https://productcenter.ru/producers/oblaka"],
    )

    result = SupplierScorer().score(supplier, request_frozen)
    reasons = [item.reason for item in result.breakdown if item.points < 0]

    assert any("каталога организаций" in reason for reason in reasons)


def test_unconfirmed_region_is_penalized(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        regions=["Москва"],
        website="https://morozko.ru",
        domain="morozko.ru",
    )

    result = SupplierScorer().score(supplier, request_frozen)

    penalties = [item for item in result.breakdown if item.points < 0]
    assert penalties
    assert result.score == 20


def test_word_forms_are_matched(make_supplier):
    supplier = make_supplier(
        product_categories=["сырник замороженный весовой"],
        website="https://morozko.ru",
        domain="morozko.ru",
    )
    request = SearchRequest(category="замороженные сырники оптом")

    result = SupplierScorer().score(supplier, request)

    assert not result.excluded
    assert result.breakdown[0].points == 30


def test_minimum_order_above_requested_volume_gets_no_bonus(
    make_supplier, request_frozen
):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        minimum_order="от 500 кг",
        minimum_order_kg=500,
        website="https://morozko.ru",
        domain="morozko.ru",
    )

    result = SupplierScorer().score(supplier, request_frozen)
    reasons = [item.reason for item in result.breakdown if item.points == 0]

    assert any("выше объёма" in reason for reason in reasons)


def test_secondary_source_only_is_penalized(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        website=None,
        source_urls=["https://catalog.example.ru/company/morozko"],
    )

    result = SupplierScorer().score(supplier, request_frozen)
    reasons = [item.reason for item in result.breakdown if item.points < 0]

    assert any("каталога организаций" in reason for reason in reasons)


def test_unavailable_pages_are_penalized(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        website="https://morozko.ru",
        domain="morozko.ru",
    )

    result = SupplierScorer().score(supplier, request_frozen, page_unavailable=True)
    reasons = [item.reason for item in result.breakdown if item.points < 0]

    assert any("не открылись" in reason for reason in reasons)


def test_weights_are_configurable(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        regions=["Екатеринбург"],
        website="https://morozko.ru",
        domain="morozko.ru",
    )
    weights = ScoringWeights(category_match=10, region_match=90)

    result = SupplierScorer(weights).score(supplier, request_frozen)

    assert result.breakdown[0].points == 10
    assert result.breakdown[1].points == 90


def test_breakdown_sums_to_score(make_supplier, request_frozen):
    supplier = make_supplier(
        product_categories=["замороженные сырники"],
        regions=["Свердловская область"],
        delivery="самовывоз и доставка",
        phone="+73431234567",
        website="https://morozko.ru",
        domain="morozko.ru",
    )

    result = SupplierScorer().score(supplier, request_frozen)

    assert sum(item.points for item in result.breakdown) == result.score
    assert format_breakdown(result).endswith(f"Итого: {result.score:g}/100")
