import sys
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from assistant.context import ToolContext
from assistant.services import sheets

PANAMA = ZoneInfo("America/Panama")
HEADER = [
    "fecha", "monto", "moneda", "categoria", "nota",
    "batch_id", "update_id", "tipo", "chat_id",
]  # fmt: skip


def make_ctx(update_id: int = 100, chat_id: str = "42") -> ToolContext:
    ahora = datetime(2026, 9, 29, 12, tzinfo=PANAMA)
    return ToolContext(chat_id, "owner", "USD", "America/Panama", update_id, ahora)


class FakeSheets:
    """Stand-in for build("sheets", "v4"): values().get / values().append."""

    def __init__(self) -> None:
        self.data: dict[str, list[list[str]]] = {
            "Gastos": [HEADER],
            "Ingresos": [HEADER[:3] + ["fuente"] + HEADER[4:]],
        }
        self.appends: list[dict] = []

    def spreadsheets(self) -> "FakeSheets":
        return self

    def values(self) -> "FakeSheets":
        return self

    def get(self, spreadsheetId: str, range: str) -> SimpleNamespace:
        rows = [list(r) for r in self.data[range.split("!")[0]]]
        return SimpleNamespace(execute=lambda: {"values": rows})

    def append(self, **kw: object) -> SimpleNamespace:
        self.appends.append(kw)
        hoja = str(kw["range"]).split("!")[0]
        self.data[hoja].extend(kw["body"]["values"])  # type: ignore[index]
        return SimpleNamespace(execute=lambda: {})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSheets:
    svc = FakeSheets()
    monkeypatch.setattr(sheets, "_service", lambda: svc)
    return svc


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = MagicMock()
    monkeypatch.setitem(sys.modules, "assistant.services.state", mod)
    return mod


ITEMS = [
    {"monto": 2, "categoria": "supermercado", "nota": "pan"},
    {"monto": 3.005, "categoria": "supermercado"},
]


def test_gasto_writes_decimal_strings(fake: FakeSheets, state: MagicMock) -> None:
    out = sheets.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    assert out == "✓ 2.00 USD → supermercado; ✓ 3.01 USD → supermercado"
    rows = fake.data["Gastos"][1:]
    assert [r[1] for r in rows] == ["2.00", "3.01"]
    assert all(isinstance(c, str) for r in rows for c in r)
    assert rows[0] == [
        "2026-09-29", "2.00", "USD", "supermercado", "pan",
        "g100", "100", "registro", "42",
    ]  # fmt: skip
    assert fake.appends[0]["valueInputOption"] == "RAW"
    state.set_last_batch.assert_called_with("42", "g100")


def test_repeated_update_id_does_not_duplicate(
    fake: FakeSheets, state: MagicMock
) -> None:
    sheets.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    sheets.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    assert len(fake.data["Gastos"]) == 3
    assert len(fake.appends) == 1
    # Partial write: only the missing item index is appended.
    fake.data["Gastos"].pop()
    sheets.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    assert [r[1] for r in fake.data["Gastos"][1:]] == ["2.00", "3.01"]
    # Another chat with the same update_id is not affected.
    sheets.registrar_gasto(make_ctx(chat_id="7"), ITEMS[:1], "USD", date(2026, 9, 29))
    assert len(fake.data["Gastos"]) == 4


def test_ingreso_idempotent(fake: FakeSheets, state: MagicMock) -> None:
    for _ in range(2):
        out = sheets.registrar_ingreso(
            make_ctx(), Decimal("1000"), "USD", "salario", date(2026, 9, 1)
        )
    assert out == "✓ +1000.00 USD ← salario"
    assert len(fake.data["Ingresos"]) == 2
    state.set_last_batch.assert_called_with("42", "i100")


def test_rejects_non_positive(fake: FakeSheets, state: MagicMock) -> None:
    with pytest.raises(ValueError):
        sheets.registrar_gasto(
            make_ctx(), [{"monto": 0, "categoria": "otros"}], "USD", date(2026, 9, 29)
        )
    with pytest.raises(ValueError):
        sheets.registrar_ingreso(
            make_ctx(), Decimal("-1"), "USD", "x", date(2026, 9, 29)
        )
    assert not fake.appends


