"""Контрактные тесты API «Госзатраты» и поиска ProductCenter."""

from __future__ import annotations

import asyncio

import httpx

from src.models import SearchRequest, SourceKind
from src.search.clear_spending import ClearSpendingProvider
from src.web_search import ProductCenterSearchProvider


def test_clear_spending_returns_real_supplier_history():
    seen_regions: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_regions.append(request.url.params.get("customerregion"))
        return httpx.Response(
            200,
            json={
                "contracts": {
                    "total": 1,
                    "data": [
                        {
                            "regNum": "2664600189924000003",
                            "signDate": "2024-04-02T00:00:00",
                            "regionCode": "66",
                            "contractUrl": "http://zakupki.gov.ru/contract?id=1",
                            "customer": {"postalAddress": "Свердловская область"},
                            "products": [{"name": "Филе грудки куриное замороженное"}],
                            "suppliers": [
                                {
                                    "organizationName": 'ООО "ВЫБОР"',
                                    "inn": "6658547883",
                                    "factualAddress": "Екатеринбург, ул. Ясная, 6",
                                    "contactInfo": {"email": "opt@example.ru"},
                                }
                            ],
                        }
                    ],
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ClearSpendingProvider(
        base_url="https://openapi.example/contracts/", client=client
    )
    result = provider.collect(
        SearchRequest(category="филе куриное", region="Екатеринбург"),
        [],
        lambda _: None,
    )
    asyncio.run(client.aclose())

    assert seen_regions == ["66", None]
    assert len(result.suppliers) == 1
    supplier = result.suppliers[0]
    assert supplier.name == 'ООО "ВЫБОР"'
    assert supplier.inn == "6658547883"
    assert supplier.regions == ["Екатеринбург"]
    assert supplier.contract_products == ["Филе грудки куриное замороженное"]
    assert supplier.matched_products == []
    assert supplier.evidence_for("contract_products")
    assert all(
        source.source_kind is SourceKind.GOVERNMENT_CONTRACT
        for source in result.sources
    )


def test_productcenter_parses_only_matching_product_cards():
    body = """
    <div class="card_item product"><div class="text">
      <a class="link" href="/products/1/file-kurinoe">Филе куриное охлаждённое</a>
      <div class="item_descriptor">Поставка оптом</div>
      <div class="ii_company" title="Производитель:ООО Птица">
        <a href="/producers/1/ptitsa">ООО Птица</a>
      </div>
      <button data-producer-id="42"></button>
    </div></div>
    <div class="card_item product"><div class="text">
      <a class="link" href="/products/2/kotlety">Котлеты куриные</a>
      <div class="ii_company" title="Производитель:ООО Котлета"></div>
    </div></div>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "филе куриное"
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ProductCenterSearchProvider(client)
    hits = asyncio.run(provider.search("филе куриное", 10))
    asyncio.run(client.aclose())

    assert len(hits) == 1
    assert hits[0].title == "ООО Птица"
    assert hits[0].url == "https://productcenter.ru/products/1/file-kurinoe"
    assert hits[0].related_urls == ("https://productcenter.ru/producers/42/ptitsa",)
