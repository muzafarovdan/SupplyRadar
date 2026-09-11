"""Составной веб-поиск официальных сайтов и отраслевых каталогов."""

from __future__ import annotations

import asyncio
import base64
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.normalization import normalize_domain, normalize_url

_BLOCKED_DOMAINS = frozenset(
    {
        "2gis.ru",
        "aliexpress.ru",
        "avito.ru",
        "barahla.net",
        "dzen.ru",
        "facebook.com",
        "instagram.com",
        "irr.ru",
        "market.yandex.ru",
        "ok.ru",
        "ozon.ru",
        "t.me",
        "telegram.me",
        "vk.com",
        "wildberries.ru",
        "youtube.com",
        "youla.ru",
        "edadeal.ru",
        "lenta.com",
        "magnit.ru",
        "online.metro-cc.ru",
        "perekrestok.ru",
        "pyaterochka.ru",
        "samokat.ru",
        "vprok.ru",
    }
)

_CATALOG_DOMAINS = frozenset(
    {
        "all.biz",
        "aboutpartner.ru",
        "agroserver.ru",
        "flagma.ru",
        "foodtender.ru",
        "gostpp.ru",
        "meatinfo.ru",
        "optomtovar.ru",
        "optkatalog.ru",
        "productcenter.ru",
        "pulscen.ru",
        "regtorg.ru",
        "satom.ru",
        "tiu.ru",
        "vsepostavshiki.ru",
        "yopt.org",
    }
)


@dataclass(frozen=True)
class SearchHit:
    """Одна ссылка из веб-поиска; сниппет не считается доказательством."""

    url: str
    title: str
    snippet: str
    query: str
    provider: str
    rank: int
    supplier_id: str | None = None
    related_urls: tuple[str, ...] = ()


@dataclass
class WebSearchOutcome:
    hits: list[SearchHit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_stats: dict[str, str] = field(default_factory=dict)
    fallback_used: bool = False


class WebSearchProvider(Protocol):
    name: str

    async def search(
        self, query: str, limit: int, supplier_id: str | None = None
    ) -> list[SearchHit]: ...


class BraveSearchProvider:
    name = "Brave Search"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client

    async def search(
        self, query: str, limit: int, supplier_id: str | None = None
    ) -> list[SearchHit]:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10, follow_redirects=True)
        try:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
                params={
                    "q": query,
                    "count": min(limit, 20),
                    "country": "ru",
                    "search_lang": "ru",
                    "safesearch": "moderate",
                },
            )
            response.raise_for_status()
            raw = response.json().get("web", {}).get("results", [])
            return _hits_from_dicts(raw, query, self.name, supplier_id)
        finally:
            if owned:
                await client.aclose()


class YandexSearchProvider:
    name = "Yandex Search API"

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._folder_id = folder_id
        self._client = client

    async def search(
        self, query: str, limit: int, supplier_id: str | None = None
    ) -> list[SearchHit]:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10, follow_redirects=True)
        try:
            response = await client.post(
                "https://searchapi.api.cloud.yandex.net/v2/web/search",
                headers={"Authorization": f"Api-Key {self._api_key}"},
                json={
                    "query": {
                        "searchType": "SEARCH_TYPE_RU",
                        "queryText": query,
                        "familyMode": "FAMILY_MODE_MODERATE",
                        "page": "0",
                        "fixTypoMode": "FIX_TYPO_MODE_ON",
                    },
                    "folderId": self._folder_id,
                    "responseFormat": "FORMAT_XML",
                    "userAgent": "supplier-search-mvp/1.0",
                },
            )
            response.raise_for_status()
            raw_data = response.json().get("rawData", "")
            xml = base64.b64decode(raw_data).decode("utf-8", errors="replace")
            return _parse_yandex_xml(xml, query, self.name, supplier_id, limit)
        finally:
            if owned:
                await client.aclose()