def test_deshacer_appends_reverso_and_never_edits(
    fake: FakeSheets, state: MagicMock
) -> None:
    sheets.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    before = [list(r) for r in fake.data["Gastos"]]
    state.last_batch.return_value = "g100"
    assert sheets.deshacer(make_ctx(update_id=101)) == "↩ deshecho: 2 fila(s)"
    rows = fake.data["Gastos"]
    assert rows[: len(before)] == before  # existing rows untouched
    assert [(r[1], r[5], r[6], r[7]) for r in rows[3:]] == [
        ("-2.00", "g100", "101", "reverso"),
        ("-3.01", "g100", "101", "reverso"),
    ]
    # Retry of the same update: success, no new rows. Another update: refused.
    assert sheets.deshacer(make_ctx(update_id=101)) == "↩ deshecho: 2 fila(s)"
    assert sheets.deshacer(make_ctx(update_id=102), "g100") == (
        "Ese lote ya estaba deshecho."
    )
    assert len(rows) == 5


def test_deshacer_edge_cases(fake: FakeSheets, state: MagicMock) -> None:
    state.last_batch.return_value = None
    assert sheets.deshacer(make_ctx()) == "Nada que deshacer."
    assert sheets.deshacer(make_ctx(), "g999") == "Lote no encontrado."
    sheets.registrar_ingreso(make_ctx(), Decimal(5), "USD", "venta", date(2026, 9, 29))
    # Other chats cannot undo this batch.
    assert sheets.deshacer(make_ctx(chat_id="7"), "i100") == "Lote no encontrado."
    assert sheets.deshacer(make_ctx(update_id=101), "i100") == "↩ deshecho: 1 fila(s)"
    assert fake.data["Ingresos"][-1][1] == "-5.00"


def test_resumen_includes_reversos(fake: FakeSheets, state: MagicMock) -> None:
    sheets.registrar_gasto(
        make_ctx(update_id=1),
        [{"monto": "45", "categoria": "restaurantes"}],
        "USD",
        date(2026, 9, 29),
    )
    sheets.registrar_gasto(make_ctx(update_id=2), ITEMS, "USD", date(2026, 9, 28))
    sheets.registrar_gasto(make_ctx(update_id=3), ITEMS, "USD", date(2026, 8, 31))
    sheets.registrar_ingreso(
        make_ctx(update_id=4), Decimal(900), "USD", "salario", date(2026, 9, 1)
    )
    sheets.deshacer(make_ctx(update_id=5), "g2")
    assert sheets.resumen_finanzas(make_ctx(), "mes") == (
        "mes: gastos 45.00 USD, ingresos 900.00 USD; mayor restaurantes 45.00"
    )
    assert sheets.gastos_por_categoria("42", date(2026, 9, 28), date(2026, 9, 28)) == {
        "supermercado": Decimal("0.00")
    }
    assert sheets.resumen_finanzas(make_ctx(chat_id="7"), "hoy") == (
        "hoy: gastos 0.00 USD, ingresos 0.00 USD"
    )


def test_rango() -> None:
    tue = date(2026, 9, 29)
    assert sheets.rango("hoy", tue) == (tue, tue)
    assert sheets.rango("semana", tue) == (date(2026, 9, 28), tue)
    assert sheets.rango("mes", tue) == (date(2026, 9, 1), tue)
    with pytest.raises(ValueError):
        sheets.rango("año", tue)


def test_q_rounds_half_up_without_float() -> None:
    assert sheets.q(0.1 + 0.2) == Decimal("0.30")
    assert sheets.q("2.675") == Decimal("2.68")
    assert str(sheets.q(Decimal("10"))) == "10.00"


def test_service_is_lazy_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    sheets._service.cache_clear()
    auth = MagicMock(return_value=("creds", "p"))
    build = MagicMock()
    monkeypatch.setattr(sheets.google.auth, "default", auth)
    monkeypatch.setattr(sheets, "build", build)
    assert sheets._service() is sheets._service()
    auth.assert_called_once_with(scopes=[sheets.SCOPE])
    build.assert_called_once()
    sheets._service.cache_clear()
