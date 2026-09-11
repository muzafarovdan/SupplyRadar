"""Формирование поисковых запросов по потребности пользователя.

Шаблоны детерминированные: одинаковый запрос всегда даёт одинаковый набор.
Синонимы категории передаются извне, чтобы позже их могла предлагать LLM,
не меняя логику построения запросов.
"""

from __future__ import annotations

import re

from src.models import SearchRequest

MAX_QUERIES = 6

# Шаблоны с регионом применяются, только если регион указан.
_TEMPLATES_WITH_REGION = (
    "{category} оптом {region}",
    "поставщик {category} доставка {region}",
    "{category} прайс поставщик {region}",
)

_TEMPLATES_WITHOUT_REGION = (
    "{category} оптом",
    "производитель {category} оптовые условия",
    "{category} сертификаты производитель",
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_queries(
    request: SearchRequest,
    category_synonyms: list[str] | None = None,
) -> list[str]:
    """Возвращает 3-6 непохожих запросов для поиска кандидатов."""
    category = _clean(request.category)
    if not category:
        return []

    region = _clean(request.region or "")
    queries: list[str] = []

    templates = (
        (*_TEMPLATES_WITH_REGION, *_TEMPLATES_WITHOUT_REGION)
        if region
        else _TEMPLATES_WITHOUT_REGION
    )
    for template in templates:
        queries.append(_clean(template.format(category=category, region=region)))

    for synonym in category_synonyms or []:
        synonym = _clean(synonym)
        if not synonym:
            continue
        template = "{category} оптом {region}" if region else "{category} оптом"
        queries.append(_clean(template.format(category=synonym, region=region)))

    if request.documents_required:
        queries.append(_clean(f"{category} декларация соответствия поставщик"))

    unique: list[str] = []
    for query in queries:
        if query not in unique:
            unique.append(query)
    return unique[:MAX_QUERIES]
