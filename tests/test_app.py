"""Проверки явного выбора карточки в интерфейсе."""

from __future__ import annotations

from datetime import datetime, timezone

from app import (
    CARD_PLACEHOLDER,
    _card_choices,
    _launch_network,
    build_interface,
    show_selected_card,
)
from src.models import (
    RunStats,
    ScoredSupplier,
    SearchRequest,
    SearchRunResult,
    Supplier,
    VerificationStatus,
)


def _result() -> SearchRunResult:
    now = datetime.now(timezone.utc)
    verified = ScoredSupplier(
        supplier=Supplier(id="one", name="Поставщик", checked_at=now)
    )
    candidate = ScoredSupplier(
        supplier=Supplier(id="two", name="Кандидат", checked_at=now),
        verification_status=VerificationStatus.CANDIDATE,
    )
    return SearchRunResult(
        request=SearchRequest(category="филе"),
        suppliers=[verified],
        candidates=[candidate],
        stats=RunStats(run_id="run", started_at=now),
    )


def test_explicit_card_selector_contains_both_result_groups():
    result = _result()

    assert _card_choices(result) == [
        ("Подтверждён · Поставщик", "supplier:0"),
        ("Кандидат · Кандидат", "candidate:0"),
    ]
    assert "## Поставщик" in show_selected_card(result, "supplier:0")
    assert "## Кандидат" in show_selected_card(result, "candidate:0")
    assert show_selected_card(result, "supplier:99") == CARD_PLACEHOLDER


def test_interface_builds_with_card_button():
    assert build_interface() is not None


def test_render_port_is_used(monkeypatch):
    monkeypatch.setenv("PORT", "10000")

    assert _launch_network() == ("0.0.0.0", 10000)


def test_local_launch_keeps_gradio_defaults(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)

    assert _launch_network() == (None, None)
