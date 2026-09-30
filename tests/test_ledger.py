import copy
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from firestore_fake import FakeDB

from assistant.context import ToolContext
from assistant.services import fx, ledger

PANAMA = ZoneInfo("America/Panama")


def make_ctx(update_id: int = 100, chat_id: str = "42") -> ToolContext:
    ahora = datetime(2026, 9, 29, 12, tzinfo=PANAMA)
    return ToolContext(chat_id, "owner", "USD", "America/Panama", update_id, ahora)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr(ledger, "_db", lambda: fake)
    monkeypatch.setattr(fx, "_db", lambda: fake)
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


@respx.mock  # USD: any HTTP call would fail as unmatched
def test_gasto_writes_decimal_strings(db: FakeDB, state: MagicMock) -> None:
    out = ledger.registrar_gasto(make_ctx(), ITEMS, "USD", date(2026, 9, 29))
    assert out == "−2.00 USD · pan; −3.01 USD · supermercado"
    assert sorted(db.store) == [f"{BASE}/100-0", f"{BASE}/100-1"]
    assert db.store[f"{BASE}/100-0"] == {
        "fecha": "2026-09-29",
        "monto": "2.00",
        "moneda": "USD",
        "monto_original": "2.00",
        "moneda_original": "USD",
        "tasa": "1",
        "fuente_tasa": "usd",
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
    assert out == "+1000.00 USD · sep"
    assert list(db.store) == [f"{BASE}/100-i0"]
    doc = db.store[f"{BASE}/100-i0"]
    assert (doc["fuente"], doc["tipo_mov"], doc["nota"]) == (
        "salario",
        "ingreso",
        "sep",
    )
    state.set_last_batch.assert_called_with("42", "i100")


def test_rejects_non_positive(db: FakeDB, state: MagicMock) -> None:
    out = ledger.registrar_gasto(
        make_ctx(), [{"monto": 0, "categoria": "otros"}], "USD", date(2026, 9, 29)
    )
    assert out == "El monto debe ser mayor que 0."
    out = ledger.registrar_ingreso(
        make_ctx(), Decimal("-1"), "USD", "x", date(2026, 9, 29)
    )
    assert out == "El monto debe ser mayor que 0."
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


TRM = "https://www.datos.gov.co/resource/32sa-8pi3.json"
ECB = "https://api.frankfurter.dev/v1/2026-09-29"


def trm(valor: str = "4081.63") -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {
                "valor": valor,
                "vigenciadesde": "2026-09-29T00:00:00.000",
                "vigenciahasta": "2026-09-29T00:00:00.000",
            }
        ],
    )


@respx.mock
def test_gasto_in_cop_converts_with_trm(db: FakeDB, state: MagicMock) -> None:
    route = respx.get(TRM).mock(return_value=trm())
    items = [{"monto": 2000, "categoria": "", "nota": "café"}]
    out = ledger.registrar_gasto(make_ctx(), items, "COP", date(2026, 9, 29))
    assert out == "−0.49 USD · café (2,000 COP)"
    params = route.calls.last.request.url.params
    assert params["$where"] == "vigenciadesde <= '2026-09-29T00:00:00'"
    assert (params["$order"], params["$limit"]) == ("vigenciadesde DESC", "1")
    doc = db.store[f"{BASE}/100-0"]
    assert {k: doc[k] for k in ("monto", "moneda", "categoria")} == {
        "monto": "0.49",
        "moneda": "USD",
        "categoria": "otros",
    }
    assert (doc["monto_original"], doc["moneda_original"]) == ("2000.00", "COP")
    assert (doc["tasa"], doc["fuente_tasa"]) == ("4081.63", "trm")
    assert db.store["fx/2026-09-29_COP"] == {"tasa": "4081.63", "fuente": "trm"}
    # Cached: a second conversion for the same day makes no HTTP call.
    ledger.registrar_gasto(make_ctx(101), items, "cop", date(2026, 9, 29))
    assert route.call_count == 1


