import copy
import operator
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

from assistant.context import ToolContext
from assistant.services import ledger

PANAMA = ZoneInfo("America/Panama")
OPS = {">=": operator.ge, "<": operator.lt, "==": operator.eq}


def make_ctx(update_id: int = 100, chat_id: str = "42") -> ToolContext:
    ahora = datetime(2026, 9, 29, 12, tzinfo=PANAMA)
    return ToolContext(chat_id, "owner", "USD", "America/Panama", update_id, ahora)


class Snap:
    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data)


class Ref:
    def __init__(self, db: "FakeDB", path: str) -> None:
        self.db, self.path = db, path

    def collection(self, name: str) -> "Query":
        return Query(self.db, f"{self.path}/{name}")


class Query:
    """Collection or query: document(), where(filter=FieldFilter), stream()."""

    def __init__(self, db: "FakeDB", path: str, filters: tuple = ()) -> None:
        self.db, self.path, self.filters = db, path, filters

    def document(self, doc_id: str) -> Ref:
        return Ref(self.db, f"{self.path}/{doc_id}")

    def where(self, *, filter: firestore.FieldFilter) -> "Query":
        return Query(self.db, self.path, (*self.filters, filter))

    def stream(self) -> list[Snap]:
        return [
            Snap(data)
            for path, data in sorted(self.db.store.items())
            if path.rsplit("/", 1)[0] == self.path
            and all(OPS[f.op_string](data[f.field_path], f.value) for f in self.filters)
        ]


class Batch:
    def __init__(self, db: "FakeDB") -> None:
        self.db, self.ops = db, []  # type: list[tuple[str, dict]]

    def create(self, ref: Ref, data: dict) -> None:
        self.ops.append((ref.path, data))

    def commit(self) -> None:
        if any(path in self.db.store for path, _ in self.ops):
            raise AlreadyExists("exists")  # atomic: nothing is written
        for path, data in self.ops:
            assert data["creado"] is firestore.SERVER_TIMESTAMP
            self.db.store[path] = {**copy.deepcopy(data), "creado": self.db.now}
        self.db.commits += 1


class FakeDB:
    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.commits = 0
        self.now = datetime(2026, 9, 29, 17, tzinfo=UTC)

    def collection(self, name: str) -> Query:
        return Query(self, name)

    def batch(self) -> Batch:
        return Batch(self)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr(ledger, "_db", lambda: fake)
    return fake


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = MagicMock()
    monkeypatch.setitem(sys.modules, "assistant.services.state", mod)
    return mod


ITEMS = [
    {"monto": 2, "categoria": "supermercado", "nota": "pan"},
    {"monto": 3.005, "categoria": "supermercado"},
]
BASE = "ledger/42/movimientos"


def test_gasto_writes_decimal_strings(db: FakeDB, state: MagicMock) -> None:
    out = ledger.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    assert out == "✓ 2.00 USD → supermercado; ✓ 3.01 USD → supermercado"
    assert sorted(db.store) == [f"{BASE}/100-0", f"{BASE}/100-1"]
    assert db.store[f"{BASE}/100-0"] == {
        "fecha": "2026-09-29",
        "monto": "2.00",
        "moneda": "USD",
        "categoria": "supermercado",
        "tipo_mov": "gasto",
        "nota": "pan",
        "batch_id": "g100",
        "update_id": 100,
        "tipo": "registro",
        "creado": db.now,
    }
    assert db.store[f"{BASE}/100-1"]["monto"] == "3.01"
    state.set_last_batch.assert_called_with("42", "g100")


def test_retry_of_same_update_writes_nothing(db: FakeDB, state: MagicMock) -> None:
    first = ledger.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    before = copy.deepcopy(db.store)
    again = ledger.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    assert again == first
    assert db.store == before and db.commits == 1
    assert state.set_last_batch.call_count == 2
    # Another chat with the same update_id is not affected.
    ledger.registrar_gasto(make_ctx(chat_id="7"), ITEMS[:1], "USD", date(2026, 9, 29))
    assert "ledger/7/movimientos/100-0" in db.store


def test_ingreso_idempotent(db: FakeDB, state: MagicMock) -> None:
    for _ in range(2):
        out = ledger.registrar_ingreso(
            make_ctx(), Decimal("1000"), "USD", "salario", date(2026, 9, 1), "sep"
        )
    assert out == "✓ +1000.00 USD ← salario"
    assert list(db.store) == [f"{BASE}/100-i0"]
    doc = db.store[f"{BASE}/100-i0"]
    assert (doc["fuente"], doc["tipo_mov"], doc["nota"]) == (
        "salario",
        "ingreso",
        "sep",
    )
    state.set_last_batch.assert_called_with("42", "i100")


def test_rejects_non_positive(db: FakeDB, state: MagicMock) -> None:
    with pytest.raises(ValueError):
        ledger.registrar_gasto(
            make_ctx(), [{"monto": 0, "categoria": "otros"}], "USD", date(2026, 9, 29)
        )
    with pytest.raises(ValueError):
        ledger.registrar_ingreso(
            make_ctx(), Decimal("-1"), "USD", "x", date(2026, 9, 29)
        )
    assert not db.store


