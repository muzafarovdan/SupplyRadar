"""Текстовые представления результата: карточка, статус запуска, методика."""

from __future__ import annotations

from src.config import ScoringWeights
from src.export import MISSING, field_label
from src.models import ScoredSupplier, SearchRunResult

_COMPANY_TYPE_LABELS = {
    "manufacturer": "производитель",
    "distributor": "дистрибьютор",
    "wholesaler": "оптовый поставщик",
    "marketplace": "каталог или площадка",
    "retailer": "розничная торговля",
    "unknown": "тип не определён",
}


def _value(text: str | None) -> str:
    return text or f"*{MISSING}*"


def _list_value(values: list[str]) -> str:
    return ", ".join(values) if values else f"*{MISSING}*"


def supplier_card(scored: ScoredSupplier) -> str:
    """Подробная карточка поставщика с источниками и подтверждениями."""
    supplier = scored.supplier
    website = (
        f"[{supplier.domain}]({supplier.website})" if supplier.website else _value(None)
    )

    strengths = [
        f"- {item.reason}" for item in scored.breakdown if item.points > 0
    ] or ["- *нет подтверждённых преимуществ*"]
    unknown = [field_label(name) for name in scored.missing_fields]
    sources = [f"- {url}" for url in supplier.source_urls] or [
        "- *источники не указаны*"
    ]

    lines = [
        f"## {supplier.name}",
        (
            f"**Оценка:** {scored.score:g}/100 · "
            f"{_COMPANY_TYPE_LABELS.get(supplier.company_type.value, '')} · "
            f"проверено {supplier.checked_at.strftime('%d.%m.%Y')}"
        ),
        "**Статус проверки:** "
        + (
            "подтверждён"
            if scored.verification_status.value == "verified"
            else "кандидат"
        ),
        *(
            [f"**Причина:** {scored.verification_reason}"]
            if scored.verification_reason
            else []
        ),
        "",
        "### Реквизиты и контакты",
        f"- Юридическое название: {_value(supplier.legal_name)}",
        f"- ИНН: {_value(supplier.inn)}",
        f"- Адрес: {_value(supplier.address)}",
        f"- Телефон: {_value(supplier.display_phone)}",
        f"- Email: {_value(supplier.email)}",
        f"- Сайт: {website}",
        "",
        "### Условия работы",
        f"- Ассортимент: {_list_value(supplier.product_categories)}",
        f"- Совпавшие позиции: {_list_value(supplier.matched_products)}",
        f"- Поставки по контрактам ЕИС: {_list_value(supplier.contract_products)}",
        f"- География: {_list_value(supplier.regions)}",
        f"- Минимальный заказ: {_value(supplier.minimum_order)}",
        f"- Доставка: {_value(supplier.delivery)}",
        f"- Цена: {_value(supplier.display_price)}",
        f"- Документы: {_list_value(supplier.certificates)}",
        "",
        "### Сильные стороны",
        *strengths,
        "",
        "### Неизвестные параметры",
        _list_value(unknown) if unknown else "Все отслеживаемые поля заполнены.",
        "",
        "### Из чего собрана оценка",
        "```text",
        *(
            f"{item.points:+.0f} — {item.reason}"
            if item.points
            else f"  0 — {item.reason}"
            for item in scored.breakdown
        ),
        f"Итого: {scored.score:g}/100",
        "```",
        "",
        "### Источники",
        *sources,
    ]

    if supplier.evidence:
        lines += ["", "### Подтверждающие фрагменты"]
        for item in supplier.evidence:
            lines.append(
                f"- **{field_label(item.field_name)}** — «{item.quote}» "
                f"([источник]({item.source_url}), уверенность {item.confidence:g})"
            )

    return "\n".join(lines)


