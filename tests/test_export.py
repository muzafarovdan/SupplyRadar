"""Тесты таблиц и выгрузки в CSV."""

from __future__ import annotations

import pandas as pd

from src.export import (
    MISSING,
    TABLE_COLUMNS,
    export_csv,
    to_comparison_table,
    to_export_frame,
    to_table,
)
from src.models import ScoredSupplier, ScoreItem


def _scored(supplier, score: float = 75.0) -> ScoredSupplier:
    return ScoredSupplier(
        supplier=supplier,
        score=score,
        breakdown=[ScoreItem(points=30, reason="ассортимент подтверждён")],
        missing_fields=["price", "inn"],
    )


def test_table_marks_missing_values(make_supplier):
    table = to_table([_scored(make_supplier())])

    assert list(table.columns) == list(TABLE_COLUMNS)
    assert table.loc[0, "Цена"] == MISSING
    assert table.loc[0, "Документы"] == MISSING
    assert table.loc[0, "Проверено"] == "10.09.2026"


def test_empty_table_keeps_columns():
    assert list(to_table([]).columns) == list(TABLE_COLUMNS)


def test_comparison_table_puts_companies_in_columns(make_supplier):
    first = _scored(make_supplier(id="a", name="Морозко"))
    second = _scored(make_supplier(id="b", name="Хладокомбинат"))

    table = to_comparison_table([first, second])

    assert list(table.columns) == ["Критерий", "Морозко", "Хладокомбинат"]
    assert "Компания" not in table["Критерий"].tolist()


def test_export_frame_includes_sources_and_breakdown(make_supplier):
    supplier = make_supplier(
        website="https://morozko.ru",
        source_urls=["https://morozko.ru", "https://morozko.ru/dostavka"],
    )

    frame = to_export_frame([_scored(supplier)])

    assert "https://morozko.ru/dostavka" in frame.loc[0, "Источники"]
    assert "ассортимент подтверждён" in frame.loc[0, "Расшифровка оценки"]
    assert frame.loc[0, "Незаполненные поля"] == "цена, ИНН"


def test_export_csv_is_readable(make_supplier, tmp_path):
    path = export_csv([_scored(make_supplier())], tmp_path, run_id="test-run")

    assert path.exists()
    restored = pd.read_csv(path, encoding="utf-8-sig")
    assert restored.loc[0, "Компания"] == "Тестовый поставщик"
    assert restored.loc[0, "Оценка"] == 75.0
