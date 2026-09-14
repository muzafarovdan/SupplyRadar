"""Gradio-интерфейс сервиса поиска поставщиков продуктов питания.

Модуль отвечает только за интерфейс и привязку событий: подготовку данных
выполняют ``src.pipeline`` и ``src.report``.
"""

from __future__ import annotations

import logging
from queue import Queue
from threading import Thread

import gradio as gr

from src.config import get_settings
from src.export import (
    CANDIDATE_COLUMNS,
    SOURCE_COLUMNS,
    TABLE_COLUMNS,
    export_csv,
    to_candidate_table,
    to_comparison_table,
    to_sources_table,
    to_table,
)
from src.models import SearchRequest, SearchRunResult
from src.pipeline import STAGES, SupplierSearchPipeline
from src.report import (
    excluded_block,
    methodology,
    run_status,
    supplier_card,
    warnings_block,
)
from src.storage import Storage

logger = logging.getLogger(__name__)

CARD_PLACEHOLDER = (
    "Выберите компанию в списке на этой вкладке и нажмите «Открыть карточку»."
)
COMPARE_HINT = "Выберите от 2 до 4 компаний и нажмите «Сравнить»."
EMPTY_RESULT = (
    "### Подходящих поставщиков не найдено\n\n"
    "Попробуйте изменить формулировку категории, убрать регион или "
    "снизить требования к минимальному объёму."
)
CANDIDATE_RESULT = (
    "Точный товар пока не подтверждён, но найдены компании для проверки. "
    "Откройте карточку кандидата ниже."
)

_settings = get_settings()
_pipeline = SupplierSearchPipeline(
    settings=_settings, storage=Storage(_settings.database_path)
)

