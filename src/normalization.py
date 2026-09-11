"""Приведение значений к общему виду: ссылки, телефоны, регионы, единицы.

Исходное значение всегда сохраняется рядом с нормализованным, потому что
преобразование бывает неоднозначным: «от 1 коробки» нельзя выразить в килограммах,
но текст условия важен для пользователя.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import phonenumbers

from src.models import Supplier

_TRACKING_PARAM_PREFIXES = ("utm_", "yclid", "gclid", "fbclid", "_openstat")

_LEGAL_FORMS = (
    "общество с ограниченной ответственностью",
    "индивидуальный предприниматель",
    "акционерное общество",
    "публичное акционерное общество",
    "открытое акционерное общество",
    "закрытое акционерное общество",
    "торговый дом",
    "производственная компания",
    "ооо",
    "оао",
    "зао",
    "пао",
    "нао",
    "ао",
    "ип",
    "тд",
    "тк",
    "тпк",
    "гк",
)

# Единицы массы приводятся к килограммам.
_WEIGHT_UNITS: dict[str, float] = {
    "кг": 1.0,
    "килограмм": 1.0,
    "килограммов": 1.0,
    "г": 0.001,
    "гр": 0.001,
    "грамм": 0.001,
    "граммов": 0.001,
    "т": 1000.0,
    "тонна": 1000.0,
    "тонн": 1000.0,
    "тонны": 1000.0,
}

# Единицы объёма приводятся к литрам.
_VOLUME_UNITS: dict[str, float] = {
    "л": 1.0,
    "литр": 1.0,
    "литров": 1.0,
    "мл": 0.001,
    "миллилитров": 0.001,
    "м3": 1000.0,
}

_NUMBER_PATTERN = r"(\d[\d\s\u00a0]*(?:[.,]\d+)?)"

# Разные написания одного региона сводятся к одной форме.
_REGION_ALIASES: dict[str, str] = {
    "екб": "Екатеринбург",
    "ёбург": "Екатеринбург",
    "ебург": "Екатеринбург",
    "екатеринбург": "Екатеринбург",
    "свердловская область": "Свердловская область",
    "свердловская обл": "Свердловская область",
    "россия": "Россия",
    "рф": "Россия",
    "российская федерация": "Россия",
    "вся россия": "Россия",
    "урал": "Урал",
    "уральский федеральный округ": "Урал",
    "урфо": "Урал",
    "москва": "Москва",
    "московская область": "Московская область",
    "санкт-петербург": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
}

# Города, входящие в более крупные регионы. Используется при сравнении географии.
_REGION_HIERARCHY: dict[str, set[str]] = {
    "Екатеринбург": {"Свердловская область", "Урал", "Россия"},
    "Свердловская область": {"Урал", "Россия"},
    "Урал": {"Россия"},
    "Москва": {"Московская область", "Россия"},
    "Московская область": {"Россия"},
    "Санкт-Петербург": {"Россия"},
}

_INN_PATTERN = re.compile(r"\b(\d{10}|\d{12})\b")


def normalize_url(url: str | None) -> str | None:
    """Убирает метки отслеживания, фрагмент и завершающий слэш."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.netloc:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    path = parsed.path.rstrip("/")
    netloc = parsed.netloc.lower().removeprefix("www.")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", urlencode(query), ""))


def normalize_domain(url: str | None) -> str | None:
    """Возвращает домен без ``www``, порта и регистра."""
    if not url:
        return None
    candidate = url.strip()
    if "//" not in candidate:
        candidate = f"//{candidate}"
    host = urlparse(candidate).netloc or urlparse(candidate).path
    host = host.split("@")[-1].split(":")[0].strip("/").lower()
    host = host.removeprefix("www.")
    return host or None


def normalize_phone(raw: str | None, default_region: str = "RU") -> str | None:
    """Приводит телефон к формату E.164. Невалидный номер отбрасывается."""
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: str | None) -> str | None:
    """Приводит email к нижнему регистру."""
    if not raw:
        return None
    email = raw.strip().lower()
    return email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email) else None


def normalize_inn(raw: str | None) -> str | None:
    """Извлекает ИНН из строки: 10 цифр для организации, 12 для ИП."""
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    match = _INN_PATTERN.search(digits)
    return match.group(1) if match else None


