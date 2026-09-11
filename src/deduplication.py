"""Объединение записей, относящихся к одной компании.

Записи объединяются по сильным признакам (домен, ИНН, телефон) сразу.
Похожие названия объединяются только при дополнительном слабом совпадении:
при сомнении записи остаются раздельными, чтобы не склеить разные компании.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from src.models import Evidence, Supplier
from src.normalization import normalize_company_name, normalize_domain

NAME_SIMILARITY_THRESHOLD = 90

_SCALAR_FIELDS = (
    "legal_name",
    "inn",
    "address",
    "minimum_order",
    "minimum_order_kg",
    "price",
    "delivery",
    "phone",
    "phone_raw",
    "email",
    "website",
    "domain",
)

_LIST_FIELDS = (
    "product_categories",
    "matched_products",
    "contract_products",
    "regions",
    "certificates",
    "discovery_queries",
)


@dataclass
class DeduplicationResult:
    """Итог дедупликации: объединённые записи и число выполненных слияний."""

    suppliers: list[Supplier] = field(default_factory=list)
    merged_count: int = 0


class _UnionFind:
    """Минимальная реализация системы непересекающихся множеств."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, left: int, right: int) -> bool:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return False
        self._parent[right_root] = left_root
        return True


def _strong_keys(supplier: Supplier) -> list[str]:
    """Признаки, при совпадении которых записи считаются одной компанией."""
    keys = [f"id:{supplier.id}"]
    domain = supplier.domain or normalize_domain(supplier.website)
    if domain:
        keys.append(f"domain:{domain}")
    if supplier.inn:
        keys.append(f"inn:{supplier.inn}")
    if supplier.phone:
        keys.append(f"phone:{supplier.phone}")
    return keys


def _weak_match(left: Supplier, right: Supplier) -> bool:
    """Слабое совпадение: адрес или домен электронной почты."""
    if left.address and right.address:
        similarity = fuzz.token_set_ratio(left.address.lower(), right.address.lower())
        if similarity >= NAME_SIMILARITY_THRESHOLD:
            return True
    if left.email and right.email:
        return left.email.split("@")[-1] == right.email.split("@")[-1]
    return False


def _looks_like_same_company(left: Supplier, right: Supplier) -> bool:
    """Похожее название плюс слабый признак."""
    left_name = normalize_company_name(left.name)
    right_name = normalize_company_name(right.name)
    if not left_name or not right_name:
        return False
    if fuzz.token_sort_ratio(left_name, right_name) < NAME_SIMILARITY_THRESHOLD:
        return False
    return _weak_match(left, right)


def _field_confidence(supplier: Supplier, field_name: str) -> float:
    """Наибольшая уверенность подтверждения для поля."""
    confidences = [item.confidence for item in supplier.evidence_for(field_name)]
    return max(confidences, default=0.0)


def _pick_scalar(group: list[Supplier], field_name: str) -> object:
    """Выбирает значение поля: приоритет у подтверждённого и более свежего."""
    filled = [item for item in group if getattr(item, field_name) not in (None, "")]
    if not filled:
        return None
    best = max(
        filled,
        key=lambda item: (_field_confidence(item, field_name), item.checked_at),
    )
    return getattr(best, field_name)


def _merge_lists(group: list[Supplier], field_name: str) -> list[str]:
    """Объединяет списки без повторов, сохраняя порядок появления."""
    merged: list[str] = []
    for supplier in group:
        for value in getattr(supplier, field_name):
            if value not in merged:
                merged.append(value)
    return merged


def _merge_evidence(group: list[Supplier]) -> list[Evidence]:
    """Сохраняет подтверждения всех записей, убирая точные повторы."""
    seen: set[tuple[str, str, str]] = set()
    merged: list[Evidence] = []
    for supplier in group:
        for item in supplier.evidence:
            key = (item.field_name, item.source_url, item.quote)
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return merged


def _merge_group(group: list[Supplier]) -> Supplier:
    """Собирает одну карточку из нескольких записей одной компании."""
    if len(group) == 1:
        return group[0]

    ordered = sorted(group, key=lambda item: item.checked_at, reverse=True)
    # Название берётся с сайта компании: в каталогах организаций оно часто
    # дополнено описанием вида «оптовый склад».
    leader = max(ordered, key=lambda item: (bool(item.website), item.checked_at))

    merged = leader.model_copy(deep=True)
    for field_name in _SCALAR_FIELDS:
        setattr(merged, field_name, _pick_scalar(ordered, field_name))
    for field_name in _LIST_FIELDS:
        setattr(merged, field_name, _merge_lists(ordered, field_name))

    merged.name = leader.name
    merged.company_type = next(
        (item.company_type for item in ordered if item.company_type.value != "unknown"),
        leader.company_type,
    )
    merged.source_urls = _merge_lists(ordered, "source_urls")
    merged.evidence = _merge_evidence(ordered)
    merged.checked_at = max(item.checked_at for item in ordered)
    return merged


def deduplicate(suppliers: list[Supplier]) -> DeduplicationResult:
    """Объединяет записи одной компании, сохраняя источники обеих."""
    if not suppliers:
        return DeduplicationResult()

    union = _UnionFind(len(suppliers))
    merged_count = 0

    seen_keys: dict[str, int] = {}
    for index, supplier in enumerate(suppliers):
        for key in _strong_keys(supplier):
            if key in seen_keys:
                if union.union(seen_keys[key], index):
                    merged_count += 1
            else:
                seen_keys[key] = index

    for left in range(len(suppliers)):
        for right in range(left + 1, len(suppliers)):
            if union.find(left) == union.find(right):
                continue
            if _looks_like_same_company(
                suppliers[left], suppliers[right]
            ) and union.union(left, right):
                merged_count += 1

    groups: dict[int, list[Supplier]] = {}
    for index, supplier in enumerate(suppliers):
        groups.setdefault(union.find(index), []).append(supplier)

    return DeduplicationResult(
        suppliers=[_merge_group(group) for group in groups.values()],
        merged_count=merged_count,
    )
