"""Finance ledger on Firestore: append-only, every amount in USD.

Layout: ``ledger/{chat_id}/movimientos/{doc_id}`` with the fields ``fecha``
(ISO date), ``monto`` (USD str, quantized to 0.01), ``moneda`` ("USD"),
``monto_original``, ``moneda_original``, ``tasa`` (moneda_original per 1 USD) and
``fuente_tasa`` (usd|trm|ecb) from ``services.fx``, ``categoria`` (gasto) or
``fuente`` (ingreso), ``tipo_mov`` (gasto|ingreso), ``nota``, ``batch_id``,
``update_id``, ``tipo`` (registro|reverso), ``reversa`` (reverso: the registro
doc id it cancels) and ``creado`` (server timestamp). Rows written before the
USD ledger lack the ``*_original``/``tasa`` fields.

Doc ids: gasto ``{update_id}-{i}``, ingreso ``{update_id}-i0``, deshacer reverso
``{batch_id}-r{i}``, editar/anular reverso ``{registro_id}-x`` and editar's new
registro ``{update_id}-e0`` (batch ``e{update_id}``). Every call creates its
docs with ``create()`` in one atomic batch, so a Pub/Sub retry of the same
update hits AlreadyExists and writes nothing. Nothing is ever edited or deleted.

Reads query one chat's subcollection by a single field (``fecha``, ``creado``,
``batch_id`` or ``update_id``): automatic single-field indexes, no composite
index. Sums run in Python with Decimal. Doc paths contain chat_ids: never log
them.
"""

from __future__ import annotations

import importlib
import logging
import os
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

from google.api_core.exceptions import Conflict
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from assistant.context import ToolContext

log = logging.getLogger(__name__)

CENT = Decimal("0.01")
POSITIVO = "El monto debe ser mayor que 0."
NO_ENCONTRADO = "No encontré ese movimiento."


@cache
def _db() -> firestore.Client:
    return firestore.Client(project=os.environ.get("GCP_PROJECT_ID") or None)


def _state() -> Any:
    return importlib.import_module("assistant.services.state")


def _col(chat_id: str) -> Any:
    return _db().collection("ledger").document(chat_id).collection("movimientos")


