"""Поиск поставщиков по публичному реестру контрактов ЕИС."""

from __future__ import annotations

import asyncio
import re
import ssl
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from src.fetching import clean_html
from src.models import (
    Evidence,
    SearchRequest,
    SourceKind,
    SourceRecord,
    SourceStatus,
    Supplier,
)
from src.normalization import normalize_email, normalize_phone, normalize_supplier
from src.search.base import ProgressCallback, ProviderResult, SupplierProvider

_RSS_PATH = "/epz/contract/search/rss"
_CARD_PATH = "/epz/contract/contractCard/common-info.html"
_PRODUCT_PATH = "/epz/contract/contractCard/payment-info-and-target-of-order.html"
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}")
_PHONE = re.compile(r"(?:\+?7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}")
_INN = re.compile(r"\b(?:\d{10}|\d{12})\b")
_WORDS = re.compile(r"[а-яёa-z0-9-]{3,}", re.IGNORECASE)
_PRODUCT_STOP = {"купить", "поставка", "поставку", "оптом", "цена", "окпд2"}
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}

CHICKEN_FILLET_QUERIES = (
    "филе куриное",
    "куриное филе",
    "филе грудки куриной",
    "грудка куриная без кости",
    "филе цыпленка-бройлера",
    "10.12.20.110",
)


@dataclass(frozen=True)
class EisRssItem:
    query: str
    url: str
    registry_number: str
    description: str


def build_eis_queries(request: SearchRequest, limit: int = 6) -> list[str]:
    """Строит короткий прозрачный набор запросов к реестру контрактов."""
    if request.eis_queries:
        values = request.eis_queries
    else:
        category = " ".join(request.category.split())
        lowered = category.casefold()
        if "фил" in lowered and ("кур" in lowered or "цып" in lowered):
            if request.region:
                values = [
                    f"{category} {request.region.strip()}",
                    category,
                    *CHICKEN_FILLET_QUERIES[2:],
                ]
            else:
                values = list(CHICKEN_FILLET_QUERIES)
        else:
            values = [
                *([f"{category} {request.region.strip()}"] if request.region else []),
                category,
            ]
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))[
        :limit
    ]