@respx.mock
def test_ingreso_in_eur_uses_frankfurter(db: FakeDB, state: MagicMock) -> None:
    route = respx.get(ECB).mock(
        return_value=httpx.Response(200, json={"rates": {"EUR": 0.8}})
    )
    out = ledger.registrar_ingreso(
        make_ctx(), Decimal("12.5"), "EUR", "", date(2026, 9, 29)
    )
    assert out == "+15.63 USD (12.50 EUR)"
    assert dict(route.calls.last.request.url.params) == {
        "base": "USD",
        "symbols": "EUR",
    }
    doc = db.store[f"{BASE}/100-i0"]
    assert (doc["monto"], doc["tasa"], doc["fuente_tasa"]) == ("15.63", "0.8", "ecb")


@respx.mock
def test_fx_failure_writes_nothing(db: FakeDB, state: MagicMock) -> None:
    respx.get(TRM).mock(return_value=httpx.Response(503))
    respx.get(ECB).mock(side_effect=httpx.ConnectTimeout("slow"))
    items = [{"monto": 5, "categoria": "otros"}]
    assert ledger.registrar_gasto(make_ctx(), items, "COP", date(2026, 9, 29)) == (
        "No pude obtener la tasa de COP, intenta luego."
    )
    assert ledger.registrar_ingreso(
        make_ctx(), Decimal(1), "GBP", "x", date(2026, 9, 29)
    ) == ("No pude obtener la tasa de GBP, intenta luego.")
    assert ledger.registrar_gasto(make_ctx(), items, "XYZ", date(2026, 9, 29)) == (
        "Moneda no soportada."
    )
    assert not db.store
    state.set_last_batch.assert_not_called()


