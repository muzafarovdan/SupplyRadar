"""Проверки адаптера живого поиска через 2ГИС."""

from __future__ import annotations

import httpx

from src.config import Settings
from src.models import CompanyType, SearchRequest, SourceStatus
from src.search import (
    LiveSupplierProvider,
    TwoGisSupplierProvider,
    create_supplier_provider,
)


def _request() -> SearchRequest:
    return SearchRequest(
        category="замороженные продукты оптом",
        region="Екатеринбург",
        max_results=10,
    )


def _item(item_id: str = "70000001000000001") -> dict:
    return {
        "id": item_id,
        "name": "Морозко Опт",
        "full_name": "Оптовая компания Морозко",
        "address_name": "Екатеринбург, улица Тестовая, 1",
        "link": "/ekaterinburg/firm/70000001000000001",
        "rubrics": [{"name": "Замороженные продукты"}, {"name": "Оптовая торговля"}],
        "adm_div": [{"name": "Екатеринбург"}, {"name": "Свердловская область"}],
        "org": {"name": "ООО «Морозко»"},
        "contact_groups": [
            {
                "contacts": [
                    {"type": "phone", "value": "+7 (343) 123-45-67"},
                    {"type": "email", "value": "OPT@MOROZKO.RU"},
                    {"type": "website", "value": "morozko.ru"},
                ]
            }
        ],
    }


def _provider(handler) -> tuple[TwoGisSupplierProvider, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TwoGisSupplierProvider("secret-key", client=client), client


def test_factory_allows_web_fallback_without_two_gis_key(tmp_path):
    settings = Settings(
        _env_file=None,
        two_gis_api_key=None,
        duckduckgo_fallback_enabled=False,
        database_path=tmp_path / "cache.db",
    )

    provider = create_supplier_provider(_request(), settings)

    assert isinstance(provider, LiveSupplierProvider)


def test_collect_maps_structured_card_and_records_url_origin():
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={"meta": {"code": 200}, "result": {"items": [_item()]}},
        )

    provider, client = _provider(handler)
    try:
        result = provider.collect(
            _request(), ["сырники оптом Екатеринбург"], lambda _: None
        )
    finally:
        client.close()

    assert captured_request is not None
    assert captured_request.url.params["q"] == "сырники оптом Екатеринбург"
    assert captured_request.url.params["key"] == "secret-key"
    assert captured_request.url.params["type"] == "branch"

    supplier = result.suppliers[0]
    assert supplier.id == "2gis-70000001000000001"
    assert supplier.legal_name == "ООО «Морозко»"
    assert supplier.company_type is CompanyType.WHOLESALER
    assert supplier.phone == "+73431234567"
    assert supplier.email == "opt@morozko.ru"
    assert supplier.website == "https://morozko.ru"
    assert supplier.regions == ["Екатеринбург", "Свердловская область"]
    assert supplier.product_categories == ["Замороженные продукты", "Оптовая торговля"]
    assert all(item.quote == item.value for item in supplier.evidence)

    assert [source.status for source in result.sources] == [
        SourceStatus.OK,
        SourceStatus.SKIPPED,
    ]
    assert all(source.provider == "2ГИС Places API" for source in result.sources)
    assert all(
        source.discovery_query == "сырники оптом Екатеринбург"
        for source in result.sources
    )
    assert all("secret-key" not in source.url for source in result.sources)


def test_duplicate_item_from_two_queries_is_returned_once():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"meta": {"code": 200}, "result": {"items": [_item()]}},
        )

    provider, client = _provider(handler)
    try:
        result = provider.collect(_request(), ["первый", "второй"], lambda _: None)
    finally:
        client.close()

    assert len(result.suppliers) == 1
    assert len(result.sources) == 2


def test_rate_limit_stops_search_with_clear_warning():
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"meta": {"code": 429}})

    provider, client = _provider(handler)
    try:
        result = provider.collect(_request(), ["первый", "второй"], lambda _: None)
    finally:
        client.close()

    assert calls == 1
    assert result.suppliers == []
    assert result.pages_failed == 1
    assert result.sources[0].http_status == 429
    assert "лимит" in result.warnings[0]


def test_one_failed_query_does_not_discard_later_results():
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"meta": {"code": 200}, "result": {"items": [_item()]}},
        )

    provider, client = _provider(handler)
    try:
        result = provider.collect(_request(), ["первый", "второй"], lambda _: None)
    finally:
        client.close()

    assert len(result.suppliers) == 1
    assert result.pages_failed == 1
    assert result.sources[0].status is SourceStatus.FAILED
    assert any("HTTP 503" in warning for warning in result.warnings)


def test_invalid_item_is_skipped_without_stopping_query():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "meta": {"code": 200},
                "result": {"items": [{"id": "broken"}, _item("valid")]},
            },
        )

    provider, client = _provider(handler)
    try:
        result = provider.collect(_request(), ["сырники"], lambda _: None)
    finally:
        client.close()

    assert [supplier.id for supplier in result.suppliers] == ["2gis-valid"]
    assert any("broken" in warning for warning in result.warnings)


def test_connect_timeout_stops_repeated_places_requests():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("TLS timeout", request=request)

    provider, client = _provider(handler)
    try:
        result = provider.collect(_request(), ["первый", "второй"], lambda _: None)
    finally:
        client.close()

    assert calls == 1
    assert result.pages_failed == 1
    assert any("Остальные запросы" in warning for warning in result.warnings)