class EisContractProvider(SupplierProvider):
    """Читает RSS ЕИС и карточки контрактов, не считая историю наличием."""

    name = "ЕИС"

    def __init__(
        self,
        *,
        base_url: str = "https://zakupki.gov.ru",
        timeout_seconds: float = 8,
        deadline_seconds: float = 12,
        max_queries: int = 6,
        max_contracts: int = 8,
        ca_bundle: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._deadline = deadline_seconds
        self._max_queries = max_queries
        self._max_contracts = max_contracts
        self._ca_bundle = ca_bundle
        self._client = client

    def collect(
        self,
        request: SearchRequest,
        queries: list[str],
        progress: ProgressCallback,
    ) -> ProviderResult:
        del queries
        eis_queries = build_eis_queries(request, self._max_queries)
        if not eis_queries:
            return ProviderResult(provider_stats={"ЕИС": "запросы не сформированы"})
        progress("ЕИС: " + " | ".join(eis_queries))
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._collect_async(request, eis_queries),
                    timeout=self._deadline,
                )
            )
        except asyncio.TimeoutError:
            return ProviderResult(
                warnings=[
                    f"ЕИС не ответила за {self._deadline:g} с; остальные источники продолжены."
                ],
                provider_stats={"ЕИС": "таймаут"},
                pages_failed=1,
            )
        except (httpx.HTTPError, ET.ParseError, OSError) as error:
            note = _error_message(error, self._ca_bundle)
            return ProviderResult(
                warnings=[f"ЕИС недоступна; остальные источники продолжены: {note}"],
                provider_stats={"ЕИС": "недоступна"},
                pages_failed=1,
            )

    async def _collect_async(
        self, request: SearchRequest, queries: list[str]
    ) -> ProviderResult:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            verify=_ssl_context(self._ca_bundle),
            headers={"User-Agent": "SupplierSearchMVP/1.0 (+public contract research)"},
        )
        try:
            rss_results = await asyncio.gather(
                *(self._fetch_rss(client, query) for query in queries),
                return_exceptions=True,
            )
            result = ProviderResult()
            items: list[EisRssItem] = []
            failure_notes: list[str] = []
            for query, rss_result in zip(queries, rss_results):
                rss_url = self._rss_url(query)
                if isinstance(rss_result, BaseException):
                    result.pages_failed += 1
                    failure_notes.append(_error_message(rss_result, self._ca_bundle))
                    result.sources.append(
                        _source(
                            rss_url,
                            SourceStatus.FAILED,
                            query,
                            note=_error_message(rss_result, self._ca_bundle),
                        )
                    )
                    continue
                items.extend(rss_result)
                result.sources.append(
                    _source(
                        rss_url,
                        SourceStatus.OK,
                        query,
                        note=f"контрактов в RSS: {len(rss_result)}",
                        http_status=200,
                    )
                )

            selected = _deduplicate_items(items)[: self._max_contracts]
            semaphore = asyncio.Semaphore(3)

            async def fetch(item: EisRssItem):
                async with semaphore:
                    return await self._fetch_contract(client, item, request)

            contracts = await asyncio.gather(
                *(fetch(item) for item in selected), return_exceptions=True
            )
            for item, contract in zip(selected, contracts):
                if isinstance(contract, BaseException):
                    result.pages_failed += 1
                    result.sources.append(
                        _source(
                            item.url,
                            SourceStatus.FAILED,
                            item.query,
                            note=_error_message(contract, self._ca_bundle),
                        )
                    )
                    continue
                supplier, sources = contract
                result.sources.extend(sources)
                result.pages_failed += sum(
                    source.status is SourceStatus.FAILED for source in sources
                )
                if supplier is not None:
                    result.suppliers.append(supplier)

            result.provider_stats["ЕИС"] = (
                f"запросов {len(queries)}, контрактов {len(selected)}, "
                f"поставщиков {len(result.suppliers)}, ошибок {result.pages_failed}"
            )
            if failure_notes:
                result.warnings.append(
                    f"ЕИС: не выполнено запросов {len(failure_notes)} из "
                    f"{len(queries)}. {failure_notes[0]}"
                )
            if not selected and result.pages_failed:
                result.warnings.append(
                    "ЕИС не вернула доступных контрактов; поиск продолжен по другим источникам."
                )
            return result
        finally:
            if owned:
                await client.aclose()

    async def _fetch_rss(
        self, client: httpx.AsyncClient, query: str
    ) -> list[EisRssItem]:
        response = await _get_with_retry(
            client, f"{self._base_url}{_RSS_PATH}", params=_rss_params(query)
        )
        response.raise_for_status()
        return _parse_rss(response.content, query, self._base_url)

    async def _fetch_contract(
        self,
        client: httpx.AsyncClient,
        item: EisRssItem,
        request: SearchRequest,
    ) -> tuple[Supplier | None, list[SourceRecord]]:
        registry_number = item.registry_number
        common_url = urljoin(self._base_url, item.url)
        product_url = (
            f"{self._base_url}{_PRODUCT_PATH}?"
            f"{urlencode({'reestrNumber': registry_number})}#contractSubjects"
        )
        common_response, product_response = await asyncio.gather(
            _get_with_retry(client, common_url),
            _get_with_retry(client, product_url),
            return_exceptions=True,
        )
        if isinstance(common_response, BaseException):
            raise common_response
        common_response.raise_for_status()
        common_text = clean_html(common_response.text)
        product_text = ""
        product_status = SourceStatus.FAILED
        product_http: int | None = None
        product_note: str | None = None
        if isinstance(product_response, BaseException):
            product_note = _error_message(product_response, self._ca_bundle)
        else:
            product_http = product_response.status_code
            if product_response.is_success:
                product_status = SourceStatus.OK
                product_text = clean_html(product_response.text)
            else:
                product_note = f"HTTP {product_response.status_code}"

        supplier = _supplier_from_contract(
            common_url=common_url,
            common_text=common_text,
            product_url=product_url,
            product_text=product_text,
            item=item,
            request=request,
        )
        name = supplier.name if supplier else None
        sources = [
            _source(
                common_url,
                SourceStatus.OK,
                item.query,
                supplier_name=name,
                http_status=common_response.status_code,
                note=f"контракт № {registry_number}",
            ),
            _source(
                product_url,
                product_status,
                item.query,
                supplier_name=name,
                http_status=product_http,
                note=product_note or f"товарные позиции контракта № {registry_number}",
            ),
        ]
        return supplier, sources

    def _rss_url(self, query: str) -> str:
        return f"{self._base_url}{_RSS_PATH}?{urlencode(_rss_params(query))}"


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Один раз повторяет временный сетевой сбой или ответ 429/5xx."""
    for attempt in range(2):
        try:
            response = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt:
                raise
        else:
            if response.status_code not in _RETRYABLE_HTTP_STATUSES or attempt:
                return response
        await asyncio.sleep(0.25)
    raise RuntimeError("недостижимая ветка повтора запроса ЕИС")


def _rss_params(query: str) -> dict[str, str]:
    return {
        "searchString": query,
        "morphology": "on",
        "fz44": "on",
        "contractStageList_1": "on",
        "contractStageList_2": "on",
        "sortBy": "UPDATE_DATE",
        "pageNumber": "1",
        "sortDirection": "false",
        "recordsPerPage": "_10",
    }


def _parse_rss(content: bytes, query: str, base_url: str) -> list[EisRssItem]:
    root = ET.fromstring(content)
    result: list[EisRssItem] = []
    for node in root.findall(".//item"):
        raw_url = (node.findtext("link") or "").strip()
        description = node.findtext("description") or ""
        url = urljoin(base_url, raw_url)
        registry_number = _registry_number(url) or _registry_number(description)
        if url.startswith("http") and registry_number:
            result.append(
                EisRssItem(
                    query=query,
                    url=url,
                    registry_number=registry_number,
                    description=description,
                )
            )
    return result


def _registry_number(value: str) -> str | None:
    parsed = parse_qs(urlparse(value).query)
    number = next(iter(parsed.get("reestrNumber", [])), "")
    if number.isdigit():
        return number
    match = re.search(r"(?:реестровой записи контракта[^\d]*|№\s*)(\d{15,25})", value)
    return match.group(1) if match else None


def _deduplicate_items(items: list[EisRssItem]) -> list[EisRssItem]:
    seen: set[str] = set()
    result: list[EisRssItem] = []
    for item in items:
        if item.registry_number not in seen:
            seen.add(item.registry_number)
            result.append(item)
    return result


def _supplier_from_contract(
    *,
    common_url: str,
    common_text: str,
    product_url: str,
    product_text: str,
    item: EisRssItem,
    request: SearchRequest,
) -> Supplier | None:
    common_lines = _lines(common_text)
    supplier_lines = _supplier_section(common_lines)
    name, inn = _supplier_identity(supplier_lines)
    if not name:
        return None

    quote, quote_url = _product_quote(product_text, request.category), product_url
    if not quote:
        quote = _product_quote(common_text, request.category)
        quote_url = common_url
    if not quote and not item.query.replace(".", "").isdigit():
        quote = _product_quote(product_text, item.query)
        quote_url = product_url
    if not quote and not item.query.replace(".", "").isdigit():
        quote = _product_quote(common_text, item.query)
        quote_url = common_url
    contract_product = quote
    evidence = [
        Evidence(
            field_name="legal_name",
            value=name,
            source_url=common_url,
            quote=name,
            confidence=0.9,
        )
    ]
    if inn:
        evidence.append(
            Evidence(
                field_name="inn",
                value=inn,
                source_url=common_url,
                quote=inn,
                confidence=0.9,
            )
        )
    products: list[str] = []
    if contract_product:
        products.append(contract_product[:300])
        evidence.append(
            Evidence(
                field_name="contract_products",
                value=contract_product[:300],
                source_url=quote_url if quote else common_url,
                quote=contract_product[:300],
                confidence=0.85,
            )
        )

    regions: list[str] = []
    delivery_place = _value_after(
        common_lines, "Место поставки товара, выполнения работы или оказания услуги"
    )
    if (
        request.region
        and delivery_place
        and request.region.casefold() in delivery_place.casefold()
    ):
        regions.append(request.region)
        evidence.append(
            Evidence(
                field_name="regions",
                value=request.region,
                source_url=common_url,
                quote=delivery_place,
                confidence=0.9,
            )
        )

    joined_supplier = " ".join(supplier_lines)
    phone_raw = _first(_PHONE, joined_supplier)
    email_raw = _first(_EMAIL, joined_supplier)
    if phone_raw:
        evidence.append(
            Evidence(
                field_name="phone",
                value=normalize_phone(phone_raw) or phone_raw,
                source_url=common_url,
                quote=phone_raw,
                confidence=0.9,
            )
        )
    if email_raw:
        evidence.append(
            Evidence(
                field_name="email",
                value=normalize_email(email_raw) or email_raw,
                source_url=common_url,
                quote=email_raw,
                confidence=0.9,
            )
        )
    source_urls = [common_url]
    if product_text:
        source_urls.append(product_url)
    return normalize_supplier(
        Supplier(
            id=f"eis-{inn or uuid.uuid5(uuid.NAMESPACE_URL, common_url).hex[:16]}",
            name=name,
            legal_name=name,
            inn=inn,
            contract_products=products,
            regions=regions,
            phone_raw=phone_raw,
            phone=normalize_phone(phone_raw),
            email=normalize_email(email_raw),
            source_urls=source_urls,
            discovery_queries=[f"ЕИС: {item.query}; контракт № {item.registry_number}"],
            evidence=evidence,
            checked_at=datetime.now(timezone.utc),
        )
    )


def _supplier_section(lines: list[str]) -> list[str]:
    try:
        start = lines.index("Информация о поставщиках") + 1
    except ValueError:
        return []
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("Информация о контракте")
            or lines[index].startswith("Обеспечение исполнения")
        ),
        min(len(lines), start + 40),
    )
    return lines[start:end]


def _supplier_identity(lines: list[str]) -> tuple[str | None, str | None]:
    headers = {
        "Организация",
        "Страна, код",
        "Адрес места нахождения",
        "Почтовый адрес",
        "Телефон, электронная почта",
        "Статус",
        "Индивидуальный предприниматель",
        "Юридическое лицо",
    }
    inn_index = next(
        (index for index, line in enumerate(lines) if line.rstrip(":") == "ИНН"), None
    )
    before_inn = lines[:inn_index] if inn_index is not None else lines
    name = next((line for line in before_inn if line not in headers), None)
    inn = None
    if inn_index is not None:
        inn = next(
            (
                match.group(0)
                for line in lines[inn_index + 1 : inn_index + 4]
                if (match := _INN.search(line))
            ),
            None,
        )
    return name, inn


def _product_quote(text: str, category: str) -> str | None:
    tokens = [token for token in _tokens(category) if token not in _PRODUCT_STOP]
    if not tokens:
        return None
    for line in sorted(_lines(text), key=len):
        line_tokens = _tokens(line)
        if all(
            any(_same_stem(token, other) for other in line_tokens) for token in tokens
        ):
            return line[:300]
    return None


def _tokens(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORDS.finditer(text)]


def _same_stem(left: str, right: str) -> bool:
    if left == right:
        return True
    length = min(5, len(left), len(right))
    return length >= 4 and left[:length] == right[:length]


def _lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def _value_after(lines: list[str], label: str) -> str | None:
    try:
        index = lines.index(label)
    except ValueError:
        return None
    return lines[index + 1] if index + 1 < len(lines) else None


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def _source(
    url: str,
    status: SourceStatus,
    query: str,
    *,
    supplier_name: str | None = None,
    http_status: int | None = None,
    note: str | None = None,
) -> SourceRecord:
    return SourceRecord(
        url=url,
        domain=urlparse(url).netloc,
        status=status,
        http_status=http_status,
        note=note,
        supplier_name=supplier_name,
        discovery_query=query,
        provider="ЕИС",
        source_kind=SourceKind.GOVERNMENT_CONTRACT,
    )


def _ssl_context(ca_bundle: Path | None) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca_bundle) if ca_bundle else None)


def _error_message(error: BaseException, ca_bundle: Path | None) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 429:
            return "HTTP 429: ЕИС временно ограничила частоту запросов"
        if status >= 500:
            return f"HTTP {status}: временная ошибка сервера ЕИС"
    message = str(error).replace("\n", " ")[:300]
    if "CERTIFICATE_VERIFY_FAILED" in message and ca_bundle is None:
        return (
            "сертификат ЕИС не доверен Python; укажите EIS_CA_BUNDLE с доверенным "
            "CA-сертификатом"
        )
    return message or type(error).__name__
