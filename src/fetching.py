"""Ограниченная асинхронная загрузка публичных HTML-страниц с кэшем."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from src.models import SourceKind, SourceRecord, SourceStatus
from src.normalization import normalize_domain, normalize_url
from src.storage import Storage
from src.web_search import is_catalog_url

_USEFUL_LINK_WORDS = (
    "catalog",
    "product",
    "produk",
    "assort",
    "opt",
    "dostav",
    "delivery",
    "contact",
    "sert",
    "document",
    "каталог",
    "продук",
    "опт",
    "достав",
    "контакт",
    "документ",
)
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_CAPTCHA_MARKERS = ("captcha", "подтвердите, что вы не робот", "проверка браузера")


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: SourceStatus
    http_status: int | None = None
    mime_type: str | None = None
    html: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def source_kind(self) -> SourceKind:
        return (
            SourceKind.CATALOG
            if is_catalog_url(self.final_url)
            else SourceKind.OFFICIAL_SITE
        )

    def source_record(
        self, supplier_name: str | None = None, provider: str = "HTTP"
    ) -> SourceRecord:
        return SourceRecord(
            url=self.final_url or self.url,
            domain=normalize_domain(self.final_url or self.url) or "",
            status=self.status,
            http_status=self.http_status,
            note=self.error,
            supplier_name=supplier_name,
            provider=provider,
            source_kind=self.source_kind,
        )


@dataclass
class FetchOutcome:
    pages: list[FetchedPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    deadline_reached: bool = False


class PageFetcher:
    """Загружает ограниченное число страниц, не выходя за общий дедлайн."""

    user_agent = "SupplierSearchMVP/1.0 (+public supplier research)"

    def __init__(
        self,
        *,
        storage: Storage | None,
        timeout_seconds: float = 8,
        deadline_seconds: float = 45,
        concurrency: int = 4,
        max_domains: int = 12,
        max_pages_per_domain: int = 3,
        max_response_bytes: int = 2_000_000,
        cache_ttl_hours: int = 168,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._storage = storage
        self._timeout = timeout_seconds
        self._deadline = deadline_seconds
        self._concurrency = concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._max_domains = max_domains
        self._max_per_domain = min(max_pages_per_domain, 3)
        self._max_bytes = max_response_bytes
        self._ttl = timedelta(hours=cache_ttl_hours)
        self._client = client
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._completed: list[FetchedPage] = []

    def bind_storage(self, storage: Storage | None) -> None:
        """Подключает хранилище, когда провайдер создаётся фабрикой пайплайна."""
        self._storage = storage

    async def fetch(
        self, urls: list[str], deadline_seconds: float | None = None
    ) -> FetchOutcome:
        selected = _select_initial_urls(urls, self._max_domains, self._max_per_domain)
        outcome = FetchOutcome()
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": self.user_agent},
        )
        self._client = client
        self._completed = []
        try:
            deadline = min(self._deadline, deadline_seconds or self._deadline)
            await asyncio.wait_for(self._fetch_plan(selected), timeout=deadline)
        except asyncio.TimeoutError:
            outcome.deadline_reached = True
            if self._completed:
                outcome.warnings.append(
                    "Основные страницы обработаны; дополнительный обход ссылок "
                    f"остановлен по лимиту {deadline:g} с."
                )
            else:
                outcome.warnings.append(
                    f"Проверка сайтов остановлена по общему лимиту {deadline:g} с."
                )
        finally:
            if owned:
                await client.aclose()
                self._client = None
        outcome.pages = list(self._completed)
        return outcome

    async def _fetch_plan(self, initial: list[str]) -> None:
        first_wave: list[FetchedPage] = []
        tasks = [asyncio.create_task(self._fetch_one(url)) for url in initial]
        for task in asyncio.as_completed(tasks):
            page = await task
            first_wave.append(page)
            self._completed.append(page)

        visited = {normalize_url(page.url) for page in first_wave}
        counts: dict[str, int] = {}
        for page in first_wave:
            domain = normalize_domain(page.final_url) or ""
            counts[domain] = counts.get(domain, 0) + 1
        extra: list[str] = []
        for page in first_wave:
            domain = normalize_domain(page.final_url) or ""
            for link in page.links:
                normalized = normalize_url(link)
                if not normalized or normalized in visited:
                    continue
                if normalize_domain(normalized) != domain:
                    continue
                if counts[domain] >= self._max_per_domain:
                    break
                visited.add(normalized)
                counts[domain] += 1
                extra.append(normalized)
        if extra:
            tasks = [asyncio.create_task(self._fetch_one(url)) for url in extra]
            for task in asyncio.as_completed(tasks):
                self._completed.append(await task)

    async def _fetch_one(self, url: str) -> FetchedPage:
        normalized = normalize_url(url) or url
        cached = self._storage.get_cached_page(normalized) if self._storage else None
        if cached:
            cached_status = _cached_status(cached)
            return FetchedPage(
                url=normalized,
                final_url=cached["final_url"],
                status=cached_status,
                http_status=cached["http_status"],
                mime_type=cached["mime_type"],
                html=cached["html"] or "",
                text=cached["cleaned_text"] or "",
                links=_useful_links(cached["html"] or "", cached["final_url"]),
                error=cached["error"],
            )

        assert self._semaphore is not None
        async with self._semaphore:
            if not await self._robots_allowed(normalized):
                return await self._finish(
                    normalized,
                    status=SourceStatus.SKIPPED,
                    error="загрузка запрещена robots.txt",
                )
            return await self._request_with_retry(normalized)

    async def _robots_allowed(self, url: str) -> bool:
        assert self._client is not None
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            robots_url = f"{origin}/robots.txt"
            try:
                response = await self._client.get(robots_url)
                if response.status_code == 200:
                    parser = RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(response.text.splitlines())
                    self._robots[origin] = parser
                else:
                    self._robots[origin] = None
            except httpx.HTTPError:
                self._robots[origin] = None
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(self.user_agent, url)

    async def _request_with_retry(self, url: str) -> FetchedPage:
        assert self._client is not None
        last_error = "неизвестная ошибка загрузки"
        for attempt in range(2):
            try:
                async with self._client.stream("GET", url) as response:
                    status = response.status_code
                    mime = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .lower()
                    )
                    if status in (401, 403):
                        return await self._finish(
                            url,
                            final_url=str(response.url),
                            status=SourceStatus.SKIPPED,
                            http_status=status,
                            mime_type=mime,
                            error="страница требует авторизацию или блокирует роботов",
                        )
                    if status == 429 or status >= 500:
                        last_error = f"HTTP {status}"
                        if attempt == 0:
                            await asyncio.sleep(0.5)
                            continue
                    if status >= 400:
                        return await self._finish(
                            url,
                            final_url=str(response.url),
                            status=SourceStatus.FAILED,
                            http_status=status,
                            mime_type=mime,
                            error=f"HTTP {status}",
                        )
                    if not any(mime.startswith(kind) for kind in _HTML_TYPES):
                        return await self._finish(
                            url,
                            final_url=str(response.url),
                            status=SourceStatus.SKIPPED,
                            http_status=status,
                            mime_type=mime,
                            error=f"неподдерживаемый MIME-тип: {mime or 'не указан'}",
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self._max_bytes:
                            return await self._finish(
                                url,
                                final_url=str(response.url),
                                status=SourceStatus.SKIPPED,
                                http_status=status,
                                mime_type=mime,
                                error="страница превышает допустимый размер",
                            )
                    html = bytes(body).decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                    text = clean_html(html)
                    visible = text.casefold()
                    captcha_detected = any(
                        marker in visible for marker in _CAPTCHA_MARKERS[1:]
                    ) or ("captcha" in visible and len(visible) < 5_000)
                    if captcha_detected:
                        return await self._finish(
                            url,
                            final_url=str(response.url),
                            status=SourceStatus.SKIPPED,
                            http_status=status,
                            mime_type=mime,
                            error="обнаружена CAPTCHA или проверка браузера",
                        )
                    return await self._finish(
                        url,
                        final_url=str(response.url),
                        status=SourceStatus.OK,
                        http_status=status,
                        mime_type=mime,
                        html=html,
                        text=text,
                    )
            except httpx.HTTPError as error:
                last_error = str(error)
                if attempt == 0:
                    await asyncio.sleep(0.5)
        return await self._finish(url, status=SourceStatus.FAILED, error=last_error)

    async def _finish(
        self,
        url: str,
        *,
        final_url: str | None = None,
        status: SourceStatus,
        http_status: int | None = None,
        mime_type: str | None = None,
        error: str | None = None,
        html: str = "",
        text: str = "",
    ) -> FetchedPage:
        final = normalize_url(final_url or url) or url
        page = FetchedPage(
            url=url,
            final_url=final,
            status=status,
            http_status=http_status,
            mime_type=mime_type,
            html=html,
            text=text,
            links=_useful_links(html, final),
            error=error,
        )
        if self._storage:
            fetched_at = datetime.now(timezone.utc)
            self._storage.save_cached_page(
                url=normalize_url(url) or url,
                final_url=final,
                http_status=http_status,
                mime_type=mime_type,
                fetched_at=fetched_at,
                expires_at=fetched_at
                + (
                    self._ttl
                    if status in {SourceStatus.OK, SourceStatus.CACHED}
                    else min(self._ttl, timedelta(hours=1))
                ),
                content_hash=hashlib.sha256(html.encode()).hexdigest()
                if html
                else None,
                html=html or None,
                cleaned_text=text or None,
                error=error,
            )
        return page


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()
    return "\n".join(
        line
        for line in (part.strip() for part in soup.get_text("\n").splitlines())
        if line
    )


def _useful_links(html: str, base_url: str) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    ranked: list[tuple[int, str]] = []
    for link in soup.find_all("a", href=True):
        target = normalize_url(urljoin(base_url, str(link["href"])))
        if not target or normalize_domain(target) != normalize_domain(base_url):
            continue
        haystack = f"{target} {link.get_text(' ', strip=True)}".casefold()
        score = sum(word in haystack for word in _USEFUL_LINK_WORDS)
        if score:
            ranked.append((score, target))
    ranked.sort(key=lambda item: (-item[0], len(item[1])))
    return list(dict.fromkeys(url for _, url in ranked))


def _select_initial_urls(
    urls: list[str], max_domains: int, max_per_domain: int
) -> list[str]:
    result: list[str] = []
    domains: set[str] = set()
    counts: dict[str, int] = {}
    for url in urls:
        normalized = normalize_url(url)
        domain = normalize_domain(normalized)
        if not normalized or not domain:
            continue
        if domain not in domains and len(domains) >= max_domains:
            continue
        if counts.get(domain, 0) >= max_per_domain:
            continue
        domains.add(domain)
        counts[domain] = counts.get(domain, 0) + 1
        if normalized not in result:
            result.append(normalized)
    return result


def _cached_status(cached: dict) -> SourceStatus:
    if cached["cleaned_text"]:
        return SourceStatus.CACHED
    error = (cached["error"] or "").casefold()
    if cached["http_status"] in (401, 403) or any(
        marker in error for marker in ("robots", "mime", "captcha", "размер")
    ):
        return SourceStatus.SKIPPED
    return SourceStatus.FAILED