@respx.mock
def test_fx_bad_payloads_and_cache_errors(
    db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(TRM).mock(return_value=httpx.Response(200, json=[]))
    respx.get(ECB).mock(return_value=httpx.Response(200, json={"rates": {}}))
    dia = date(2026, 9, 29)
    for moneda in ("COP", "EUR"):
        with pytest.raises(fx.FxError, match="data"):
            fx.a_usd(Decimal(1), moneda, dia)
    respx.get(TRM).mock(return_value=trm("0"))
    with pytest.raises(fx.FxError, match="data"):
        fx.a_usd(Decimal(1), "COP", dia)
    assert fx.a_usd(Decimal("3"), "USD", dia) == (Decimal("3.00"), 1, "usd")

    def down() -> None:
        raise RuntimeError("firestore down")

    monkeypatch.setattr(fx, "_db", down)
    with pytest.raises(fx.FxError, match="cache"):
        fx.a_usd(Decimal(1), "COP", dia)


def test_fx_db_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    fx._db.cache_clear()
    client = MagicMock()
    monkeypatch.setattr(fx.firestore, "Client", client)
    assert fx._db() is fx._db()
    client.assert_called_once()
    fx._db.cache_clear()


def seed(db: FakeDB) -> None:
    """Three movements, newest last: gasto 2 (pan), ingreso 900, gasto 2000 COP."""
    ledger.registrar_gasto(make_ctx(1), ITEMS[:1], "USD", date(2026, 9, 28))
    ledger.registrar_ingreso(
        make_ctx(2), Decimal(900), "USD", "salario", date(2026, 9, 29)
    )
    db.store["fx/2026-09-30_COP"] = {"tasa": "4000", "fuente": "trm"}
    ledger.registrar_gasto(
        make_ctx(3), [{"monto": 2000, "nota": "café"}], "COP", date(2026, 9, 30)
    )


def test_ultimos_lists_newest_first_and_skips_reversed(
    db: FakeDB, state: MagicMock
) -> None:
    assert ledger.ultimos_texto(make_ctx()) == "Sin movimientos."
    seed(db)
    assert ledger.ultimos_texto(make_ctx()) == (
        "1) 30/09 −0.50 USD café (2,000 COP)\n"
        "2) 29/09 +900.00 USD salario\n"
        "3) 28/09 −2.00 USD pan"
    )
    primero = ledger.ultimos(make_ctx(), 1)
    assert primero == [
        {
            "indice": 1,
            "id": "3-0",
            "fecha": "2026-09-30",
            "tipo_mov": "gasto",
            "monto": "0.50",
            "monto_original": "2000.00",
            "moneda_original": "COP",
            "nota": "café",
            "categoria": "otros",
        }
    ]
    assert ledger.anular(make_ctx(10), 2) == "✓ anulado: +900.00 USD · salario"
    ledger.deshacer(make_ctx(11), "g1")
    assert [m["id"] for m in ledger.ultimos(make_ctx())] == ["3-0"]
    # Legacy reversos (no ``reversa``) cancel their whole batch.
    del db.store[f"{BASE}/g1-r0"]["reversa"]
    assert [m["id"] for m in ledger.ultimos(make_ctx())] == ["3-0"]


def test_editar_appends_reverso_and_new_registro(db: FakeDB, state: MagicMock) -> None:
    seed(db)
    before = copy.deepcopy(db.store)
    out = ledger.editar(make_ctx(20), 1, monto=Decimal(4000), categoria="restaurantes")
    assert out == "✓ editado: −1.00 USD · café (4,000 COP)"
    assert {k: db.store[k] for k in before} == before  # originals untouched
    rev = db.store[f"{BASE}/3-0-x"]
    assert (rev["tipo"], rev["reversa"], rev["monto"], rev["monto_original"]) == (
        "reverso",
        "3-0",
        "-0.50",
        "-2000.00",
    )
    nuevo = db.store[f"{BASE}/20-e0"]
    assert (nuevo["tipo"], nuevo["batch_id"], nuevo["categoria"]) == (
        "registro",
        "e20",
        "restaurantes",
    )
    assert (nuevo["monto"], nuevo["fecha"], nuevo["nota"]) == (
        "1.00",
        "2026-09-30",
        "café",
    )
    # Retry of the same update: same reply, nothing new, same target.
    commits = db.commits
    assert ledger.editar(make_ctx(20), 1, monto=Decimal(4000)) == out
    assert db.commits == commits
    assert ledger.gastos_por_categoria("42", date(2026, 9, 30), date(2026, 9, 30)) == {
        "otros": Decimal("0.00"),
        "restaurantes": Decimal("1.00"),
    }
    # Edit the ingreso's note only; its amount stays.
    out = ledger.editar(make_ctx(21), 2, nota="bono")
    assert out == "✓ editado: +900.00 USD · bono"
    assert ledger.editar(make_ctx(22), 1, categoria="") == (
        "✓ editado: +900.00 USD · bono"
    )
    assert db.store[f"{BASE}/22-e0"]["fuente"] == ""


def test_edit_errors(db: FakeDB, state: MagicMock) -> None:
    seed(db)
    for indice in (0, 4):
        assert ledger.editar(make_ctx(30), indice, nota="x") == (
            "No encontré ese movimiento."
        )
        assert ledger.anular(make_ctx(30), indice) == "No encontré ese movimiento."
    assert ledger.editar(make_ctx(30), 1, monto=Decimal(0)) == (
        "El monto debe ser mayor que 0."
    )
    assert ledger.editar(make_ctx(30), 1, moneda="XYZ") == "Moneda no soportada."
    n = len(db.store)
    assert ledger.anular(make_ctx(31)) == "✓ anulado: −0.50 USD · café (2,000 COP)"
    assert len(db.store) == n + 1
    assert ledger.anular(make_ctx(31)) == "✓ anulado: −0.50 USD · café (2,000 COP)"
    assert len(db.store) == n + 1  # retry


def test_edit_race_reports_not_found(
    db: FakeDB, state: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db)
    monkeypatch.setattr(ledger, "_crear", lambda chat_id, docs: False)
    assert ledger.anular(make_ctx(40)) == "No encontré ese movimiento."
    assert ledger.editar(make_ctx(40), nota="x") == "No encontré ese movimiento."


def test_totals_in_usd_for_any_user_currency(db: FakeDB, state: MagicMock) -> None:
    seed(db)
    ctx = ToolContext("42", "owner", "COP", "America/Panama", 50, make_ctx().ahora)
    assert ledger.resumen_finanzas(ctx, "mes") == (
        "mes: gastos 2.00 USD, ingresos 900.00 USD; mayor supermercado 2.00"
    )
