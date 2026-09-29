"""Finance ledger on Google Sheets: append-only, Decimal amounts.

Sheets ``Gastos`` and ``Ingresos`` share the columns ``fecha, monto, moneda,
categoria|fuente, nota, batch_id, update_id, tipo, chat_id``. ``tipo`` is
``registro`` or ``reverso``; undo appends negative ``reverso`` rows and never
edits or deletes. Every read is filtered by ``chat_id``.

Idempotency is by ``(update_id, item index)``: a Pub/Sub retry re-runs the turn,
so rows already written for that update are skipped.
"""

from __future__ import annotations

import importlib
import logging
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

import google.auth
from googleapiclient.discovery import build

from assistant.config import get_worker_settings
from assistant.context import ToolContext

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GASTOS = "Gastos"
INGRESOS = "Ingresos"
# Column positions.
FECHA, MONTO, MONEDA, CATEGORIA, NOTA, BATCH, UPDATE, TIPO, CHAT = range(9)
CENT = Decimal("0.01")


@cache
def _service() -> Any:
    creds, _ = google.auth.default(scopes=[SCOPE])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _state() -> Any:
    return importlib.import_module("assistant.services.state")


def q(value: object) -> Decimal:
    """Amount as Decimal rounded to cents; str() first so floats never leak."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def valores(hoja: str) -> list[list[str]]:
    """Raw cell values of a sheet (header included)."""
    resp = (
        _service()
        .spreadsheets()
        .values()
        .get(spreadsheetId=get_worker_settings().spreadsheet_id, range=f"{hoja}!A:I")
        .execute()
    )
    return resp.get("values", [])


def _rows(hoja: str, chat_id: str) -> list[list[str]]:
    """Ledger rows of one chat, padded to 9 columns; the header is dropped."""
    padded = (r + [""] * (9 - len(r)) for r in valores(hoja))
    return [
        r for r in padded if r[TIPO] in ("registro", "reverso") and r[CHAT] == chat_id
    ]


def _append(hoja: str, rows: list[list[str]]) -> None:
    # RAW: values are stored as typed, so a note like "=IMPORTXML(...)" is text.
    _service().spreadsheets().values().append(
        spreadsheetId=get_worker_settings().spreadsheet_id,
        range=f"{hoja}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    log.info("sheets_append sheet=%s rows=%d", hoja, len(rows))


def _written(rows: list[list[str]], update_id: int) -> int:
    return sum(1 for r in rows if r[UPDATE] == str(update_id) and r[TIPO] == "registro")


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


# ponytail: batch_id = update_id, so one registrar_gasto call per turn (the prompt
# batches items); a second call in the same turn is treated as a retry.
def registrar_gasto(
    ctx: ToolContext, items: list[dict], moneda: str, fecha: date
) -> str:
    batch = f"g{ctx.update_id}"
    nuevas = []
    for item in items:
        monto = q(item["monto"])
        if monto <= 0:
            raise ValueError("monto")
        nuevas.append(
            [
                fecha.isoformat(),
                str(monto),
                moneda,
                str(item["categoria"]),
                str(item.get("nota") or ""),
                batch,
                str(ctx.update_id),
                "registro",
                ctx.chat_id,
            ]
        )
    done = _written(_rows(GASTOS, ctx.chat_id), ctx.update_id)
    if nuevas[done:]:
        _append(GASTOS, nuevas[done:])
    _state().set_last_batch(ctx.chat_id, batch)
    return "; ".join(f"✓ {r[MONTO]} {moneda} → {r[CATEGORIA]}" for r in nuevas)


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
    if not _written(_rows(INGRESOS, ctx.chat_id), ctx.update_id):
        row = [fecha.isoformat(), str(valor), moneda, fuente, nota or ""]
        _append(INGRESOS, [row + [batch, str(ctx.update_id), "registro", ctx.chat_id]])
    _state().set_last_batch(ctx.chat_id, batch)
    return f"✓ +{valor} {moneda} ← {fuente}"


def deshacer(ctx: ToolContext, batch_id: str | None = None) -> str:
    batch = batch_id or _state().last_batch(ctx.chat_id)
    if not batch:
        return "Nada que deshacer."
    hoja = INGRESOS if batch.startswith("i") else GASTOS
    lote = [r for r in _rows(hoja, ctx.chat_id) if r[BATCH] == batch]
    registros = [r for r in lote if r[TIPO] == "registro"]
    if not registros:
        return "Lote no encontrado."
    hecho = f"↩ deshecho: {len(registros)} fila(s)"
    previos = [r for r in lote if r[TIPO] == "reverso"]
    if previos:
        # Same update = Pub/Sub retry: report success again, write nothing.
        retry = all(r[UPDATE] == str(ctx.update_id) for r in previos)
        return hecho if retry else "Ese lote ya estaba deshecho."
    reversos = [
        [
            r[FECHA],
            str(-q(r[MONTO])),
            r[MONEDA],
            r[CATEGORIA],
            r[NOTA],
            batch,
            str(ctx.update_id),
            "reverso",
            ctx.chat_id,
        ]
        for r in registros
    ]
    _append(hoja, reversos)
    return hecho


def _in_range(rows: list[list[str]], desde: date, hasta: date) -> list[list[str]]:
    lo, hi = desde.isoformat(), hasta.isoformat()
    return [r for r in rows if lo <= r[FECHA] <= hi]


# ponytail: sums ignore the moneda column (one currency per user); add FX if users
# mix currencies. Full-sheet scan per call; fine for a personal ledger.
def gastos_por_categoria(chat_id: str, desde: date, hasta: date) -> dict[str, Decimal]:
    """Net spend per category (registro + reverso) in the inclusive range."""
    totales: dict[str, Decimal] = {}
    for r in _in_range(_rows(GASTOS, chat_id), desde, hasta):
        totales[r[CATEGORIA]] = totales.get(r[CATEGORIA], Decimal(0)) + q(r[MONTO])
    return totales


def total_ingresos(chat_id: str, desde: date, hasta: date) -> Decimal:
    rows = _in_range(_rows(INGRESOS, chat_id), desde, hasta)
    return sum((q(r[MONTO]) for r in rows), Decimal("0.00"))


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
