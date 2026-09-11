"""Контрактные тесты веб-поиска без внешних запросов."""

from __future__ import annotations

import asyncio
import base64

import httpx

from src.extraction import supplier_from_hit
from src.models import CompanyType
from src.web_search import (
    BraveSearchProvider,
    CompositeWebSearch,
    DuckDuckGoSearchProvider,
    SearchHit,
    YandexSearchProvider,
    deduplicate_hits,
)


def test_brave_maps_json_and_sends_key_in_header():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-subscription-token"] == "secret"
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "url": "https://supplier.example/catalog",
                                "title": "Поставщик <b>филе</b>",
                                "description": "Филе оптом",
                            }
                        ]
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = BraveSearchProvider("secret", client)
            return await provider.search("филе оптом", 5)

    hits = asyncio.run(scenario())
    assert hits[0].url == "https://supplier.example/catalog"
    assert hits[0].title == "Поставщик филе"
    assert hits[0].provider == "Brave Search"


def test_yandex_decodes_xml_response():
    xml = """<yandexsearch><response><results><grouping><group><doc>
    <url>https://supplier.example/product</url><title>Филе куриное</title>
    <passages><passage>Продажа оптом</passage></passages>
    </doc></group></grouping></results></response></yandexsearch>"""

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"rawData": base64.b64encode(xml.encode()).decode()}
                )
            )
        ) as client:
            provider = YandexSearchProvider("key", "folder", client)
            return await provider.search("филе", 5)

    hits = asyncio.run(scenario())
    assert hits[0].title == "Филе куриное"
    assert hits[0].snippet == "Продажа оптом"


def test_duckduckgo_parses_html_and_unwraps_redirect():
    body = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsupplier.example%2Fcatalog">Поставщик</a>
      <div class="result__snippet">Филе куриное оптом</div>
    </div>
    """

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
        ) as client:
            return await DuckDuckGoSearchProvider(client).search("филе", 5)

    hits = asyncio.run(scenario())
    assert hits[0].url == "https://supplier.example/catalog"
    assert hits[0].snippet == "Филе куриное оптом"


def test_composite_uses_fallback_after_primary_failure():
    class Broken:
        name = "primary"

        async def search(self, query, limit, supplier_id=None):
            raise httpx.ConnectError("offline")

    class Fallback:
        name = "fallback"

        async def search(self, query, limit, supplier_id=None):
            return [
                SearchHit(
                    "https://supplier.example", "Supplier", "", query, self.name, 1
                )
            ]

    outcome = asyncio.run(
        CompositeWebSearch(Broken(), Fallback()).search([("филе", None)], 5)
    )
    assert [hit.url for hit in outcome.hits] == ["https://supplier.example"]
    assert outcome.fallback_used
    assert outcome.warnings


def test_composite_explains_rate_limit_and_uses_fallback():
    class Limited:
        name = "official"

        async def search(self, query, limit, supplier_id=None):
            request = httpx.Request("GET", "https://search.example")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("limited", request=request, response=response)

    class Fallback:
        name = "DuckDuckGo"

        async def search(self, query, limit, supplier_id=None):
            return [
                SearchHit(
                    "https://supplier.example", "Supplier", "", query, self.name, 1
                )
            ]

    outcome = asyncio.run(
        CompositeWebSearch(Limited(), Fallback()).search([("филе", None)], 5)
    )
    assert outcome.hits
    assert any("лимит" in warning for warning in outcome.warnings)


def test_dedup_prefers_hit_linked_to_supplier_and_blocks_marketplace():
    hits = deduplicate_hits(
        [
            SearchHit("https://shop.example/p", "A", "", "q1", "web", 1),
            SearchHit("https://shop.example/p", "A", "", "q2", "web", 2, "supplier-1"),
            SearchHit("https://ozon.ru/product/1", "Ozon", "", "q", "web", 3),
        ]
    )
    assert len(hits) == 1
    assert hits[0].supplier_id == "supplier-1"


def test_catalog_listing_is_not_created_as_a_supplier():
    supplier = supplier_from_hit(
        SearchHit(
            "https://meatinfo.ru/all/trade/meat/kura/file",
            "Кура, филе оптом купить/продать по цене от производителя",
            "",
            "филе куриное",
            "DuckDuckGo",
            1,
        )
    )

    assert supplier.company_type is CompanyType.MARKETPLACE
