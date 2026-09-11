"""Контрактные тесты публичной RSS-ленты и карточек ЕИС."""

from __future__ import annotations

import asyncio

import httpx

from src.config import Settings
from src.models import SearchRequest, SourceKind
from src.scoring import SupplierScorer
from src.search.eis import EisContractProvider, build_eis_queries

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><item>
  <title>№ 3090200765326000012</title>
  <link>/epz/contract/contractCard/common-info.html?reestrNumber=3090200765326000012</link>
  <description><![CDATA[Номер реестровой записи контракта: 3090200765326000012]]></description>
</item></channel></rss>""".encode()

COMMON_HTML = """
<html><body>
<div>Предмет контракта</div><div>Поставка продуктов питания</div>
<div>Место поставки товара, выполнения работы или оказания услуги</div>
<div>Российская Федерация, Свердловская область, г. Екатеринбург</div>
<div>Информация о поставщиках</div>
<div>Организация</div><div>Страна, код</div><div>Адрес места нахождения</div>
<div>Почтовый адрес</div><div>Телефон, электронная почта</div><div>Статус</div>
<div>ООО «Морозко»</div><div>Юридическое лицо</div>
<div>ИНН:</div><div>6671000000</div>
<div>+7 (343) 123-45-67</div><div>opt@morozko.example</div>
<div>Обеспечение исполнения контракта</div>
</body></html>
"""

PRODUCT_HTML = """
<html><body><h1>Объекты закупки</h1>
<div>Филе куриное охлажденное, без кожи и кости, 500 кг</div>
</body></html>
"""


def test_default_and_explicit_eis_queries():
    automatic = build_eis_queries(SearchRequest(category="филе куриное"))
    explicit = build_eis_queries(
        SearchRequest(
            category="филе куриное",
            eis_queries=["грудка куриная", "10.12.20.110"],
        )
    )

    assert automatic == [
        "филе куриное",
        "куриное филе",
        "филе грудки куриной",
        "грудка куриная без кости",
        "филе цыпленка-бройлера",
        "10.12.20.110",
    ]
    assert explicit == ["грудка куриная", "10.12.20.110"]
    regional = build_eis_queries(
        SearchRequest(category="филе куриное", region="Екатеринбург")
    )
    assert regional == [
        "филе куриное Екатеринбург",
        "филе куриное",
        "филе грудки куриной",
        "грудка куриная без кости",
        "филе цыпленка-бройлера",
        "10.12.20.110",
    ]
    assert Settings(_env_file=None, eis_ca_bundle="").eis_ca_bundle is None


def test_eis_contract_becomes_candidate_with_history():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/rss"):
            assert request.url.params["searchString"] == "филе куриное Екатеринбург"
            return httpx.Response(200, content=RSS)
        if request.url.path.endswith("common-info.html"):
            return httpx.Response(200, text=COMMON_HTML)
        if request.url.path.endswith("payment-info-and-target-of-order.html"):
            return httpx.Response(200, text=PRODUCT_HTML)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EisContractProvider(
        base_url="https://zakupki.example",
        max_queries=1,
        client=client,
    )
    request = SearchRequest(category="филе куриное", region="Екатеринбург")

    result = provider.collect(request, [], lambda message: None)
    asyncio.run(client.aclose())

    assert len(result.suppliers) == 1
    supplier = result.suppliers[0]
    assert supplier.name == "ООО «Морозко»"
    assert supplier.inn == "6671000000"
    assert supplier.phone == "+73431234567"
    assert supplier.email == "opt@morozko.example"
    assert supplier.regions == ["Екатеринбург"]
    assert supplier.matched_products == []
    assert supplier.contract_products == [
        "Филе куриное охлажденное, без кожи и кости, 500 кг"
    ]
    assert supplier.evidence_for("contract_products")
    assert all(
        source.source_kind is SourceKind.GOVERNMENT_CONTRACT
        for source in result.sources
    )

    scored = SupplierScorer().score_candidate(supplier, request)
    assert "история контрактов" in (scored.verification_reason or "")
    assert "актуальное наличие" in (scored.verification_reason or "")


def test_eis_failure_does_not_raise():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("ЕИС не отвечает", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EisContractProvider(
        base_url="https://zakupki.example",
        max_queries=1,
        client=client,
    )

    result = provider.collect(
        SearchRequest(category="масло сливочное"), [], lambda message: None
    )
    asyncio.run(client.aclose())

    assert result.suppliers == []
    assert result.pages_failed == 1
    assert result.provider_stats["ЕИС"].endswith("ошибок 1")


def test_eis_retries_temporary_rss_failure():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/search/rss"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, text="Backend fetch failed")
            return httpx.Response(200, content=b"<rss><channel /></rss>")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EisContractProvider(
        base_url="https://zakupki.example",
        max_queries=1,
        client=client,
    )

    result = provider.collect(
        SearchRequest(category="масло сливочное"), [], lambda message: None
    )
    asyncio.run(client.aclose())

    assert attempts == 2
    assert result.pages_failed == 0
    assert result.provider_stats["ЕИС"].endswith("ошибок 0")
