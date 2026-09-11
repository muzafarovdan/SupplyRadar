"""Живой поиск организаций через 2ГИС Places API.

Провайдер отвечает только за обнаружение компаний и переносит в карточку
структурированные сведения, которые вернул API. Он не считает найденный сайт
проверенным: загрузка и разбор страниц выполняются отдельным следующим этапом.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from src.models import (
    CompanyType,
    Evidence,
    SearchRequest,
    SourceKind,
    SourceRecord,
    SourceStatus,
    Supplier,
)
from src.normalization import normalize_domain, normalize_supplier, normalize_url
from src.search.base import ProgressCallback, ProviderResult, SupplierProvider

logger = logging.getLogger(__name__)

_FIELDS = "items.adm_div,items.contact_groups,items.org,items.rubrics"

_RETAIL_MARKERS = (
    "рознич",
    "фирменный магазин",
    "гипермаркет",
    "магазин продуктов",
    "магазин у дома",
    "мясная лавка",
    "продуктовый магазин",
    "супермаркет",
)
_MANUFACTURER_MARKERS = ("производитель", "производство", "фабрика", "завод")
_DISTRIBUTOR_MARKERS = ("дистрибьютор", "дистрибуция")
_WHOLESALE_MARKERS = ("опт", "оптов", "поставщик", "склад")


class TwoGisSupplierProvider(SupplierProvider):
    """Находит карточки организаций в 2ГИС по сформированным запросам."""

    name = "2ГИС Places API"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://catalog.api.2gis.com/3.0",
        timeout_seconds: float = 15,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def collect(
        self,
        request: SearchRequest,
        queries: list[str],
        progress: ProgressCallback,
    ) -> ProviderResult:
        result = ProviderResult()
        seen_item_ids: set[str] = set()
        candidate_limit = max(request.max_results * 3, 10)

        try:
            for query in queries:
                progress(f"2ГИС: ищем «{query}»")
                search_url = self._public_search_url(query)
                try:
                    response = self._client.get(
                        f"{self._base_url}/items",
                        params={
                            "q": query,
                            "key": self._api_key,
                            "type": "branch",
                            "fields": _FIELDS,
                            "locale": "ru_RU",
                            "page_size": min(max(request.max_results, 10), 50),
                        },
                    )
                except httpx.HTTPError as error:
                    safe_error = self._safe_error(error)
                    logger.warning("Запрос к 2ГИС не выполнен: %s", safe_error)
                    result.pages_failed += 1
                    result.warnings.append(
                        f"2ГИС не ответил на запрос «{query}»: {safe_error}"
                    )
                    result.sources.append(
                        self._failed_source(search_url, query, safe_error)
                    )
                    if isinstance(
                        error,
                        (
                            httpx.ConnectError,
                            httpx.ConnectTimeout,
                            httpx.PoolTimeout,
                            httpx.ProxyError,
                        ),
                    ):
                        result.warnings.append(
                            "Остальные запросы к 2ГИС пропущены; продолжаем "
                            "через веб-поиск."
                        )
                        return result
                    continue

                if response.status_code in (401, 403):
                    return self._stop_for_api_error(
                        result,
                        search_url,
                        query,
                        response.status_code,
                        "ключ TWO_GIS_API_KEY отклонён или не имеет доступа",
                    )
                if response.status_code == 429:
                    return self._stop_for_api_error(
                        result,
                        search_url,
                        query,
                        response.status_code,
                        "лимит запросов 2ГИС исчерпан",
                    )
                if response.is_error:
                    result.pages_failed += 1
                    result.warnings.append(
                        f"2ГИС вернул HTTP {response.status_code} для запроса "
                        f"«{query}»; остальные запросы продолжены."
                    )
                    result.sources.append(
                        self._failed_source(
                            search_url,
                            query,
                            f"HTTP {response.status_code}",
                            response.status_code,
                        )
                    )
                    continue

                try:
                    payload = response.json()
                except ValueError:
                    result.pages_failed += 1
                    result.warnings.append(
                        f"2ГИС вернул некорректный JSON для запроса «{query}»."
                    )
                    result.sources.append(
                        self._failed_source(
                            search_url,
                            query,
                            "ответ API не является JSON",
                            response.status_code,
                        )
                    )
                    continue

                api_status = _api_status(payload)
                api_error = _api_error(payload)
                if api_error:
                    if api_status in (401, 403):
                        return self._stop_for_api_error(
                            result,
                            search_url,
                            query,
                            api_status,
                            "ключ TWO_GIS_API_KEY отклонён или не имеет доступа",
                        )
                    if api_status == 429:
                        return self._stop_for_api_error(
                            result,
                            search_url,
                            query,
                            api_status,
                            "лимит запросов 2ГИС исчерпан",
                        )
                    result.pages_failed += 1
                    result.warnings.append(
                        f"2ГИС не выполнил запрос «{query}»: {api_error}"
                    )
                    result.sources.append(
                        self._failed_source(
                            search_url, query, api_error, response.status_code
                        )
                    )
                    continue

                items = payload.get("result", {}).get("items", [])
                if not isinstance(items, list):
                    items = []

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("id") or "").strip()
                    if not item_id or item_id in seen_item_ids:
                        continue
                    try:
                        supplier, sources = self._supplier_from_item(item, query)
                    except (TypeError, ValueError) as error:
                        logger.warning(
                            "Некорректная карточка 2ГИС %s: %s", item_id, error
                        )
                        result.warnings.append(
                            f"Карточка 2ГИС {item_id} пропущена: некорректные данные."
                        )
                        continue
                    seen_item_ids.add(item_id)
                    result.suppliers.append(supplier)
                    result.sources.extend(sources)

                if len(result.suppliers) >= candidate_limit:
                    break
        finally:
            if self._owns_client:
                self._client.close()

        progress(
            f"2ГИС: получено {len(result.suppliers)} уникальных карточек "
            f"из {len(queries)} запросов"
        )
        return result

    def _supplier_from_item(
        self, item: dict[str, Any], query: str
    ) -> tuple[Supplier, list[SourceRecord]]:
        item_id = str(item["id"])
        name = str(item.get("name") or item.get("full_name") or "").strip()
        if not name:
            raise ValueError("нет названия")

        catalog_url = _catalog_url(item.get("link"), item_id)
        contacts = list(_contacts(item.get("contact_groups")))
        website = _first_contact(contacts, "website")
        phone = _first_contact(contacts, "phone")
        email = _first_contact(contacts, "email")
        categories = _names(item.get("rubrics"))
        regions = _names(item.get("adm_div"))
        address = _text(item.get("address_name"))

        supplier = normalize_supplier(
            Supplier(
                id=f"2gis-{_organization_id(item.get('org')) or item_id}",
                name=name,
                legal_name=_legal_name(item.get("org")),
                company_type=_company_type(name, categories),
                product_categories=categories,
                address=address,
                regions=regions,
                phone=phone,
                email=email,
                website=website,
                source_urls=_unique([catalog_url, website]),
                discovery_queries=[query],
                evidence=_evidence(
                    source_url=catalog_url,
                    name=name,
                    categories=categories,
                    regions=regions,
                    address=address,
                    phone=phone,
                    email=email,
                    website=website,
                ),
                checked_at=datetime.now(timezone.utc),
            )
        )

        sources = [
            SourceRecord(
                url=catalog_url,
                domain=normalize_domain(catalog_url) or "2gis.ru",
                status=SourceStatus.OK,
                http_status=200,
                note="карточка организации получена через Places API",
                supplier_name=name,
                discovery_query=query,
                provider=self.name,
                source_kind=SourceKind.ORGANIZATION_API,
            )
        ]
        if website and normalize_url(website) != normalize_url(catalog_url):
            sources.append(
                SourceRecord(
                    url=website,
                    domain=normalize_domain(website) or "",
                    status=SourceStatus.SKIPPED,
                    note="сайт найден в карточке 2ГИС; загрузка ещё не выполнялась",
                    supplier_name=name,
                    discovery_query=query,
                    provider=self.name,
                    source_kind=SourceKind.OFFICIAL_SITE,
                )
            )
        return supplier, sources

    def _public_search_url(self, query: str) -> str:
        return f"https://2gis.ru/search/{quote(query, safe='')}"

    def _safe_error(self, error: httpx.HTTPError) -> str:
        """Не допускает попадания API-ключа из URL исключения в логи и UI."""
        return str(error).replace(self._api_key, "***")

    def _failed_source(
        self,
        url: str,
        query: str,
        note: str,
        http_status: int | None = None,
    ) -> SourceRecord:
        return SourceRecord(
            url=url,
            domain="2gis.ru",
            status=SourceStatus.FAILED,
            http_status=http_status,
            note=note,
            discovery_query=query,
            provider=self.name,
            source_kind=SourceKind.ORGANIZATION_API,
        )

    def _stop_for_api_error(
        self,
        result: ProviderResult,
        url: str,
        query: str,
        status: int,
        message: str,
    ) -> ProviderResult:
        result.pages_failed += 1
        result.warnings.append(f"Живой поиск остановлен: {message} (HTTP {status}).")
        result.sources.append(self._failed_source(url, query, message, status))
        return result


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def _contacts(groups: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(groups, list):
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        contacts = group.get("contacts")
        if not isinstance(contacts, list):
            continue
        for contact in contacts:
            if isinstance(contact, dict):
                yield contact


def _first_contact(contacts: list[dict[str, Any]], contact_type: str) -> str | None:
    for contact in contacts:
        if contact.get("type") != contact_type:
            continue
        value = _text(contact.get("value")) or _text(contact.get("text"))
        if not value:
            continue
        if contact_type == "website" and "://" not in value:
            value = f"https://{value.lstrip('/')}"
        return value
    return None


def _legal_name(org: Any) -> str | None:
    if not isinstance(org, dict):
        return None
    return _text(org.get("name"))


def _organization_id(org: Any) -> str | None:
    if not isinstance(org, dict):
        return None
    return _text(org.get("id"))


def _company_type(name: str, categories: list[str]) -> CompanyType:
    text = " ".join((name, *categories)).casefold()
    if any(marker in text for marker in _RETAIL_MARKERS):
        return CompanyType.RETAILER
    if any(marker in text for marker in _MANUFACTURER_MARKERS):
        return CompanyType.MANUFACTURER
    if any(marker in text for marker in _DISTRIBUTOR_MARKERS):
        return CompanyType.DISTRIBUTOR
    if any(marker in text for marker in _WHOLESALE_MARKERS):
        return CompanyType.WHOLESALER
    return CompanyType.UNKNOWN


def _catalog_url(raw_link: Any, item_id: str) -> str:
    link = _text(raw_link)
    if not link:
        return f"https://2gis.ru/firm/{quote(item_id, safe='')}"
    if link.startswith("//"):
        return f"https:{link}"
    if link.startswith("/"):
        return f"https://2gis.ru{link}"
    if "://" not in link:
        return f"https://2gis.ru/{link.lstrip('/')}"
    return link


def _unique(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _evidence(
    *,
    source_url: str,
    name: str,
    categories: list[str],
    regions: list[str],
    address: str | None,
    phone: str | None,
    email: str | None,
    website: str | None,
) -> list[Evidence]:
    """Создаёт подтверждения только из буквальных значений ответа API."""
    values: list[tuple[str, str]] = [("name", name)]
    values.extend(("product_categories", value) for value in categories)
    values.extend(("regions", value) for value in regions)
    values.extend(
        (field, value)
        for field, value in (
            ("address", address),
            ("phone", phone),
            ("email", email),
            ("website", website),
        )
        if value
    )
    return [
        Evidence(
            field_name=field,
            value=value,
            source_url=source_url,
            quote=value,
            confidence=0.9,
        )
        for field, value in values
    ]


def _api_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "ответ API имеет неожиданную структуру"
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    code = _api_status(payload)
    if code in (None, 200):
        return None
    error = meta.get("error")
    if isinstance(error, dict):
        return _text(error.get("message")) or f"ошибка API {code}"
    return _text(error) or f"ошибка API {code}"


def _api_status(payload: Any) -> int | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        return None
    raw_code = payload["meta"].get("code")
    try:
        return int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        return None
