"""Поиск поставщиков в публичном API «Госзатраты» по данным ЕИС."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any

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
from src.normalization import normalize_email, normalize_phone, normalize_supplier
from src.search.base import ProgressCallback, ProviderResult, SupplierProvider

_REGION_CODES = {
    "москва": "77",
    "московская область": "50",
    "санкт-петербург": "78",
    "ленинградская область": "47",
    "екатеринбург": "66",
    "свердловская область": "66",
    "новосибирск": "54",
    "новосибирская область": "54",
    "краснодар": "23",
    "краснодарский край": "23",
    "красноярск": "24",
    "красноярский край": "24",
    "челябинск": "74",
    "челябинская область": "74",
    "пермь": "59",
    "пермский край": "59",
    "уфа": "02",
    "республика башкортостан": "02",
    "казань": "16",
    "республика татарстан": "16",
    "самара": "63",
    "самарская область": "63",
    "ростов-на-дону": "61",
    "ростовская область": "61",
    "нижний новгород": "52",
    "нижегородская область": "52",
    "омск": "55",
    "омская область": "55",
    "воронеж": "36",
    "воронежская область": "36",
    "волгоград": "34",
    "волгоградская область": "34",
    "тюмень": "72",
    "тюменская область": "72",
}
_WORD = re.compile(r"[а-яёa-z0-9-]{3,}", re.IGNORECASE)
_STOP = {"купить", "оптом", "поставка", "поставщик", "цена"}


class ClearSpendingProvider(SupplierProvider):
    """Получает исполнителей контрактов без обращения к HTML-карточкам ЕИС."""

    name = "Госзатраты (данные ЕИС)"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 12,
        max_contracts: int = 30,
        max_suppliers: int = 12,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._max_contracts = max_contracts
        self._max_suppliers = max_suppliers
        self._client = client

    def collect(
        self,
        request: SearchRequest,
        queries: list[str],
        progress: ProgressCallback,
    ) -> ProviderResult:
        del queries
        progress("Госзатраты: " + " | ".join(_contract_queries(request)))
        try:
            return asyncio.run(
                asyncio.wait_for(self._collect_async(request), timeout=self._timeout)
            )
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, OSError) as error:
            return ProviderResult(
                warnings=[
                    f"Госзатраты временно недоступны; поиск продолжен: {_safe_error(error)}"
                ],
                provider_stats={self.name: "недоступен"},
                pages_failed=1,
            )

    async def _collect_async(self, request: SearchRequest) -> ProviderResult:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": "SupplierSearchMVP/1.0 (public procurement data)"},
        )
        try:
            region_code = _region_code(request.region)
            contract_queries = _contract_queries(request)
            parameter_sets = [
                *(
                    [
                        _params(
                            contract_queries[0],
                            region_code,
                            self._max_contracts,
                        )
                    ]
                    if region_code and contract_queries
                    else []
                ),
                *(
                    _params(query, None, self._max_contracts)
                    for query in contract_queries
                ),
            ]
            responses = await asyncio.gather(
                *(
                    _get_with_retry(client, self._base_url, params)
                    for params in parameter_sets
                ),
                return_exceptions=True,
            )
            return self._build_result(request, parameter_sets, responses)
        finally:
            if owned:
                await client.aclose()

    def _build_result(
        self,
        request: SearchRequest,
        parameter_sets: list[dict[str, str]],
        responses: list[httpx.Response | BaseException],
    ) -> ProviderResult:
        result = ProviderResult()
        newest_date = ""
        contracts_seen: set[str] = set()
        suppliers_seen: set[tuple[str, str]] = set()
        for params, response in zip(parameter_sets, responses):
            url = str(httpx.URL(self._base_url, params=params))
            if isinstance(response, BaseException):
                result.pages_failed += 1
                result.sources.append(
                    _source(url, SourceStatus.FAILED, _safe_error(response))
                )
                continue
            response.raise_for_status()
            payload = response.json().get("contracts", {})
            contracts = payload.get("data") or []
            result.sources.append(
                _source(
                    str(response.url),
                    SourceStatus.OK,
                    f"найдено контрактов: {payload.get('total', len(contracts))}",
                    response.status_code,
                )
            )
            for contract in contracts:
                reg_num = str(contract.get("regNum") or contract.get("number") or "")
                if not reg_num or reg_num in contracts_seen:
                    continue
                contracts_seen.add(reg_num)
                sign_date = str(contract.get("signDate") or "")[:10]
                newest_date = max(newest_date, sign_date)
                products = _matching_products(
                    contract.get("products"), request.category
                )
                if not products:
                    continue
                official_url = _contract_url(contract, reg_num)
                for raw_supplier in contract.get("suppliers") or []:
                    supplier = _supplier(
                        raw_supplier,
                        contract,
                        products,
                        request,
                        str(response.url),
                        official_url,
                    )
                    if supplier is None:
                        continue
                    key = (supplier.inn or "", supplier.name.casefold())
                    if key in suppliers_seen:
                        continue
                    suppliers_seen.add(key)
                    result.suppliers.append(supplier)
                    result.sources.append(
                        SourceRecord(
                            url=official_url,
                            domain="zakupki.gov.ru",
                            status=SourceStatus.SKIPPED,
                            note=f"контракт № {reg_num}, дата {sign_date or 'не указана'}",
                            supplier_name=supplier.name,
                            discovery_query=request.category,
                            provider=self.name,
                            source_kind=SourceKind.GOVERNMENT_CONTRACT,
                        )
                    )
                    if len(result.suppliers) >= self._max_suppliers:
                        break
                if len(result.suppliers) >= self._max_suppliers:
                    break
            if len(result.suppliers) >= self._max_suppliers:
                break
        result.provider_stats[self.name] = (
            f"контрактов просмотрено {len(contracts_seen)}, поставщиков "
            f"{len(result.suppliers)}, ошибок {result.pages_failed}"
            + (f", свежая запись {newest_date}" if newest_date else "")
        )
        return result


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, str]
) -> httpx.Response:
    for attempt in range(2):
        try:
            response = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt:
                raise
        else:
            if response.status_code not in {429, 500, 502, 503, 504} or attempt:
                return response
        await asyncio.sleep(0.3)
    raise RuntimeError("повтор запроса не завершён")


def _params(
    category: str, region_code: str | None, max_contracts: int
) -> dict[str, str]:
    result = {
        "productsearch": category.strip(),
        "perpage": str(min(max(max_contracts, 1), 50)),
        "sort": "-signDate",
    }
    if region_code:
        result["customerregion"] = region_code
    return result


def _contract_queries(request: SearchRequest) -> list[str]:
    values = [request.category, *request.eis_queries]
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        if not normalized or re.fullmatch(r"[\d.\s-]+", normalized):
            continue
        if normalized.casefold() not in {item.casefold() for item in result}:
            result.append(normalized)
        if len(result) >= 2:
            break
    return result


def _region_code(region: str | None) -> str | None:
    if not region:
        return None
    lowered = re.sub(r"\s+", " ", region.casefold()).strip()
    return next((code for name, code in _REGION_CODES.items() if name in lowered), None)


def _matching_products(raw: object, category: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    wanted = [word for word in _WORD.findall(category.casefold()) if word not in _STOP]
    result: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"].strip()
        words = _WORD.findall(name.casefold())
        if (
            wanted
            and not _conflicts_with_query(name, category)
            and all(any(_same_stem(term, word) for word in words) for term in wanted)
        ):
            result.append(name[:240])
    return list(dict.fromkeys(result))


def _same_stem(left: str, right: str) -> bool:
    length = min(5, len(left), len(right))
    return left == right or (length >= 4 and left[:length] == right[:length])


def _conflicts_with_query(product: str, query: str) -> bool:
    prepared_markers = (
        "сэндвич",
        "сендвич",
        "пельмен",
        "пельмеш",
        "котлет",
        "жульен",
        "рулет",
        "вялен",
        "снек",
    )
    product_text = product.casefold()
    query_text = query.casefold()
    return any(
        marker in product_text and marker not in query_text
        for marker in prepared_markers
    )


def _supplier(
    raw: object,
    contract: dict[str, Any],
    products: list[str],
    request: SearchRequest,
    api_url: str,
    official_url: str,
) -> Supplier | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("organizationName") or "").strip()
    if not name:
        return None
    inn = str(raw.get("inn") or "").strip() or None
    address = (
        str(raw.get("factualAddress") or raw.get("postalAddress") or "").strip() or None
    )
    contact = raw.get("contactInfo") if isinstance(raw.get("contactInfo"), dict) else {}
    phone_raw = (
        str(contact.get("phone") or contact.get("phoneNumber") or "").strip() or None
    )
    email = normalize_email(str(contact.get("email") or ""))
    supplier = Supplier(
        id=f"contracts-{uuid.uuid5(uuid.NAMESPACE_URL, inn or name).hex[:16]}",
        name=name,
        legal_name=name,
        inn=inn,
        company_type=CompanyType.UNKNOWN,
        contract_products=products,
        address=address,
        regions=[request.region]
        if request.region and _contract_in_region(contract, request.region)
        else [],
        phone=normalize_phone(phone_raw),
        phone_raw=phone_raw,
        email=email,
        source_urls=[api_url, official_url],
        discovery_queries=[request.category],
        checked_at=datetime.now(timezone.utc),
    )
    for product in products:
        supplier.evidence.append(
            Evidence(
                field_name="contract_products",
                value=product,
                source_url=api_url,
                quote=product,
                confidence=0.78,
            )
        )
    for field, value in (
        ("legal_name", name),
        ("inn", inn),
        ("address", address),
        ("phone", phone_raw),
        ("email", email),
    ):
        if value:
            supplier.evidence.append(
                Evidence(
                    field_name=field,
                    value=str(value),
                    source_url=api_url,
                    quote=str(value),
                    confidence=0.78,
                )
            )
    if supplier.regions:
        quote = _customer_address(contract) or request.region or ""
        supplier.evidence.append(
            Evidence(
                field_name="regions",
                value=supplier.regions[0],
                source_url=api_url,
                quote=quote,
                confidence=0.75,
            )
        )
    return normalize_supplier(supplier)


def _contract_in_region(contract: dict[str, Any], region: str) -> bool:
    code = _region_code(region)
    return bool(
        code
        and (
            str(contract.get("regionCode") or "") == code
            or code in _customer_address(contract)
        )
    )


def _customer_address(contract: dict[str, Any]) -> str:
    customer = contract.get("customer")
    if not isinstance(customer, dict):
        return ""
    return str(customer.get("postalAddress") or customer.get("legalAddress") or "")


def _contract_url(contract: dict[str, Any], reg_num: str) -> str:
    raw = str(contract.get("contractUrl") or "")
    if raw.startswith("http://"):
        raw = "https://" + raw.removeprefix("http://")
    return (
        raw
        if raw.startswith("https://")
        else f"https://zakupki.gov.ru/epz/contract/contractCard/common-info.html?reestrNumber={reg_num}"
    )


def _source(
    url: str, status: SourceStatus, note: str, http_status: int | None = None
) -> SourceRecord:
    return SourceRecord(
        url=url,
        domain="openapi.clearspending.ru",
        status=status,
        http_status=http_status,
        note=note,
        provider=ClearSpendingProvider.name,
        source_kind=SourceKind.GOVERNMENT_CONTRACT,
    )


def _safe_error(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return str(error).replace("\n", " ")[:200] or type(error).__name__