def normalize_region(raw: str | None) -> str | None:
    """Сводит написание региона к одной форме."""
    if not raw:
        return None
    key = raw.strip().lower()
    key = re.sub(r"^(г\.?|город|гор\.)\s*", "", key)
    key = re.sub(r"\s*(обл\.?|область)$", " область", key).strip()
    key = re.sub(r"\s+", " ", key).rstrip(".")
    if key in _REGION_ALIASES:
        return _REGION_ALIASES[key]
    return raw.strip()


def normalize_regions(values: list[str]) -> list[str]:
    """Нормализует список регионов, сохраняя порядок и убирая повторы."""
    result: list[str] = []
    for value in values:
        normalized = normalize_region(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def region_covers(supplier_region: str, requested_region: str) -> bool:
    """Проверяет, покрывает ли география поставщика запрошенный регион.

    «Россия» покрывает «Екатеринбург», обратное неверно.
    """
    supplier = normalize_region(supplier_region)
    requested = normalize_region(requested_region)
    if not supplier or not requested:
        return False
    if supplier.casefold() == requested.casefold():
        return True
    return supplier in _REGION_HIERARCHY.get(requested, set())


def normalize_company_name(raw: str | None) -> str | None:
    """Готовит название к сравнению: без ОПФ, кавычек и лишних пробелов."""
    if not raw:
        return None
    name = raw.strip().lower().replace("«", " ").replace("»", " ")
    name = name.replace('"', " ").replace("'", " ").replace("`", " ")
    name = re.sub(r"[^\w\s-]", " ", name, flags=re.UNICODE)
    for form in _LEGAL_FORMS:
        name = re.sub(rf"(?<!\w){re.escape(form)}(?!\w)", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or None


def _parse_number(raw: str) -> float | None:
    cleaned = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_with_units(text: str | None, units: dict[str, float]) -> float | None:
    if not text:
        return None
    unit_pattern = "|".join(sorted(map(re.escape, units), key=len, reverse=True))
    match = re.search(
        rf"{_NUMBER_PATTERN}\s*({unit_pattern})(?!\w)",
        text.lower(),
        flags=re.UNICODE,
    )
    if not match:
        return None
    amount = _parse_number(match.group(1))
    if amount is None:
        return None
    return round(amount * units[match.group(2)], 6)


def parse_weight_kg(text: str | None) -> float | None:
    """Извлекает массу в килограммах. Возвращает ``None``, если массы нет."""
    return _parse_with_units(text, _WEIGHT_UNITS)


def parse_volume_l(text: str | None) -> float | None:
    """Извлекает объём в литрах."""
    return _parse_with_units(text, _VOLUME_UNITS)


def normalize_supplier(supplier: Supplier) -> Supplier:
    """Приводит карточку поставщика к общему виду.

    Ненормализуемые значения не отбрасываются: телефон, который не удалось
    привести к E.164, остаётся в ``phone_raw`` и показывается пользователю.
    """
    normalized = supplier.model_copy(deep=True)

    normalized.website = normalize_url(supplier.website)
    normalized.domain = normalize_domain(supplier.website)
    normalized.phone_raw = supplier.phone_raw or supplier.phone
    normalized.phone = normalize_phone(normalized.phone_raw)
    normalized.email = normalize_email(supplier.email)
    normalized.inn = normalize_inn(supplier.inn)
    normalized.regions = normalize_regions(supplier.regions)
    normalized.minimum_order_kg = supplier.minimum_order_kg or parse_weight_kg(
        supplier.minimum_order
    )

    source_urls: list[str] = []
    for url in supplier.source_urls:
        cleaned = normalize_url(url)
        if cleaned and cleaned not in source_urls:
            source_urls.append(cleaned)

    # Ссылки в подтверждениях приводятся к тому же виду, иначе в карточке
    # цитата будет ссылаться на адрес, которого нет в списке источников.
    for item in normalized.evidence:
        item.source_url = normalize_url(item.source_url) or item.source_url
        if item.source_url not in source_urls:
            source_urls.append(item.source_url)

    normalized.source_urls = source_urls

    return normalized
