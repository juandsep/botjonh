import sys
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from assistant.context import ToolContext
from assistant.services import budgets, sheets

D = Decimal
CTX = ToolContext(
    "42", "owner", "USD", "America/Panama", 1,
    datetime(2026, 9, 29, 12, tzinfo=ZoneInfo("America/Panama")),
)  # fmt: skip


def test_503020_picks_bucket_with_largest_excess() -> None:
    gastos = {"restaurantes": D("250"), "viajes": D("100"), "vivienda": D("520")}
    # Income 1000: necesidades cap 500 (+20), ocio cap 300 (+50).
    assert budgets.mayor_exceso(gastos, None, D("1000")) == (
        "ocio",
        D("350"),
        D("300.00"),
    )


def test_503020_saving_more_is_not_an_excess() -> None:
    assert budgets.mayor_exceso({"ahorro": D("900")}, None, D("1000")) is None


def test_per_category_budget_wins_and_prorates() -> None:
    gastos = {"supermercado": D("120"), "restaurantes": D("90")}
    presupuesto = {"supermercado": "100", "restaurantes": "60"}
    assert budgets.mayor_exceso(gastos, presupuesto, D("0")) == (
        "restaurantes",
        D("90"),
        D("60.00"),
    )
    assert budgets.mayor_exceso(gastos, presupuesto, D(0), factor=D(2)) is None


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = MagicMock()
    mod.get_preferences.return_value = {}
    monkeypatch.setitem(sys.modules, "assistant.services.state", mod)
    return mod


def test_recomendar_messages(monkeypatch: pytest.MonkeyPatch, prefs: MagicMock) -> None:
    calls: list[tuple[date, date]] = []

    def gastos(chat_id: str, desde: date, hasta: date) -> dict[str, Decimal]:
        calls.append((desde, hasta))
        return {"restaurantes": D("400.00")}

    ingresos = MagicMock(return_value=D("0.00"))
    monkeypatch.setattr(sheets, "gastos_por_categoria", gastos)
    monkeypatch.setattr(sheets, "total_ingresos", ingresos)
    assert budgets.recomendar_presupuesto(CTX) == (
        "Sin presupuesto ni ingresos del mes para comparar."
    )
    assert calls[-1] == (date(2026, 9, 1), date(2026, 9, 29))
    ingresos.return_value = D("1000.00")
    assert budgets.recomendar_presupuesto(CTX).startswith(
        "Exceso en ocio (50/30/20): 400.00 de 300.00 USD (+100.00)."
    )
    prefs.get_preferences.return_value = {"presupuesto": {"restaurantes": "500"}}
    assert budgets.recomendar_presupuesto(CTX) == "Dentro del presupuesto."
    # A week pro-rates the monthly cap: 500 * 2/30 over Mon-Tue.
    assert budgets.recomendar_presupuesto(CTX, "semana").startswith(
        "Exceso en restaurantes: 400.00 de 33.33 USD"
    )