def q(value: object) -> Decimal:
    """Amount as Decimal rounded to cents; str() first so floats never leak."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def hoy(ctx: ToolContext) -> date:
    return ctx.ahora.astimezone(ZoneInfo(ctx.zona_horaria)).date()


def rango(periodo: str, dia: date) -> tuple[date, date]:
    """Inclusive date range for hoy|semana|mes ending on ``dia``."""
    if periodo == "hoy":
        return dia, dia
    if periodo == "semana":
        return dia - timedelta(days=dia.weekday()), dia
    if periodo == "mes":
        return dia.replace(day=1), dia
    raise ValueError("periodo")


def _crear(chat_id: str, docs: dict[str, dict]) -> bool:
    """Create all docs atomically. False when they already exist (a retry)."""
    col = _col(chat_id)
    batch = _db().batch()
    for doc_id, data in docs.items():
        batch.create(
            col.document(doc_id), {**data, "creado": firestore.SERVER_TIMESTAMP}
        )
    try:
        batch.commit()
    except Conflict:
        log.info("ledger_retry docs=%d", len(docs))
        return False
    log.info("ledger_write docs=%d", len(docs))
    return True


def movimientos(chat_id: str, campo: str, desde: Any, hasta: Any) -> list[dict]:
    """Docs of one chat with ``desde <= campo < hasta``."""
    consulta = (
        _col(chat_id)
        .where(filter=FieldFilter(campo, ">=", desde))
        .where(filter=FieldFilter(campo, "<", hasta))
    )
    return [s.to_dict() for s in consulta.stream()]


def _por_fecha(chat_id: str, tipo_mov: str, desde: date, hasta: date) -> list[dict]:
    fin = (hasta + timedelta(days=1)).isoformat()
    docs = movimientos(chat_id, "fecha", desde.isoformat(), fin)
    return [d for d in docs if d["tipo_mov"] == tipo_mov]


def _fx(monto: object, moneda: str, fecha: date) -> dict | str:
    """USD fields for one amount, or the reply when the rate is unavailable."""
    original = q(monto)
    if original <= 0:
        return POSITIVO
    cur = moneda.strip().upper()
    fx = importlib.import_module("assistant.services.fx")
    try:
        usd, tasa, fuente = fx.a_usd(original, cur, fecha)
    except fx.FxError as e:
        if str(e) == "unsupported":
            return "Moneda no soportada."
        return f"No pude obtener la tasa de {cur}, intenta luego."
    return {
        "monto": str(usd),
        "moneda": "USD",
        "monto_original": str(original),
        "moneda_original": cur,
        "tasa": str(tasa),
        "fuente_tasa": fuente,
    }


def _cifra(valor: Decimal) -> str:
    """2000 -> "2,000"; 12.5 -> "12.50"."""
    valor = abs(valor)
    return f"{valor:,.0f}" if valor == valor.to_integral() else f"{valor:,.2f}"


def texto(d: dict, sep: str = " · ") -> str:
    """One movement as "−0.49 USD · café (2,000 COP)"; a reverso as its registro."""
    signo = "−" if d["tipo_mov"] == "gasto" else "+"
    texto = f"{signo}{abs(q(d['monto']))} USD"
    etiqueta = d.get("nota") or d.get("categoria") or d.get("fuente")
    if etiqueta:
        texto += f"{sep}{etiqueta}"
    if d.get("moneda_original", "USD") != "USD":
        texto += f" ({_cifra(Decimal(d['monto_original']))} {d['moneda_original']})"
    return texto


# ponytail: batch_id = update_id, so one registrar_gasto call per turn (the prompt
# batches items); a second call in the same turn is treated as a retry.
def registrar_gasto(
    ctx: ToolContext, items: list[dict], moneda: str, fecha: date
) -> str:
    batch = f"g{ctx.update_id}"
    docs = {}
    for i, item in enumerate(items):
        usd = _fx(item["monto"], moneda, fecha)
        if isinstance(usd, str):
            return usd
        docs[f"{ctx.update_id}-{i}"] = {
            "fecha": fecha.isoformat(),
            **usd,
            "categoria": str(item.get("categoria") or "").strip() or "otros",
            "tipo_mov": "gasto",
            "nota": str(item.get("nota") or ""),
            "batch_id": batch,
            "update_id": ctx.update_id,
            "tipo": "registro",
        }
    _crear(ctx.chat_id, docs)
    _state().set_last_batch(ctx.chat_id, batch)
    return "; ".join(texto(d) for d in docs.values())


def registrar_ingreso(
    ctx: ToolContext,
    monto: Decimal,
    moneda: str,
    fuente: str,
    fecha: date,
    nota: str | None = None,
) -> str:
    usd = _fx(monto, moneda, fecha)
    if isinstance(usd, str):
        return usd
    batch = f"i{ctx.update_id}"
    doc = {
        "fecha": fecha.isoformat(),
        **usd,
        "fuente": fuente or "",
        "tipo_mov": "ingreso",
        "nota": nota or "",
        "batch_id": batch,
        "update_id": ctx.update_id,
        "tipo": "registro",
    }
    _crear(ctx.chat_id, {f"{ctx.update_id}-i0": doc})
    _state().set_last_batch(ctx.chat_id, batch)
    return texto(doc)


def _reverso(ctx: ToolContext, doc_id: str, d: dict) -> dict:
    """Negative copy of a registro; ``reversa`` names the registro it cancels."""
    rev = {**d, "monto": str(-q(d["monto"])), "update_id": ctx.update_id}
    if "monto_original" in d:
        rev["monto_original"] = str(-q(d["monto_original"]))
    return {**rev, "tipo": "reverso", "reversa": doc_id}


def deshacer(ctx: ToolContext, batch_id: str | None = None) -> str:
    batch = batch_id or _state().last_batch(ctx.chat_id)
    if not batch:
        return "Nada que deshacer."
    consulta = _col(ctx.chat_id).where(filter=FieldFilter("batch_id", "==", batch))
    lote = [(s.id, s.to_dict()) for s in consulta.stream()]
    registros = [(i, d) for i, d in lote if d["tipo"] == "registro"]
    if not registros:
        return "Lote no encontrado."
    hecho = f"↩ deshecho: {len(registros)} fila(s)"
    ya = "Ese lote ya estaba deshecho."
    previos = [d for _, d in lote if d["tipo"] == "reverso"]
    if previos:
        # Same update = Pub/Sub retry: report success again, write nothing.
        retry = all(d["update_id"] == ctx.update_id for d in previos)
        return hecho if retry else ya
    reversos = {
        f"{batch}-r{i}": _reverso(ctx, doc_id, d)
        for i, (doc_id, d) in enumerate(registros)
    }
    return hecho if _crear(ctx.chat_id, reversos) else ya


# ponytail: scans the last max(50, 10n) writes; movements older than that (behind
# many reversos) are not listed. Page further back if users edit old rows.
def _vigentes(ctx: ToolContext, n: int) -> list[tuple[str, dict]]:
    """Last n registros not cancelled by a reverso, newest first."""
    consulta = (
        _col(ctx.chat_id)
        .order_by("creado", direction=firestore.Query.DESCENDING)
        .limit(max(50, 10 * n))
    )
    docs = [(s.id, s.to_dict()) for s in consulta.stream()]
    reversos = [d for _, d in docs if d["tipo"] == "reverso"]
    anulados = {d["reversa"] for d in reversos if "reversa" in d}
    # Reversos written before ``reversa`` existed cancel their whole batch.
    lotes = {d["batch_id"] for d in reversos if "reversa" not in d}
    return [
        (i, d)
        for i, d in docs
        if d["tipo"] == "registro" and i not in anulados and d["batch_id"] not in lotes
    ][:n]


def ultimos(ctx: ToolContext, n: int = 5) -> list[dict]:
    salida = []
    for indice, (doc_id, d) in enumerate(_vigentes(ctx, n), start=1):
        mov = {
            "indice": indice,
            "id": doc_id,
            "fecha": d["fecha"],
            "tipo_mov": d["tipo_mov"],
            "monto": d["monto"],
            "monto_original": d.get("monto_original", d["monto"]),
            "moneda_original": d.get("moneda_original", d.get("moneda", "USD")),
            "nota": d.get("nota", ""),
        }
        campo = "categoria" if d["tipo_mov"] == "gasto" else "fuente"
        mov[campo] = d.get(campo, "")
        salida.append(mov)
    return salida


def ultimos_texto(ctx: ToolContext, n: int = 5) -> str:
    lineas = [
        f"{m['indice']}) {date.fromisoformat(m['fecha']):%d/%m} {texto(m, ' ')}"
        for m in ultimos(ctx, n)
    ]
    return "\n".join(lineas) or "Sin movimientos."


def _repetido(ctx: ToolContext) -> dict | None:
    """Docs this update already wrote with editar/anular (a Pub/Sub retry)."""
    consulta = _col(ctx.chat_id).where(
        filter=FieldFilter("update_id", "==", ctx.update_id)
    )
    docs = {s.id: s.to_dict() for s in consulta.stream()}
    rev = [d for i, d in docs.items() if d["tipo"] == "reverso" and i.endswith("-x")]
    if not rev:
        return None
    return docs.get(f"{ctx.update_id}-e0", rev[0])


def _elegir(ctx: ToolContext, indice: int) -> tuple[str, dict] | None:
    if indice < 1:
        return None
    movs = _vigentes(ctx, indice)
    return movs[indice - 1] if len(movs) >= indice else None


def anular(ctx: ToolContext, indice: int = 1) -> str:
    previo = _repetido(ctx)
    if previo is not None:
        return f"✓ anulado: {texto(previo)}"
    elegido = _elegir(ctx, indice)
    if elegido is None:
        return NO_ENCONTRADO
    doc_id, d = elegido
    # Reverso id per registro: two updates can never cancel the same row twice.
    if not _crear(ctx.chat_id, {f"{doc_id}-x": _reverso(ctx, doc_id, d)}):
        return NO_ENCONTRADO
    return f"✓ anulado: {texto(d)}"


def editar(
    ctx: ToolContext,
    indice: int = 1,
    monto: Decimal | None = None,
    moneda: str | None = None,
    categoria: str | None = None,
    nota: str | None = None,
) -> str:
    """Reverso of the chosen movement plus a new registro with merged fields."""
    previo = _repetido(ctx)
    if previo is not None:
        return f"✓ editado: {texto(previo)}"
    elegido = _elegir(ctx, indice)
    if elegido is None:
        return NO_ENCONTRADO
    doc_id, d = elegido
    usd = _fx(
        d.get("monto_original", d["monto"]) if monto is None else monto,
        moneda or d.get("moneda_original", d.get("moneda", "USD")),
        date.fromisoformat(d["fecha"]),
    )
    if isinstance(usd, str):
        return usd
    campo = "categoria" if d["tipo_mov"] == "gasto" else "fuente"
    nuevo = {
        **{k: v for k, v in d.items() if k != "creado"},
        **usd,
        "batch_id": f"e{ctx.update_id}",
        "update_id": ctx.update_id,
    }
    if categoria is not None:
        nuevo[campo] = categoria.strip() or ("otros" if campo == "categoria" else "")
    if nota is not None:
        nuevo["nota"] = nota
    docs = {f"{doc_id}-x": _reverso(ctx, doc_id, d), f"{ctx.update_id}-e0": nuevo}
    if not _crear(ctx.chat_id, docs):
        return NO_ENCONTRADO
    return f"✓ editado: {texto(nuevo)}"


def del_dia(chat_id: str, dia: date) -> list[dict]:
    """Registros of one day still in force (not reversed), oldest first."""
    fin = (dia + timedelta(days=1)).isoformat()
    consulta = (
        _col(chat_id)
        .where(filter=FieldFilter("fecha", ">=", dia.isoformat()))
        .where(filter=FieldFilter("fecha", "<", fin))
    )
    docs = [(s.id, s.to_dict()) for s in consulta.stream()]
    reversos = [d for _, d in docs if d.get("tipo") == "reverso"]
    anulados = {d["reversa"] for d in reversos if d.get("reversa")}
    # Reversos written before the reversa field cancel their whole batch.
    lotes = {d["batch_id"] for d in reversos if not d.get("reversa")}
    vivos = [
        d
        for doc_id, d in docs
        if d.get("tipo") != "reverso"
        and doc_id not in anulados
        and d.get("batch_id") not in lotes
    ]
    return sorted(vivos, key=lambda d: str(d.get("creado", "")))


def gastos_por_categoria(chat_id: str, desde: date, hasta: date) -> dict[str, Decimal]:
    """Net spend per category (registro + reverso) in the inclusive range."""
    totales: dict[str, Decimal] = {}
    for d in _por_fecha(chat_id, "gasto", desde, hasta):
        cat = d["categoria"]
        totales[cat] = totales.get(cat, Decimal(0)) + q(d["monto"])
    return totales


def total_ingresos(chat_id: str, desde: date, hasta: date) -> Decimal:
    docs = _por_fecha(chat_id, "ingreso", desde, hasta)
    return sum((q(d["monto"]) for d in docs), Decimal("0.00"))


def resumen_finanzas(ctx: ToolContext, periodo: str) -> str:
    desde, hasta = rango(periodo, hoy(ctx))
    gastos = gastos_por_categoria(ctx.chat_id, desde, hasta)
    total = sum(gastos.values(), Decimal("0.00"))
    ingresos = total_ingresos(ctx.chat_id, desde, hasta)
    texto = f"{periodo}: gastos {total} USD, ingresos {ingresos} USD"
    top = max(gastos, key=lambda c: gastos[c], default=None)
    if top is not None and gastos[top] > 0:
        texto += f"; mayor {top} {gastos[top]}"
    return texto