logging.basicConfig(
    level=_settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _build_request(
    category: str,
    region: str,
    volume_kg: float | None,
    delivery_required: bool,
    documents_required: bool,
    extra_requirements: str,
    eis_queries_text: str,
    max_results: float,
) -> SearchRequest:
    return SearchRequest(
        category=(category or "").strip(),
        region=(region or "").strip() or None,
        required_volume_kg=volume_kg or None,
        delivery_required=delivery_required,
        documents_required=documents_required,
        extra_requirements=(extra_requirements or "").strip() or None,
        eis_queries=[
            value.strip()
            for value in (eis_queries_text or "").split("|")
            if value.strip()
        ],
        max_results=int(max_results),
    )


def _progress_markdown(done: list[str]) -> str:
    """Показывает пройденные этапы и текущий."""
    lines = ["**Обработка**", "", "```text"]
    lines += done
    lines += ["```"]
    return "\n".join(lines)


def _run_pipeline(request: SearchRequest):
    """Выполняет поиск в отдельном потоке, отдавая сообщения о прогрессе."""
    messages: Queue[str | None] = Queue()
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            outcome["result"] = _pipeline.run(request, progress=messages.put)
        except Exception as error:  # интерфейс не должен падать вместе с поиском
            logger.exception("Поиск завершился ошибкой")
            outcome["error"] = error
        finally:
            messages.put(None)

    thread = Thread(target=worker, daemon=True)
    thread.start()

    reported: list[str] = []
    while True:
        message = messages.get()
        if message is None:
            break
        reported.append(message)
        yield reported, None

    thread.join()
    yield reported, outcome


def search(
    category: str,
    region: str,
    volume_kg: float | None,
    delivery_required: bool,
    documents_required: bool,
    extra_requirements: str,
    eis_queries_text: str,
    max_results: float,
):
    """Обработчик кнопки поиска. Обновляет статус по мере прохождения этапов."""
    if not (category or "").strip():
        yield (
            "**Укажите категорию товара**",
            *[gr.skip()] * 11,
        )
        return

    request = _build_request(
        category,
        region,
        volume_kg,
        delivery_required,
        documents_required,
        extra_requirements,
        eis_queries_text,
        max_results,
    )

    outcome: dict[str, object] = {}
    for reported, current in _run_pipeline(request):
        if current is None:
            yield (_progress_markdown(reported), *[gr.skip()] * 11)
        else:
            outcome = current

    if "error" in outcome:
        yield (
            f"**Поиск не выполнен**\n\n{outcome['error']}",
            *[gr.skip()] * 11,
        )
        return

    result: SearchRunResult = outcome["result"]  # type: ignore[assignment]
    names = [scored.supplier.name for scored in result.suppliers]
    card_choices = _card_choices(result)

    export_items = [*result.suppliers, *result.candidates]
    if export_items:
        csv_path = export_csv(export_items, _settings.export_dir, result.stats.run_id)
        download = gr.update(value=str(csv_path), interactive=True)
        selected_card = card_choices[0][1]
        card = show_selected_card(result, selected_card)
    else:
        download = gr.update(value=None, interactive=False)
        card = EMPTY_RESULT
        selected_card = None

    yield (
        run_status(result),
        warnings_block(result),
        to_table(result.suppliers),
        to_candidate_table(result.candidates),
        excluded_block(result),
        to_sources_table(result.sources),
        download,
        gr.update(
            choices=card_choices,
            value=selected_card,
            interactive=bool(card_choices),
        ),
        card,
        gr.update(choices=names, value=[]),
        to_comparison_table([]),
        result,
    )


def _card_choices(result: SearchRunResult) -> list[tuple[str, str]]:
    choices = [
        (f"Подтверждён · {item.supplier.name}", f"supplier:{index}")
        for index, item in enumerate(result.suppliers)
    ]
    choices.extend(
        (f"Кандидат · {item.supplier.name}", f"candidate:{index}")
        for index, item in enumerate(result.candidates)
    )
    return choices


def show_selected_card(result: SearchRunResult | None, selection: str | None) -> str:
    """Открывает карточку по явному выбору, не полагаясь на клик Dataframe."""
    if result is None or not selection or ":" not in selection:
        return CARD_PLACEHOLDER
    group, raw_index = selection.split(":", 1)
    try:
        index = int(raw_index)
    except ValueError:
        return CARD_PLACEHOLDER
    items = result.suppliers if group == "supplier" else result.candidates
    if group not in {"supplier", "candidate"} or not 0 <= index < len(items):
        return CARD_PLACEHOLDER
    return supplier_card(items[index])


def show_card(result: SearchRunResult | None, event: gr.SelectData):
    """Открывает карточку выбранной в таблице компании."""
    if result is None or not result.suppliers:
        return CARD_PLACEHOLDER, gr.skip()
    index = event.index[0] if isinstance(event.index, (list, tuple)) else event.index
    if not 0 <= index < len(result.suppliers):
        return CARD_PLACEHOLDER, gr.skip()
    return supplier_card(result.suppliers[index]), gr.update(value=f"supplier:{index}")


def show_candidate_card(result: SearchRunResult | None, event: gr.SelectData):
    """Открывает карточку кандидата, не смешивая его с подтверждённой выдачей."""
    if result is None or not result.candidates:
        return CARD_PLACEHOLDER, gr.skip()
    index = event.index[0] if isinstance(event.index, (list, tuple)) else event.index
    if not 0 <= index < len(result.candidates):
        return CARD_PLACEHOLDER, gr.skip()
    return supplier_card(result.candidates[index]), gr.update(
        value=f"candidate:{index}"
    )


def compare(result: SearchRunResult | None, names: list[str]):
    """Строит сравнительную таблицу по выбранным компаниям."""
    if result is None or not result.suppliers:
        return COMPARE_HINT, to_comparison_table([])
    if not 2 <= len(names) <= 4:
        return COMPARE_HINT, to_comparison_table([])

    selected = [scored for scored in result.suppliers if scored.supplier.name in names]
    return "", to_comparison_table(selected)


def build_interface() -> gr.Blocks:
    """Собирает интерфейс приложения."""
    with gr.Blocks(title="Поиск поставщиков продуктов питания") as demo:
        gr.Markdown(
            "# Поиск поставщиков продуктов питания\n"
            "Сервис собирает сведения о поставщиках из открытых источников, "
            "приводит их к общей структуре и сравнивает по прозрачным критериям. "
            "Каждое значение сопровождается ссылкой на источник, "
            "а незаполненные поля показываются явно."
        )

        state = gr.State(value=None)

        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown("### Параметры поиска")
                category = gr.Textbox(
                    label="Категория товара",
                    placeholder="замороженные сырники",
                    value="замороженные сырники",
                )
                region = gr.Textbox(
                    label="Город или регион доставки",
                    placeholder="Екатеринбург",
                    value="Екатеринбург",
                )
                volume = gr.Number(
                    label="Требуемый объём, кг", value=50, minimum=0, step=10
                )
                delivery_required = gr.Checkbox(label="Требуется доставка", value=True)
                documents_required = gr.Checkbox(
                    label="Требуются документы на продукцию", value=True
                )
                extra = gr.Textbox(
                    label="Дополнительные требования",
                    lines=3,
                    placeholder="желательно собственное производство",
                )
                eis_queries = gr.Textbox(
                    label="Дополнительные запросы по контрактам — необязательно",
                    placeholder=(
                        "филе куриное | грудка куриная без кости | 10.12.20.110"
                    ),
                    info=(
                        "До двух текстовых вариантов через |. Основная категория "
                        "всегда ищется автоматически."
                    ),
                )
                gr.Markdown(
                    "**Примеры для куриного филе:** "
                    "`филе грудки куриной | грудка куриная без кости`. "
                    "Город берётся из отдельного поля выше."
                )
                max_results = gr.Slider(
                    label="Максимум поставщиков",
                    minimum=3,
                    maximum=20,
                    step=1,
                    value=10,
                )
                search_button = gr.Button("Найти поставщиков", variant="primary")

            with gr.Column(scale=1):
                gr.Markdown("### Статус")
                status = gr.Markdown(
                    "Заполните параметры и нажмите «Найти поставщиков».\n\n"
                    "```text\n"
                    + "\n".join(
                        f"{index + 1}/{len(STAGES)} {stage}"
                        for index, stage in enumerate(STAGES)
                    )
                    + "\n```"
                )
                warnings = gr.Markdown()
                excluded = gr.Markdown()
                download = gr.DownloadButton(
                    "Скачать CSV", interactive=False, variant="secondary"
                )

        with gr.Tabs():
            with gr.Tab("Поставщики"):
                table = gr.Dataframe(
                    headers=list(TABLE_COLUMNS),
                    interactive=False,
                    wrap=True,
                    label="Подтверждённые поставщики",
                )
                gr.Markdown("### Кандидаты для проверки")
                candidate_table = gr.Dataframe(
                    headers=list(CANDIDATE_COLUMNS),
                    interactive=False,
                    wrap=True,
                    label="Точный товар ещё не подтверждён на открытой странице",
                )
            with gr.Tab("Карточка"):
                card_selector = gr.Dropdown(
                    label="Компания",
                    choices=[],
                    interactive=False,
                    info="Здесь доступны и подтверждённые поставщики, и кандидаты.",
                )
                open_card_button = gr.Button("Открыть карточку", variant="primary")
                card = gr.Markdown(CARD_PLACEHOLDER)
            with gr.Tab("Сравнение"):
                compare_choices = gr.CheckboxGroup(
                    label="Компании для сравнения (2-4)", choices=[]
                )
                compare_button = gr.Button("Сравнить")
                compare_message = gr.Markdown(COMPARE_HINT)
                compare_table = gr.Dataframe(interactive=False, wrap=True)
            with gr.Tab("Источники"):
                sources = gr.Dataframe(
                    headers=list(SOURCE_COLUMNS), interactive=False, wrap=True
                )
            with gr.Tab("Методика"):
                gr.Markdown(methodology(_settings.scoring))

        search_button.click(
            fn=search,
            inputs=[
                category,
                region,
                volume,
                delivery_required,
                documents_required,
                extra,
                eis_queries,
                max_results,
            ],
            outputs=[
                status,
                warnings,
                table,
                candidate_table,
                excluded,
                sources,
                download,
                card_selector,
                card,
                compare_choices,
                compare_table,
                state,
            ],
        )
        table.select(fn=show_card, inputs=state, outputs=[card, card_selector])
        candidate_table.select(
            fn=show_candidate_card, inputs=state, outputs=[card, card_selector]
        )
        open_card_button.click(
            fn=show_selected_card,
            inputs=[state, card_selector],
            outputs=card,
        )
        card_selector.change(
            fn=show_selected_card,
            inputs=[state, card_selector],
            outputs=card,
        )
        compare_button.click(
            fn=compare,
            inputs=[state, compare_choices],
            outputs=[compare_message, compare_table],
        )

    return demo


def _launch_auth() -> tuple[str, str] | None:
    """Включает Basic Auth, только если заданы оба секрета развёртывания."""
    if _settings.app_username and _settings.app_password:
        return (_settings.app_username, _settings.app_password)
    return None


if __name__ == "__main__":
    build_interface().queue(default_concurrency_limit=1).launch(
        auth=_launch_auth(),
        show_api=False,
    )
