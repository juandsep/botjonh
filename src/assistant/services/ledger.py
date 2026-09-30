"""Finance ledger on Firestore: append-only, Decimal amounts stored as strings.

Layout: ``ledger/{chat_id}/movimientos/{doc_id}`` with the fields ``fecha``
(ISO date), ``monto`` (str, quantized to 0.01), ``moneda``, ``categoria`` (gasto)
or ``fuente`` (ingreso), ``tipo_mov`` (gasto|ingreso), ``nota``, ``batch_id``,
``update_id``, ``tipo`` (registro|reverso) and ``creado`` (server timestamp).

Doc ids: gasto ``{update_id}-{i}``, ingreso ``{update_id}-i0``, reverso
``{batch_id}-r{i}``. Every call creates its docs with ``create()`` in one atomic
batch, so a Pub/Sub retry of the same update hits AlreadyExists and writes
nothing. Undo appends negative ``reverso`` docs and never edits or deletes.

Reads query one chat's subcollection by a single field (``fecha``, ``creado``
or ``batch_id``): automatic single-field indexes, no composite index. Sums run
in Python with Decimal. Doc paths contain chat_ids: never log them.
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


# ponytail: batch_id = update_id, so one registrar_gasto call per turn (the prompt
# batches items); a second call in the same turn is treated as a retry.
def registrar_gasto(
    ctx: ToolContext, items: list[dict], moneda: str, fecha: date
) -> str:
    batch = f"g{ctx.update_id}"
    docs = {}
    for i, item in enumerate(items):
        monto = q(item["monto"])
        if monto <= 0:
            raise ValueError("monto")
        docs[f"{ctx.update_id}-{i}"] = {
            "fecha": fecha.isoformat(),
            "monto": str(monto),
            "moneda": moneda,
            "categoria": str(item["categoria"]),
            "tipo_mov": "gasto",
            "nota": str(item.get("nota") or ""),
            "batch_id": batch,
            "update_id": ctx.update_id,
            "tipo": "registro",
        }
    _crear(ctx.chat_id, docs)
    _state().set_last_batch(ctx.chat_id, batch)
    return "; ".join(
        f"✓ {d['monto']} {moneda} → {d['categoria']}" for d in docs.values()
    )


def registrar_ingreso(
    ctx: ToolContext,
    monto: Decimal,
    moneda: str,
    fuente: str,
    fecha: date,
    nota: str | None = None,
) -> str:
    valor = q(monto)
    if valor <= 0:
        raise ValueError("monto")
    batch = f"i{ctx.update_id}"
    doc = {
        "fecha": fecha.isoformat(),
        "monto": str(valor),
        "moneda": moneda,
        "fuente": fuente,
        "tipo_mov": "ingreso",
        "nota": nota or "",
        "batch_id": batch,
        "update_id": ctx.update_id,
        "tipo": "registro",
    }
    _crear(ctx.chat_id, {f"{ctx.update_id}-i0": doc})
    _state().set_last_batch(ctx.chat_id, batch)
    return f"✓ +{valor} {moneda} ← {fuente}"


def deshacer(ctx: ToolContext, batch_id: str | None = None) -> str:
    batch = batch_id or _state().last_batch(ctx.chat_id)
    if not batch:
        return "Nada que deshacer."
    consulta = _col(ctx.chat_id).where(filter=FieldFilter("batch_id", "==", batch))
    lote = [s.to_dict() for s in consulta.stream()]
    registros = [d for d in lote if d["tipo"] == "registro"]
    if not registros:
        return "Lote no encontrado."
    hecho = f"↩ deshecho: {len(registros)} fila(s)"
    ya = "Ese lote ya estaba deshecho."
    previos = [d for d in lote if d["tipo"] == "reverso"]
    if previos:
        # Same update = Pub/Sub retry: report success again, write nothing.
        retry = all(d["update_id"] == ctx.update_id for d in previos)
        return hecho if retry else ya
    reversos = {
        f"{batch}-r{i}": {
            **d,
            "monto": str(-q(d["monto"])),
            "update_id": ctx.update_id,
            "tipo": "reverso",
        }
        for i, d in enumerate(registros)
    }
    return hecho if _crear(ctx.chat_id, reversos) else ya


# ponytail: sums ignore moneda (one currency per user); add FX if users mix them.
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
    texto = f"{periodo}: gastos {total} {ctx.moneda}, ingresos {ingresos} {ctx.moneda}"
    top = max(gastos, key=lambda c: gastos[c], default=None)
    if top is not None and gastos[top] > 0:
        texto += f"; mayor {top} {gastos[top]}"
    return texto
