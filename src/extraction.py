"""Детерминированное извлечение подтверждённых фактов из HTML."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from src.fetching import FetchedPage
from src.models import CompanyType, Evidence, SearchRequest, SourceStatus, Supplier
from src.normalization import (
    normalize_email,
    normalize_inn,
    normalize_phone,
    normalize_supplier,
)
from src.web_search import SearchHit, is_catalog_url

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}")
_PHONE = re.compile(r"(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}")
_INN = re.compile(r"(?:ИНН\s*[:№]?\s*)(\d{10}|\d{12})", re.IGNORECASE)
_PRICE = re.compile(
    r"(?:от\s*)?\d[\d\s]*(?:[,.]\d+)?\s*(?:₽|р\.?\b|руб(?:\.|лей|ля)?)"
    r"(?:\s*(?:/|за\s+)\s*(?:кг|килограмм\w*|шт\.?|штук\w*|упаков\w*))?",
    re.IGNORECASE,
)
_ACCESS_PRICE_MARKERS = (
    "цена доступа",
    "стоимость доступа",
    "доступ к контакт",
    "откроются все контакты",
    "оплатить",
    "подписк",
    "тариф",
)
_MINIMUM = re.compile(
    r"(?:минимальн\w*\s+заказ|минимальн\w*\s+партия|заказ\s+от)[^\n]{0,100}",
    re.IGNORECASE,
)
_STOP = {"для", "или", "при", "оптом", "купить", "цена", "поставщик"}


def supplier_from_hit(hit: SearchHit) -> Supplier:
    """Создаёт кандидата из ссылки, не принимая сниппет за доказательство."""
    domain = urlparse(hit.url).netloc.removeprefix("www.")
    title = re.split(r"\s+[|—–-]\s+", hit.title, maxsplit=1)[0].strip()
    name = title or domain
    company_type, _ = _company_type([], name)
    catalog = is_catalog_url(hit.url)
    if catalog and _looks_like_catalog_listing(name):
        company_type = CompanyType.MARKETPLACE
    return normalize_supplier(
        Supplier(
            id=f"web-{uuid.uuid5(uuid.NAMESPACE_URL, hit.url).hex[:16]}",
            name=name,
            company_type=company_type,
            website=None
            if catalog
            else f"{urlparse(hit.url).scheme}://{urlparse(hit.url).netloc}",
            source_urls=[hit.url, *hit.related_urls],
            discovery_queries=[hit.query],
            checked_at=datetime.now(timezone.utc),
        )
    )


def _looks_like_catalog_listing(name: str) -> bool:
    """Отличает заголовок раздела/выдачи каталога от названия компании."""
    lowered = name.casefold()
    return any(
        marker in lowered
        for marker in (
            "купить/продать",
            "купить оптом",
            "поставщики ",
            "производители ",
            " в россии",
            "цены на ",
        )
    )


def enrich_supplier(
    supplier: Supplier, pages: Iterable[FetchedPage], request: SearchRequest
) -> Supplier:
    """Дополняет карточку только значениями с буквальной цитатой на странице."""
    enriched = supplier.model_copy(deep=True)
    for page in pages:
        if page.status not in {SourceStatus.OK, SourceStatus.CACHED} or not (
            page.text or page.html
        ):
            continue
        _extract_page(enriched, page, request)
    return normalize_supplier(enriched)


def _extract_page(
    supplier: Supplier, page: FetchedPage, request: SearchRequest
) -> None:
    confidence = 0.7 if is_catalog_url(page.final_url) else 0.9
    lines = _lines(page.text)
    structured = _json_ld(page.html)
    product_names = [
        value
        for item in structured
        if _is_product(item)
        for value in [_string_value(item.get("name"))]
        if value
    ]

    product_quote = _product_quote([*product_names, *lines], request.category)
    if product_quote and product_quote not in supplier.matched_products:
        supplier.matched_products.append(product_quote)
        _add_evidence(
            supplier,
            "matched_products",
            product_quote,
            page.final_url,
            product_quote,
            confidence,
        )

    organizations = [item for item in structured if _is_organization(item)]
    name = _structured_value(organizations, "legalName") or _structured_value(
        organizations, "name"
    )
    if name and not supplier.legal_name:
        supplier.legal_name = name
        _add_evidence(supplier, "legal_name", name, page.final_url, name, confidence)

    if supplier.company_type is CompanyType.UNKNOWN:
        inferred, quote = _company_type(lines, supplier.name)
        if inferred is not CompanyType.UNKNOWN:
            supplier.company_type = inferred
            _add_evidence(
                supplier,
                "company_type",
                inferred.value,
                page.final_url,
                quote,
                confidence,
            )

    email = _first_match(_EMAIL, page.text)
    normalized_email = normalize_email(email)
    if normalized_email and not supplier.email:
        supplier.email = normalized_email
        _add_evidence(
            supplier, "email", normalized_email, page.final_url, email or "", confidence
        )

    phone_raw = _first_match(_PHONE, page.text)
    phone = normalize_phone(phone_raw)
    if phone_raw and not supplier.display_phone:
        supplier.phone_raw = phone_raw
        supplier.phone = phone
        _add_evidence(
            supplier, "phone", phone or phone_raw, page.final_url, phone_raw, confidence
        )

    inn_match = _INN.search(page.text)
    inn = normalize_inn(inn_match.group(1)) if inn_match else None
    if inn and not supplier.inn:
        supplier.inn = inn
        _add_evidence(
            supplier, "inn", inn, page.final_url, inn_match.group(0), confidence
        )

    if not supplier.price:
        price_line = next(
            (
                line[:240]
                for line in lines
                if _PRICE.search(line) and not is_access_price_text(line)
            ),
            None,
        )
        if price_line:
            price_match = _PRICE.search(price_line)
            price_value = price_match.group(0).strip() if price_match else price_line
            supplier.price = price_value
            _add_evidence(
                supplier,
                "price",
                price_value,
                page.final_url,
                price_line,
                confidence,
            )
        elif not supplier.price_access_note:
            access_line = next(
                (
                    line[:240]
                    for line in lines
                    if _PRICE.search(line) and is_access_price_text(line)
                ),
                None,
            )
            if access_line:
                amount = _PRICE.search(access_line)
                price = amount.group(0).strip() if amount else "платно"
                note = (
                    "Цена товара не опубликована; платный доступ ProductCenter "
                    f"к закрытой информации — {price} на 24 часа"
                )
                supplier.price_access_note = note
                _add_evidence(
                    supplier,
                    "price_access_note",
                    note,
                    page.final_url,
                    access_line,
                    confidence,
                )

    if not supplier.minimum_order:
        minimum = _line_with(lines, _MINIMUM)
        if minimum:
            supplier.minimum_order = minimum
            _add_evidence(
                supplier, "minimum_order", minimum, page.final_url, minimum, confidence
            )

    if not supplier.delivery:
        delivery = _keyword_line(lines, ("доставк", "самовывоз", "транспортн"))
        if delivery:
            supplier.delivery = delivery
            _add_evidence(
                supplier, "delivery", delivery, page.final_url, delivery, confidence
            )

    certificate = _keyword_line(
        lines, ("сертификат", "деклараци", "ветеринарн", "меркурий")
    )
    if certificate and certificate not in supplier.certificates:
        supplier.certificates.append(certificate)
        _add_evidence(
            supplier,
            "certificates",
            certificate,
            page.final_url,
            certificate,
            confidence,
        )

    address = _structured_value(organizations, "streetAddress")
    if address and not supplier.address:
        supplier.address = address
        _add_evidence(supplier, "address", address, page.final_url, address, confidence)

    if page.final_url not in supplier.source_urls:
        supplier.source_urls.append(page.final_url)


def _product_quote(lines: list[str], category: str) -> str | None:
    tokens = [token for token in _tokens(category) if token not in _STOP]
    if not tokens:
        return None
    for line in sorted(lines, key=len):
        line_tokens = _tokens(line)
        if all(
            any(_same_stem(token, other) for other in line_tokens) for token in tokens
        ):
            return line[:240]
    return None


def is_access_price_text(value: str) -> bool:
    """Проверяет, что сумма относится к доступу каталога, а не к товару."""
    lowered = value.casefold()
    return any(marker in lowered for marker in _ACCESS_PRICE_MARKERS)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9-]{3,}", text.casefold())


def _same_stem(left: str, right: str) -> bool:
    if left == right:
        return True
    length = min(5, len(left), len(right))
    return length >= 4 and left[:length] == right[:length]


def _lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()
    ]


def _line_with(lines: list[str], pattern: re.Pattern[str]) -> str | None:
    return next((line[:240] for line in lines if pattern.search(line)), None)


def _keyword_line(lines: list[str], keywords: tuple[str, ...]) -> str | None:
    return next(
        (
            line[:240]
            for line in lines
            if any(word in line.casefold() for word in keywords)
        ),
        None,
    )


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def _json_ld(html: str) -> list[dict[str, Any]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict[str, Any]] = []
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        result.extend(_dicts(value))
    return result


def _dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        nested = value.get("@graph")
        return [value, *(_dicts(nested) if nested else [])]
    if isinstance(value, list):
        return [item for value_item in value for item in _dicts(value_item)]
    return []


def _structured_value(items: list[dict[str, Any]], key: str) -> str | None:
    for item in items:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        address = item.get("address")
        if isinstance(address, dict) and isinstance(address.get(key), str):
            return address[key].strip() or None
    return None


def _is_organization(item: dict[str, Any]) -> bool:
    raw_type = item.get("@type")
    types = [raw_type] if isinstance(raw_type, str) else raw_type or []
    return any(
        value in {"Organization", "LocalBusiness", "Corporation"} for value in types
    )


def _is_product(item: dict[str, Any]) -> bool:
    raw_type = item.get("@type")
    types = [raw_type] if isinstance(raw_type, str) else raw_type or []
    return "Product" in types


def _string_value(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _company_type(lines: list[str], name: str) -> tuple[CompanyType, str]:
    markers = (
        (
            CompanyType.RETAILER,
            ("розничн", "супермаркет", "магазин у дома", "мясная лавка"),
        ),
        (
            CompanyType.MANUFACTURER,
            ("производител", "производство", "фабрика", "завод"),
        ),
        (CompanyType.DISTRIBUTOR, ("дистрибьют",)),
        (CompanyType.WHOLESALER, ("оптов", "оптом", "поставщик")),
    )
    for line in [name, *lines[:80]]:
        lowered = line.casefold()
        for company_type, words in markers:
            if any(word in lowered for word in words):
                return company_type, line[:240]
    return CompanyType.UNKNOWN, ""


def _add_evidence(
    supplier: Supplier,
    field_name: str,
    value: str,
    source_url: str,
    quote: str,
    confidence: float,
) -> None:
    key = (field_name, source_url, quote)
    if any(
        (item.field_name, item.source_url, item.quote) == key
        for item in supplier.evidence
    ):
        return
    supplier.evidence.append(
        Evidence(
            field_name=field_name,
            value=value,
            source_url=source_url,
            quote=quote,
            confidence=confidence,
        )
    )