def run_status(result: SearchRunResult) -> str:
    """Короткая сводка запуска для правой колонки интерфейса."""
    stats = result.stats
    failed_note = f", не открылось {stats.pages_failed}" if stats.pages_failed else ""
    lines = [
        "**Готово**",
        "",
        f"- Запросов сформировано: {len(stats.queries_used)}",
        f"- Источников обработано: {stats.pages_processed}{failed_note}",
        f"- Из кэша или сохранённого набора: {stats.cache_hits}",
        f"- Дублей объединено: {stats.duplicates_merged}",
        f"- Поставщиков в выдаче: {len(result.suppliers)}",
        f"- Кандидатов для проверки: {len(result.candidates)}",
        f"- Ссылок из веб-поиска: {stats.web_hits_found}",
        f"- Исключено как нерелевантные: {stats.suppliers_excluded}",
        f"- Время обработки: {stats.duration_seconds:g} с",
        f"- Идентификатор запуска: `{stats.run_id}`",
    ]
    if stats.queries_used:
        lines += ["", "**Поисковые запросы**", *(f"- {q}" for q in stats.queries_used)]
    if stats.provider_stats:
        lines += [
            "",
            "**Источники поиска**",
            *(f"- {name}: {value}" for name, value in stats.provider_stats.items()),
        ]
    return "\n".join(lines)


def warnings_block(result: SearchRunResult) -> str:
    """Предупреждения запуска. Пустая строка, если всё прошло без замечаний."""
    if not result.warnings:
        return ""
    return "\n".join(["**Предупреждения**", *(f"- {item}" for item in result.warnings)])


def excluded_block(result: SearchRunResult) -> str:
    """Список исключённых компаний с причиной."""
    if not result.excluded:
        return ""
    lines = ["**Исключены из выдачи**"]
    lines += [
        f"- {scored.supplier.name} — {scored.exclusion_reason}"
        for scored in result.excluded
    ]
    return "\n".join(lines)


def methodology(weights: ScoringWeights) -> str:
    """Описание правил рейтинга, работы с пробелами и ограничений сервиса."""
    return f"""## Как считается оценка

Оценку считает формула, а не языковая модель. Каждый балл привязан к факту,
найденному на странице-источнике, и попадает в расшифровку в карточке компании.

| Критерий | Баллы |
|---|---:|
| Ассортимент подтверждает нужную категорию | +{weights.category_match:g} |
| Подтверждена работа в нужном регионе | +{weights.region_match:g} |
| Опубликованы условия доставки | +{weights.delivery_available:g} |
| Указан минимальный заказ | +{weights.minimum_order_found:g} |
| Минимальный заказ укладывается в объём запроса | +{weights.minimum_order_fits:g} |
| Опубликованы сведения о документах | +{weights.certificates_found:g} |
| Найдены прямые контакты | +{weights.direct_contacts:g} |
| Опубликована цена | +{weights.price_published:g} |

| Штраф | Баллы |
|---|---:|
| Работа в регионе не подтверждена | {weights.penalty_region_unconfirmed:g} |
| Сведения только из каталога организаций | {weights.penalty_secondary_source_only:g} |
| Страницы поставщика не открылись | {weights.penalty_page_unavailable:g} |

В живом режиме компания без точного подтверждения товара не исчезает: она
показывается в отдельной таблице кандидатов с причиной проверки. Розничные компании,
маркетплейсы и явно нерелевантные результаты исключаются и показываются отдельным
списком, чтобы решение можно было проверить.

Если совпала только часть слов запроса, за категорию начисляется половина баллов
и это отмечается в расшифровке.

Веса вынесены в конфигурацию (`src/config.py`), поэтому правила оценки можно
менять без изменения логики расчёта.

## Работа с отсутствующими сведениями

Незаполненное поле остаётся пустым и помечается как «{MISSING}». Сервис не
подставляет средние значения и не достраивает условия по косвенным признакам:
для закупщика ошибочная цена хуже, чем её отсутствие.

Каждое содержательное значение сопровождается ссылкой на страницу и дословной
цитатой. Если цитаты нет, значение считается неподтверждённым.

## Ограничения

- Сервис читает только публично доступные страницы. Разделы, требующие входа,
  и страницы с защитой от автоматических запросов не обрабатываются.
- Сведения о документах означают, что компания упоминает их на сайте.
  Действительность сертификатов в государственных реестрах не проверяется.
- Цены и условия на сайтах устаревают: в карточке всегда указана дата проверки.
- Юридический статус компании и её платёжеспособность не оцениваются.
- Итоговый список — это первичная подборка для звонка, а не готовое решение
  о закупке.
"""
