"""Проверки ограниченного загрузчика страниц и SQLite-кэша."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from src.fetching import PageFetcher
from src.models import SourceStatus
from src.storage import Storage


def test_fetches_html_follows_useful_link_and_uses_cache(tmp_path):
    calls: list[str] = []
    html = '<html><body>Главная <a href="/catalog">Каталог продукции</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    storage = Storage(tmp_path / "cache.db")

    async def first_run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = PageFetcher(
                storage=storage, client=client, max_domains=2, max_pages_per_domain=2
            )
            return await fetcher.fetch(["https://supplier.example"])

    first = asyncio.run(first_run())
    assert len(first.pages) == 2
    assert all(page.status is SourceStatus.OK for page in first.pages)
    assert "/catalog" in calls

    async def second_run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError(request.url))
            )
        ) as client:
            fetcher = PageFetcher(
                storage=storage, client=client, max_domains=2, max_pages_per_domain=2
            )
            return await fetcher.fetch(["https://supplier.example"])

    second = asyncio.run(second_run())
    assert second.pages[0].status is SourceStatus.CACHED


def test_respects_robots_txt(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = PageFetcher(storage=Storage(tmp_path / "cache.db"), client=client)
            return await fetcher.fetch(["https://supplier.example/private"])

    outcome = asyncio.run(scenario())
    assert outcome.pages[0].status is SourceStatus.SKIPPED
    assert "robots.txt" in (outcome.pages[0].error or "")


def test_skips_unsupported_mime_type(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"pdf"
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await PageFetcher(
                storage=Storage(tmp_path / "cache.db"), client=client
            ).fetch(["https://supplier.example/file.pdf"])

    outcome = asyncio.run(scenario())
    assert outcome.pages[0].status is SourceStatus.SKIPPED
    assert "MIME" in (outcome.pages[0].error or "")


def test_retries_temporary_server_error(tmp_path):
    page_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_calls
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        page_calls += 1
        if page_calls == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>готово</p>"
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await PageFetcher(
                storage=Storage(tmp_path / "cache.db"), client=client
            ).fetch(["https://supplier.example"])

    outcome = asyncio.run(scenario())
    assert page_calls == 2
    assert outcome.pages[0].status is SourceStatus.OK


def test_stops_reading_oversized_response(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"x" * 100
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await PageFetcher(
                storage=Storage(tmp_path / "cache.db"),
                client=client,
                max_response_bytes=20,
            ).fetch(["https://supplier.example"])

    outcome = asyncio.run(scenario())
    assert outcome.pages[0].status is SourceStatus.SKIPPED
    assert "размер" in (outcome.pages[0].error or "")


def test_expired_cache_entry_is_not_returned(tmp_path):
    storage = Storage(tmp_path / "cache.db")
    now = datetime.now(timezone.utc)
    storage.save_cached_page(
        url="https://supplier.example",
        final_url="https://supplier.example",
        http_status=200,
        mime_type="text/html",
        fetched_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
        content_hash="hash",
        html="<p>old</p>",
        cleaned_text="old",
        error=None,
    )
    assert storage.get_cached_page("https://supplier.example") is None


def test_global_deadline_keeps_pages_completed_before_timeout(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "slow.example":
            await asyncio.sleep(1)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<p>страница</p>"
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await PageFetcher(
                storage=Storage(tmp_path / "cache.db"), client=client, concurrency=2
            ).fetch(
                ["https://fast.example", "https://slow.example"],
                deadline_seconds=0.05,
            )

    outcome = asyncio.run(scenario())
    assert outcome.deadline_reached
    assert [page.final_url for page in outcome.pages] == ["https://fast.example"]