def test_deshacer_appends_reverso_and_never_edits(db: FakeDB, state: MagicMock) -> None:
    ledger.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    before = copy.deepcopy(db.store)
    state.last_batch.return_value = "g100"
    assert ledger.deshacer(make_ctx(update_id=101)) == "↩ deshecho: 2 fila(s)"
    assert {k: db.store[k] for k in before} == before  # originals untouched
    rev = db.store[f"{BASE}/g100-r0"]
    assert (rev["monto"], rev["batch_id"], rev["update_id"], rev["tipo"]) == (
        "-2.00",
        "g100",
        101,
        "reverso",
    )
    assert db.store[f"{BASE}/g100-r1"]["monto"] == "-3.01"
    # Retry of the same update: success, no new docs. Another update: refused.
    assert ledger.deshacer(make_ctx(update_id=101)) == "↩ deshecho: 2 fila(s)"
    assert ledger.deshacer(make_ctx(update_id=102), "g100") == (
        "Ese lote ya estaba deshecho."
    )
    assert len(db.store) == 4 and db.commits == 2


def test_deshacer_race_reports_already_undone(
    db: FakeDB, state: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.registrar_gasto(make_ctx(), ITEMS[:1], "USD", date(2026, 9, 29))
    monkeypatch.setattr(ledger, "_crear", lambda chat_id, docs: False)
    assert ledger.deshacer(make_ctx(update_id=101), "g100") == (
        "Ese lote ya estaba deshecho."
    )


def test_deshacer_edge_cases(db: FakeDB, state: MagicMock) -> None:
    state.last_batch.return_value = None
    assert ledger.deshacer(make_ctx()) == "Nada que deshacer."
    assert ledger.deshacer(make_ctx(), "g999") == "Lote no encontrado."
    ledger.registrar_ingreso(make_ctx(), Decimal(5), "USD", "venta", date(2026, 9, 29))
    # Other chats cannot undo this batch.
    assert ledger.deshacer(make_ctx(chat_id="7"), "i100") == "Lote no encontrado."
    assert ledger.deshacer(make_ctx(update_id=101), "i100") == "↩ deshecho: 1 fila(s)"
    assert db.store[f"{BASE}/i100-r0"]["monto"] == "-5.00"
    assert db.store[f"{BASE}/i100-r0"]["fuente"] == "venta"


def test_resumen_includes_reversos(db: FakeDB, state: MagicMock) -> None:
    ledger.registrar_gasto(
        make_ctx(update_id=1),
        [{"monto": "45", "categoria": "restaurantes"}],
        "USD",
        date(2026, 9, 29),
    )
    ledger.registrar_gasto(make_ctx(update_id=2), ITEMS, "USD", date(2026, 9, 28))
    ledger.registrar_gasto(make_ctx(update_id=3), ITEMS, "USD", date(2026, 8, 31))
    ledger.registrar_ingreso(
        make_ctx(update_id=4), Decimal(900), "USD", "salario", date(2026, 9, 1)
    )
    ledger.deshacer(make_ctx(update_id=5), "g2")
    assert ledger.resumen_finanzas(make_ctx(), "mes") == (
        "mes: gastos 45.00 USD, ingresos 900.00 USD; mayor restaurantes 45.00"
    )
    assert ledger.gastos_por_categoria("42", date(2026, 9, 28), date(2026, 9, 28)) == {
        "supermercado": Decimal("0.00")
    }
    assert ledger.total_ingresos("42", date(2026, 9, 2), date(2026, 9, 29)) == 0
    assert ledger.resumen_finanzas(make_ctx(chat_id="7"), "hoy") == (
        "hoy: gastos 0.00 USD, ingresos 0.00 USD"
    )


def test_movimientos_by_creado(db: FakeDB, state: MagicMock) -> None:
    ledger.registrar_gasto(make_ctx(), ITEMS[:1], "USD", date(2026, 1, 1))
    dia = datetime(2026, 9, 29, tzinfo=PANAMA)
    docs = ledger.movimientos("42", "creado", dia, dia.replace(day=30))
    assert [d["fecha"] for d in docs] == ["2026-01-01"]  # backdated, written today
    assert (
        ledger.movimientos(
            "42", "creado", dia.replace(day=30), datetime(2027, 1, 1, tzinfo=UTC)
        )
        == []
    )


def test_rango() -> None:
    tue = date(2026, 9, 29)
    assert ledger.rango("hoy", tue) == (tue, tue)
    assert ledger.rango("semana", tue) == (date(2026, 9, 28), tue)
    assert ledger.rango("mes", tue) == (date(2026, 9, 1), tue)
    with pytest.raises(ValueError):
        ledger.rango("año", tue)


def test_q_rounds_half_up_without_float() -> None:
    assert ledger.q(0.1 + 0.2) == Decimal("0.30")
    assert ledger.q("2.675") == Decimal("2.68")
    assert ledger.q("-0.005") == Decimal("-0.01")
    assert str(ledger.q(Decimal("10"))) == "10.00"


def test_db_is_lazy_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger._db.cache_clear()
    client = MagicMock()
    monkeypatch.setattr(ledger.firestore, "Client", client)
    assert ledger._db() is ledger._db()
    client.assert_called_once()
    ledger._db.cache_clear()
