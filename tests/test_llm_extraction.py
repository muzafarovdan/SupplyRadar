"""LLM дополняет карточки только проверяемыми фактами из переданного текста."""

from __future__ import annotations

import asyncio
import json

import httpx

from src.fetching import FetchedPage
from src.llm_extraction import LlmExtractor
from src.models import SearchRequest, SourceStatus


def _payload(**overrides) -> dict:
    empty = {
        "matched_products": [],
        "legal_name": None,
        "inn": None,
        "company_type": None,
        "address": None,
        "regions": [],
        "minimum_order": None,
        "price": None,
        "delivery": None,
        "certificates": [],
        "phone": None,
        "email": None,
    }
    return {**empty, **overrides}


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(payload)}}]},
    )


def _page() -> FetchedPage:
    return FetchedPage(
        url="https://supplier.example/catalog",
        final_url="https://supplier.example/catalog",
        status=SourceStatus.OK,
        text=(
            "Куриная грудка без кожи для предприятий общепита.\n"
            "Цена 420 рублей за кг.\n"
            "Поставляем по Екатеринбургу."
        ),
    )


def test_llm_fills_semantic_product_and_rejects_invented_quote(make_supplier):
    page = _page()
    payload = _payload(
        matched_products=[
            {
                "value": "куриное филе (грудка)",
                "quote": "Куриная грудка без кожи для предприятий общепита.",
                "source_url": page.final_url,
            }
        ],
        price={
            "value": "420 рублей за кг",
            "quote": "Цена 420 рублей за кг.",
            "source_url": page.final_url,
        },
        email={
            "value": "sales@supplier.example",
            "quote": "sales@supplier.example",
            "source_url": page.final_url,
        },
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        return _response(payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            extractor = LlmExtractor(
                api_key="secret",
                model="test-model",
                base_url="https://api.example/v1",
                client=client,
            )
            return await extractor.enrich(
                [make_supplier(website="https://supplier.example")],
                {"supplier.example": [page]},
                SearchRequest(category="филе куриное", region="Екатеринбург"),
                deadline_seconds=5,
            )

    outcome = asyncio.run(run())
    supplier = outcome.suppliers[0]
    assert supplier.matched_products == ["куриное филе (грудка)"]
    assert supplier.price == "420 рублей за кг"
    assert supplier.email is None
    assert outcome.accepted_facts == 2
    assert outcome.rejected_facts == 1
    assert supplier.evidence_for("matched_products")[0].quote in page.text


def test_llm_retries_once_after_invalid_structured_response(make_supplier):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "not json"}}]}
            )
        return _response(_payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            extractor = LlmExtractor(
                api_key="secret",
                model="test-model",
                base_url="https://api.example/v1",
                client=client,
            )
            return await extractor.enrich(
                [make_supplier(website="https://supplier.example")],
                {"supplier.example": [_page()]},
                SearchRequest(category="филе куриное"),
                deadline_seconds=5,
            )

    outcome = asyncio.run(run())
    assert calls == 2
    assert outcome.succeeded == 1
    assert outcome.warnings == []


def test_llm_rejects_catalog_access_fee_as_product_price(make_supplier):
    page = FetchedPage(
        url="https://productcenter.ru/products/1/file",
        final_url="https://productcenter.ru/products/1/file",
        status=SourceStatus.OK,
        text=(
            "Филе куриное оптом.\n"
            "Цена доступа — 250 рублей, на 24 часа откроются все контакты."
        ),
    )
    payload = _payload(
        price={
            "value": "250 рублей",
            "quote": "Цена доступа — 250 рублей, на 24 часа откроются все контакты.",
            "source_url": page.final_url,
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return _response(payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            extractor = LlmExtractor(
                api_key="secret",
                model="test-model",
                base_url="https://api.example/v1",
                client=client,
            )
            return await extractor.enrich(
                [make_supplier(website="https://productcenter.ru")],
                {"productcenter.ru": [page]},
                SearchRequest(category="филе куриное"),
                deadline_seconds=5,
            )

    outcome = asyncio.run(run())
    assert outcome.suppliers[0].price is None
    assert outcome.rejected_facts == 1