class DuckDuckGoSearchProvider:
    name = "DuckDuckGo"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def search(
        self, query: str, limit: int, supplier_id: str | None = None
    ) -> list[SearchHit]:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 supplier-search-mvp/1.0"},
        )
        try:
            response = await client.post(
                "https://html.duckduckgo.com/html/", data={"q": query, "kl": "ru-ru"}
            )
            response.raise_for_status()
            return _parse_duckduckgo(
                response.text, query, self.name, supplier_id, limit
            )
        finally:
            if owned:
                await client.aclose()


class ProductCenterSearchProvider:
    """Ищет товарные карточки в открытом B2B-каталоге российских производителей."""

    name = "ProductCenter"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def search(
        self, query: str, limit: int, supplier_id: str | None = None
    ) -> list[SearchHit]:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "SupplierSearchMVP/1.0 (B2B product research)"},
        )
        try:
            response = await client.get(
                "https://productcenter.ru/search/", params={"q": query}
            )
            response.raise_for_status()
            return _parse_productcenter(
                response.text, query, self.name, supplier_id, limit
            )
        finally:
            if owned:
                await client.aclose()


class CompositeWebSearch:
    """Использует официальный API и дополняет его DuckDuckGo при необходимости."""

    def __init__(
        self,
        primary: WebSearchProvider | None,
        fallback: WebSearchProvider | None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    async def search(
        self,
        queries: list[tuple[str, str | None]],
        target: int,
    ) -> WebSearchOutcome:
        outcome = WebSearchOutcome()
        if self._primary:
            await self._run_provider(self._primary, queries, target, outcome)
        if self._fallback and (not self._primary or len(outcome.hits) < target):
            outcome.fallback_used = self._primary is not None
            await self._run_provider(self._fallback, queries, target, outcome)
        unique = deduplicate_hits(outcome.hits)
        unique.sort(key=lambda hit: (hit.supplier_id is None, hit.rank))
        outcome.hits = unique[: max(target, 1)]
        return outcome

    async def _run_provider(
        self,
        provider: WebSearchProvider,
        queries: list[tuple[str, str | None]],
        target: int,
        outcome: WebSearchOutcome,
    ) -> None:
        semaphore = asyncio.Semaphore(2)

        async def one(query: str, supplier_id: str | None) -> list[SearchHit]:
            async with semaphore:
                return await provider.search(query, min(target, 10), supplier_id)

        results = await asyncio.gather(
            *(one(query, supplier_id) for query, supplier_id in queries),
            return_exceptions=True,
        )
        found = 0
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                if isinstance(result, httpx.HTTPStatusError):
                    status = result.response.status_code
                    if status in (401, 403):
                        outcome.warnings.append(
                            f"{provider.name}: ключ отклонён или не имеет доступа."
                        )
                    elif status == 429:
                        outcome.warnings.append(
                            f"{provider.name}: лимит поисковых запросов исчерпан."
                        )
                continue
            found += len(result)
            outcome.hits.extend(result)
        if failures:
            outcome.warnings.append(
                f"{provider.name}: не выполнено запросов {failures} из {len(queries)}."
            )
        outcome.provider_stats[provider.name] = f"ссылок {found}, ошибок {failures}"


def create_web_search(settings: Settings) -> CompositeWebSearch:
    primary: WebSearchProvider | None = None
    if settings.web_search_provider == "yandex":
        primary = YandexSearchProvider(
            settings.yandex_search_api_key or "", settings.yandex_folder_id or ""
        )
    elif settings.web_search_provider == "brave":
        primary = BraveSearchProvider(settings.brave_search_api_key or "")

    fallback = (
        DuckDuckGoSearchProvider() if settings.duckduckgo_fallback_enabled else None
    )
    return CompositeWebSearch(primary, fallback)


def deduplicate_hits(hits: list[SearchHit]) -> list[SearchHit]:
    positions: dict[str, int] = {}
    result: list[SearchHit] = []
    for hit in hits:
        url = normalize_url(hit.url)
        if not url or is_blocked_url(url):
            continue
        normalized_hit = SearchHit(
            url=url,
            title=hit.title,
            snippet=hit.snippet,
            query=hit.query,
            provider=hit.provider,
            rank=hit.rank,
            supplier_id=hit.supplier_id,
            related_urls=tuple(
                related
                for value in hit.related_urls
                if (related := normalize_url(value))
                and not is_blocked_url(related)
                and related != url
            ),
        )
        if url in positions:
            index = positions[url]
            if not result[index].supplier_id and hit.supplier_id:
                result[index] = normalized_hit
            continue
        positions[url] = len(result)
        result.append(normalized_hit)
    return result


def is_blocked_url(url: str) -> bool:
    domain = normalize_domain(url) or ""
    return any(
        domain == item or domain.endswith(f".{item}") for item in _BLOCKED_DOMAINS
    )


def is_catalog_url(url: str) -> bool:
    domain = normalize_domain(url) or ""
    return any(
        domain == item or domain.endswith(f".{item}") for item in _CATALOG_DOMAINS
    )


def _hits_from_dicts(
    raw: object, query: str, provider: str, supplier_id: str | None
) -> list[SearchHit]:
    if not isinstance(raw, list):
        return []
    hits: list[SearchHit] = []
    for rank, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        hits.append(
            SearchHit(
                url=str(item["url"]),
                title=_plain(str(item.get("title") or "")),
                snippet=_plain(str(item.get("description") or "")),
                query=query,
                provider=provider,
                rank=rank,
                supplier_id=supplier_id,
            )
        )
    return deduplicate_hits(hits)


def _parse_duckduckgo(
    body: str,
    query: str,
    provider: str,
    supplier_id: str | None,
    limit: int,
) -> list[SearchHit]:
    soup = BeautifulSoup(body, "html.parser")
    hits: list[SearchHit] = []
    for result in soup.select(".result"):
        link = result.select_one("a.result__a")
        if link is None or not link.get("href"):
            continue
        url = _duckduckgo_target(str(link["href"]))
        snippet = result.select_one(".result__snippet")
        hits.append(
            SearchHit(
                url=url,
                title=link.get_text(" ", strip=True),
                snippet=snippet.get_text(" ", strip=True) if snippet else "",
                query=query,
                provider=provider,
                rank=len(hits) + 1,
                supplier_id=supplier_id,
            )
        )
        if len(hits) >= limit:
            break
    return deduplicate_hits(hits)


def _parse_productcenter(
    body: str,
    query: str,
    provider: str,
    supplier_id: str | None,
    limit: int,
) -> list[SearchHit]:
    soup = BeautifulSoup(body, "html.parser")
    hits: list[SearchHit] = []
    for card in soup.select(".card_item.product"):
        link = card.select_one('a.link[href^="/products/"]')
        if link is None or not link.get("href"):
            continue
        title = link.get_text(" ", strip=True)
        if not _title_matches_query(title, query):
            continue
        if _conflicts_with_query(title, query):
            continue
        company = card.select_one(".ii_company")
        company_name = ""
        related_urls: tuple[str, ...] = ()
        if company:
            company_name = str(company.get("title") or "").split(":", 1)[-1].strip()
            company_link = company.select_one('a[href^="/producers/"]')
            if company_link is None and company.name == "a":
                company_link = company
            if company_link is not None and company_link.get("href"):
                profile_path = str(company_link["href"])
                producer_button = card.select_one("[data-producer-id]")
                producer_id = (
                    str(producer_button.get("data-producer-id") or "")
                    if producer_button
                    else ""
                )
                if producer_id:
                    profile_path = re.sub(
                        r"^/producers/\d+/", f"/producers/{producer_id}/", profile_path
                    )
                related_urls = (urljoin("https://productcenter.ru", profile_path),)
        descriptor = card.select_one(".item_descriptor")
        snippet = " — ".join(
            value
            for value in (
                title,
                descriptor.get_text(" ", strip=True) if descriptor else "",
            )
            if value
        )
        hits.append(
            SearchHit(
                url=urljoin("https://productcenter.ru", str(link["href"])),
                title=company_name or title,
                snippet=snippet,
                query=query,
                provider=provider,
                rank=len(hits) + 1,
                supplier_id=supplier_id,
                related_urls=related_urls,
            )
        )
        if len(hits) >= max(limit * 3, 30):
            break
    hits.sort(key=lambda hit: (-_productcenter_score(hit, query), hit.rank))
    reranked = [
        SearchHit(
            url=hit.url,
            title=hit.title,
            snippet=hit.snippet,
            query=hit.query,
            provider=hit.provider,
            rank=rank,
            supplier_id=hit.supplier_id,
            related_urls=hit.related_urls,
        )
        for rank, hit in enumerate(hits[:limit], start=1)
    ]
    return deduplicate_hits(reranked)


def _productcenter_score(hit: SearchHit, query: str) -> int:
    text = f"{hit.title} {hit.snippet}".casefold()
    title = hit.snippet.split("—", 1)[0].casefold()
    query_text = query.casefold()
    score = sum(
        marker in text
        for marker in (
            "опт",
            "производител",
            "поставщик",
            "гост",
            "заморож",
            "охлажд",
            "доставка",
            "прайс",
        )
    )
    prepared_markers = (
        "гречк",
        "пельмен",
        "пельмеш",
        "сэндвич",
        "вялен",
        "снек",
        "жульен",
        "котлет",
        "рулет",
        "запеч",
        "жарен",
        "салат",
        "шаурм",
    )
    score -= 3 * sum(
        marker in title and marker not in query_text for marker in prepared_markers
    )
    return score


def _conflicts_with_query(title: str, query: str) -> bool:
    """Отбрасывает готовое блюдо, когда искали сырьевой продукт."""
    prepared_markers = (
        "гречк",
        "пельмен",
        "сэндвич",
        "вялен",
        "снек",
        "жульен",
        "котлет",
        "рулет",
        "запеч",
        "жарен",
        "салат",
        "шаурм",
    )
    lowered_title = title.casefold()
    lowered_query = query.casefold()
    return any(
        marker in lowered_title and marker not in lowered_query
        for marker in prepared_markers
    )


def _title_matches_query(title: str, query: str) -> bool:
    ignored = {"купить", "оптом", "поставщик", "производитель", "цена"}
    wanted = [
        word
        for word in re.findall(r"[а-яёa-z0-9-]{3,}", query.casefold())
        if word not in ignored
    ]
    actual = re.findall(r"[а-яёa-z0-9-]{3,}", title.casefold())
    return bool(wanted) and all(
        any(
            left == right
            or (
                min(len(left), len(right)) >= 4
                and left[: min(5, len(left), len(right))]
                == right[: min(5, len(left), len(right))]
            )
            for right in actual
        )
        for left in wanted
    )


def _duckduckgo_target(url: str) -> str:
    parsed = urlparse(url)
    target = parse_qs(parsed.query).get("uddg", [None])[0]
    return unquote(target) if target else url


def _parse_yandex_xml(
    xml: str,
    query: str,
    provider: str,
    supplier_id: str | None,
    limit: int,
) -> list[SearchHit]:
    root = ET.fromstring(xml)
    hits: list[SearchHit] = []
    for document in root.findall(".//doc"):
        url = document.findtext("url")
        if not url:
            continue
        passages = " ".join(node.text or "" for node in document.findall(".//passage"))
        hits.append(
            SearchHit(
                url=url,
                title=_plain(document.findtext("title") or ""),
                snippet=_plain(passages),
                query=query,
                provider=provider,
                rank=len(hits) + 1,
                supplier_id=supplier_id,
            )
        )
        if len(hits) >= limit:
            break
    return deduplicate_hits(hits)


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()
