"""Прозрачный расчёт оценки поставщика.

Оценку считает формула с весами из конфигурации. Каждое начисление и каждый
штраф попадают в расшифровку, поэтому пользователь видит, из чего собран балл.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from src.config import ScoringWeights, get_settings
from src.models import (
    CompanyType,
    ScoredSupplier,
    ScoreItem,
    SearchRequest,
    Supplier,
    VerificationStatus,
)
from src.normalization import parse_weight_kg, region_covers

# Служебные слова запроса, которые не описывают товар.
_STOP_WORDS = frozenset(
    {
        "оптом",
        "опт",
        "от",
        "до",
        "с",
        "и",
        "для",
        "в",
        "на",
        "по",
        "или",
        "а",
        "также",
        "куплю",
        "купить",
        "поставщик",
        "поставщики",
        "производитель",
        "цена",
        "цены",
        "доставка",
    }
)

# Слова, которые описывают группу товаров, но не конкретную позицию:
# «замороженные полуфабрикаты» не подтверждают наличие сырников.
_GENERIC_TOKENS = frozenset(
    {
        "замороженный",
        "заморозка",
        "охлаждённый",
        "охлажденный",
        "свежий",
        "продукты",
        "продукция",
        "полуфабрикаты",
        "товары",
        "весовой",
        "фасованный",
        "пищевой",
    }
)

# Каталоги организаций и отраслевые площадки: страница на таком домене
# не является публикацией самой компании.
_CATALOG_DOMAINS = frozenset(
    {
        "productcenter.ru",
        "aboutpartner.ru",
        "agroserver.ru",
        "all.biz",
        "foodtender.ru",
        "gostpp.ru",
        "meatinfo.ru",
        "optomtovar.ru",
        "optkatalog.ru",
        "pulscen.ru",
        "tiu.ru",
        "satom.ru",
        "flagma.ru",
        "blizko.ru",
        "yell.ru",
        "2gis.ru",
        "rusprofile.ru",
        "regtorg.ru",
        "vsepostavshiki.ru",
        "yopt.org",
    }
)

# Минимальная длина общего префикса, при которой формы слова считаются одной.
_STEM_LENGTH = 5

# Поля, отсутствие которых показывается пользователю явно.
_TRACKED_FIELDS = (
    "regions",
    "minimum_order",
    "price",
    "delivery",
    "certificates",
    "phone",
    "email",
    "address",
    "inn",
)


def tokenize(text: str) -> list[str]:
    """Разбивает текст на значимые слова в нижнем регистре."""
    words = re.findall(r"[\w-]+", text.lower(), flags=re.UNICODE)
    return [word for word in words if len(word) > 2 and word not in _STOP_WORDS]


def _same_word(left: str, right: str) -> bool:
    """Сравнивает слова с поправкой на словоформы: «сырники» и «сырник»."""
    if left == right:
        return True
    prefix = min(len(left), len(right), _STEM_LENGTH)
    if prefix >= _STEM_LENGTH and left[:prefix] == right[:prefix]:
        return True
    return fuzz.ratio(left, right) >= 88


def _is_generic(token: str) -> bool:
    """Проверяет, что слово описывает группу товаров, а не позицию."""
    return any(_same_word(token, generic) for generic in _GENERIC_TOKENS)


@dataclass
class CategoryMatch:
    """Итог сопоставления запроса с ассортиментом поставщика.

    ``key_ratio`` считается только по словам, которые называют товар:
    совпадение слова «замороженные» само по себе ничего не подтверждает.
    """

    key_ratio: float
    related_only: bool
    positions: list[str]


def _match_category(supplier: Supplier, request: SearchRequest) -> CategoryMatch:
    """Сопоставляет запрошенную категорию с ассортиментом поставщика."""
    query_tokens = tokenize(request.category)
    if not query_tokens:
        return CategoryMatch(0.0, False, [])

    key_tokens = [token for token in query_tokens if not _is_generic(token)]
    if not key_tokens:
        key_tokens = query_tokens

    matched_key_tokens: set[str] = set()
    key_positions: list[str] = []
    related_positions: list[str] = []

    for position in supplier.product_categories + supplier.matched_products:
        if _conflicts_with_query(position, request.category):
            continue
        position_tokens = tokenize(position)
        hits = {
            token
            for token in key_tokens
            if any(_same_word(token, other) for other in position_tokens)
        }
        if hits:
            matched_key_tokens |= hits
            key_positions.append(position)
        elif any(
            _same_word(token, other)
            for token in query_tokens
            for other in position_tokens
        ):
            related_positions.append(position)

    if matched_key_tokens:
        return CategoryMatch(
            len(matched_key_tokens) / len(key_tokens), False, key_positions
        )
    return CategoryMatch(0.0, bool(related_positions), related_positions)


def _conflicts_with_query(product: str, query: str) -> bool:
    """Не принимает готовое блюдо за одноимённое пищевое сырьё."""
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
        "запеч",
        "жарен",
        "салат",
        "шаурм",
    )
    product_text = product.casefold()
    query_text = query.casefold()
    return any(
        marker in product_text and marker not in query_text
        for marker in prepared_markers
    )


def _region_confirmed(supplier: Supplier, region: str) -> bool:
    """Работа в регионе подтверждается географией, адресом или доставкой."""
    if any(region_covers(value, region) for value in supplier.regions):
        return True
    haystack = " ".join(filter(None, (supplier.address, supplier.delivery))).lower()
    return bool(haystack) and region.lower() in haystack


def _is_catalog_domain(domain: str | None) -> bool:
    """Проверяет, что домен принадлежит каталогу организаций."""
    if not domain:
        return False
    return any(
        domain == catalog or domain.endswith(f".{catalog}")
        for catalog in _CATALOG_DOMAINS
    )


def _uses_only_secondary_sources(supplier: Supplier) -> bool:
    """Данные получены только из каталога организаций, без сайта компании."""
    if not supplier.website:
        return bool(supplier.source_urls)
    return _is_catalog_domain(supplier.domain)


def _missing_fields(supplier: Supplier) -> list[str]:
    """Значимые поля, для которых значение не подтверждено."""
    values = {name: getattr(supplier, name) for name in _TRACKED_FIELDS}
    values["phone"] = supplier.display_phone
    return [name for name, value in values.items() if value in (None, "", [])]


class SupplierScorer:
    """Считает оценку поставщика относительно запроса пользователя."""

    def __init__(self, weights: ScoringWeights | None = None) -> None:
        self._weights = weights or get_settings().scoring

    def score(
        self,
        supplier: Supplier,
        request: SearchRequest,
        page_unavailable: bool = False,
    ) -> ScoredSupplier:
        """Возвращает поставщика с баллом, расшифровкой и списком пробелов."""
        weights = self._weights
        breakdown: list[ScoreItem] = []

        if supplier.company_type is CompanyType.RETAILER:
            return self._excluded(supplier, "розничная торговля, а не оптовая поставка")
        if supplier.company_type is CompanyType.MARKETPLACE:
            return self._excluded(supplier, "каталог организаций, а не поставщик")

        match = _match_category(supplier, request)
        if match.key_ratio == 0 and not match.related_only:
            return self._excluded(
                supplier, f"ассортимент не подтверждает категорию «{request.category}»"
            )

        breakdown.append(self._category_item(match, request.category))
        breakdown.append(self._region_item(supplier, request))
        breakdown.append(self._delivery_item(supplier))
        breakdown.extend(self._minimum_order_items(supplier, request))
        breakdown.append(self._certificates_item(supplier))
        breakdown.append(self._contacts_item(supplier))
        breakdown.append(self._price_item(supplier))

        secondary_only = _uses_only_secondary_sources(supplier)
        if secondary_only:
            breakdown.append(
                ScoreItem(
                    points=weights.penalty_secondary_source_only,
                    reason="сведения только из каталога организаций, сайт не найден",
                )
            )
        if page_unavailable:
            breakdown.append(
                ScoreItem(
                    points=weights.penalty_page_unavailable,
                    reason="страницы поставщика не открылись при проверке",
                )
            )

        total = sum(item.points for item in breakdown)
        normalized = max(0.0, min(100.0, total / weights.max_score * 100))
        return ScoredSupplier(
            supplier=supplier,
            score=round(normalized, 1),
            breakdown=breakdown,
            missing_fields=_missing_fields(supplier),
            verification_status=VerificationStatus.VERIFIED,
            verification_reason=(
                "точный товар подтверждён отраслевым каталогом; "
                "актуальность рекомендуется уточнить у поставщика"
                if secondary_only
                else "точный товар подтверждён официальным сайтом"
            ),
        )

    def score_candidate(
        self,
        supplier: Supplier,
        request: SearchRequest,
        page_unavailable: bool = False,
    ) -> ScoredSupplier:
        """Оценивает найденную организацию без начисления за точный товар."""
        if supplier.company_type is CompanyType.RETAILER:
            return self._excluded(supplier, "розничная торговля, а не оптовая поставка")
        if supplier.company_type is CompanyType.MARKETPLACE:
            return self._excluded(supplier, "каталог организаций, а не поставщик")

        breakdown = [
            ScoreItem(
                points=0,
                reason=f"точное наличие «{request.category}» требует проверки",
            ),
            self._region_item(supplier, request),
            self._delivery_item(supplier),
            *self._minimum_order_items(supplier, request),
            self._certificates_item(supplier),
            self._contacts_item(supplier),
            self._price_item(supplier),
        ]
        if _uses_only_secondary_sources(supplier):
            breakdown.append(
                ScoreItem(
                    points=self._weights.penalty_secondary_source_only,
                    reason="сведения только из вторичного источника",
                )
            )
        if page_unavailable:
            breakdown.append(
                ScoreItem(
                    points=self._weights.penalty_page_unavailable,
                    reason="страницы поставщика не открылись при проверке",
                )
            )
        total = sum(item.points for item in breakdown)
        normalized = max(0.0, min(100.0, total / self._weights.max_score * 100))
        if supplier.contract_products:
            verification_reason = (
                "в ЕИС найдена история контрактов по товару: "
                f"{', '.join(supplier.contract_products[:2])}; актуальное наличие "
                "на сайте ещё не подтверждено"
            )
        else:
            verification_reason = (
                f"организация найдена по запросу, но точное наличие "
                f"«{request.category}» на странице не подтверждено"
            )
        return ScoredSupplier(
            supplier=supplier,
            score=round(normalized, 1),
            breakdown=breakdown,
            missing_fields=_missing_fields(supplier),
            verification_status=VerificationStatus.CANDIDATE,
            verification_reason=verification_reason,
        )

    def _excluded(self, supplier: Supplier, reason: str) -> ScoredSupplier:
        return ScoredSupplier(
            supplier=supplier,
            score=0,
            breakdown=[ScoreItem(points=0, reason=f"исключено: {reason}")],
            missing_fields=_missing_fields(supplier),
            excluded=True,
            exclusion_reason=reason,
            verification_status=VerificationStatus.EXCLUDED,
            verification_reason=reason,
        )

    def _category_item(self, match: CategoryMatch, category: str) -> ScoreItem:
        listed = ", ".join(f"«{item}»" for item in match.positions[:3])
        if match.key_ratio >= 1:
            return ScoreItem(
                points=self._weights.category_match,
                reason=f"ассортимент подтверждает категорию: {listed}",
            )
        if match.key_ratio > 0:
            return ScoreItem(
                points=round(self._weights.category_match / 2, 1),
                reason=f"категория подтверждена частично: {listed}",
            )
        return ScoreItem(
            points=0,
            reason=f"позиции «{category}» в ассортименте нет, найдены смежные "
            f"товары: {listed} — наличие нужно уточнить у поставщика",
        )

    def _region_item(self, supplier: Supplier, request: SearchRequest) -> ScoreItem:
        if not request.region:
            return ScoreItem(points=0, reason="регион в запросе не указан")
        if _region_confirmed(supplier, request.region):
            return ScoreItem(
                points=self._weights.region_match,
                reason=f"подтверждена работа в регионе «{request.region}»",
            )
        return ScoreItem(
            points=self._weights.penalty_region_unconfirmed,
            reason=f"работа в регионе «{request.region}» не подтверждена",
        )

    def _delivery_item(self, supplier: Supplier) -> ScoreItem:
        if supplier.delivery:
            return ScoreItem(
                points=self._weights.delivery_available,
                reason=f"опубликованы условия доставки: {supplier.delivery}",
            )
        return ScoreItem(points=0, reason="условия доставки не найдены")

    def _minimum_order_items(
        self, supplier: Supplier, request: SearchRequest
    ) -> list[ScoreItem]:
        if not supplier.minimum_order:
            return [ScoreItem(points=0, reason="минимальный заказ не указан")]

        items = [
            ScoreItem(
                points=self._weights.minimum_order_found,
                reason=f"указан минимальный заказ: {supplier.minimum_order}",
            )
        ]
        if request.required_volume_kg is None:
            return items

        minimum_kg = supplier.minimum_order_kg or parse_weight_kg(
            supplier.minimum_order
        )
        if minimum_kg is None:
            items.append(
                ScoreItem(
                    points=0,
                    reason="минимальный заказ указан не в весовых единицах, "
                    "сравнение с объёмом запроса невозможно",
                )
            )
        elif minimum_kg <= request.required_volume_kg:
            items.append(
                ScoreItem(
                    points=self._weights.minimum_order_fits,
                    reason=f"минимальный заказ {minimum_kg:g} кг укладывается "
                    f"в объём {request.required_volume_kg:g} кг",
                )
            )
        else:
            items.append(
                ScoreItem(
                    points=0,
                    reason=f"минимальный заказ {minimum_kg:g} кг выше объёма "
                    f"{request.required_volume_kg:g} кг",
                )
            )
        return items

    def _certificates_item(self, supplier: Supplier) -> ScoreItem:
        if supplier.certificates:
            return ScoreItem(
                points=self._weights.certificates_found,
                reason="опубликованы сведения о документах: "
                + ", ".join(supplier.certificates[:3]),
            )
        return ScoreItem(points=0, reason="сведения о документах не найдены")

    def _contacts_item(self, supplier: Supplier) -> ScoreItem:
        contacts = [
            value for value in (supplier.display_phone, supplier.email) if value
        ]
        if contacts:
            return ScoreItem(
                points=self._weights.direct_contacts,
                reason="найдены прямые контакты: " + ", ".join(contacts),
            )
        return ScoreItem(points=0, reason="прямые контакты не найдены")

    def _price_item(self, supplier: Supplier) -> ScoreItem:
        if supplier.price:
            return ScoreItem(
                points=self._weights.price_published,
                reason=f"опубликована цена: {supplier.price}",
            )
        return ScoreItem(points=0, reason="цена не опубликована")


def format_breakdown(scored: ScoredSupplier) -> str:
    """Готовит расшифровку рейтинга для показа пользователю."""
    lines = [
        f"{item.points:+.0f} — {item.reason}" if item.points else f"  0 — {item.reason}"
        for item in scored.breakdown
    ]
    lines.append(f"Итого: {scored.score:g}/100")
    return "\n".join(lines)
