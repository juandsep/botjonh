"""Jobs: digest (07:30: ledger CSV, reminders), checkin (21:00), weekly (backup).

The assistant is concise: a job messages a chat only when there is something to
say. One failing chat never stops the others.
"""

from __future__ import annotations

import calendar as cal
import importlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from assistant.channels.telegram import Telegram
from assistant.config import get_worker_settings
from assistant.context import ToolContext
from assistant.jobs import backup
from assistant.services import agenda, budgets, ledger

log = logging.getLogger(__name__)


def _digest(ctx: ToolContext) -> str | None:
    agenda.encolar_recordatorios(ctx)
    lineas = agenda.agenda(ctx, "hoy")
    ayer = ledger.hoy(ctx) - timedelta(days=1)
    gastado = sum(ledger.gastos_por_categoria(ctx.chat_id, ayer, ayer).values())
    if gastado:
        lineas.append(f"Ayer: {gastado} {ctx.moneda}.")
    return "\n".join(lineas) or None


def _checkin(ctx: ToolContext) -> str | None:
    dia = ledger.hoy(ctx)
    if ledger.gastos_por_categoria(ctx.chat_id, dia, dia):
        return None
    if ledger.total_ingresos(ctx.chat_id, dia, dia):
        return None
    return "¿Algún gasto hoy? Escríbelo y lo anoto."


def _weekly(ctx: ToolContext) -> str | None:
    dia = ledger.hoy(ctx)
    desde, hasta = ledger.rango("semana", dia)
    gastos = ledger.gastos_por_categoria(ctx.chat_id, desde, hasta)
    total = sum(gastos.values(), Decimal("0.00"))
    if not total:
        return None
    semana = Decimal(7) / cal.monthrange(dia.year, dia.month)[1]
    lineas = [
        f"Semana: {total} {ctx.moneda}.",
        budgets.linea_exceso(ctx, gastos, semana),
    ]
    return "\n".join(line for line in lineas if line)


JOBS: dict[str, Callable[[ToolContext], str | None]] = {
    "digest": _digest,
    "checkin": _checkin,
    "weekly": _weekly,
}


def _ctx(chat_id: str, user: dict[str, Any], default_tz: str) -> ToolContext:
    zona = user.get("zona_horaria") or default_tz
    return ToolContext(
        chat_id=chat_id,
        rol=user.get("rol", "beta"),
        moneda=user.get("moneda", "USD"),
        zona_horaria=zona,
        update_id=0,
        ahora=datetime.now(ZoneInfo(zona)),
    )


def run_job(name: str) -> None:
    job = JOBS.get(name)
    if job is None:
        raise ValueError(f"unknown job: {name}")
    settings = get_worker_settings()
    if name == "weekly":
        # Before messaging: a failed backup raises, so Pub/Sub retries the job.
        backup.run(settings)
    if name == "digest":
        # Before messaging, but best-effort: the weekly backup still has the data.
        try:
            backup.export_ledger(settings)
        except Exception as e:
            log.warning("ledger_export_failed error=%s", type(e).__name__)
    state = importlib.import_module("assistant.services.state")
    telegram = Telegram(settings.telegram_bot_token)
    sent = failed = 0
    for chat_id in state.list_chat_ids():
        try:
            user = state.get_user(chat_id)
            if not user:
                continue
            texto = job(_ctx(chat_id, user, settings.default_timezone))
            if texto:
                telegram.send_message(chat_id, texto)
                sent += 1
        except Exception as e:  # one chat never blocks the rest
            failed += 1
            log.warning("job_chat_failed job=%s error=%s", name, type(e).__name__)
    log.info("job_done job=%s sent=%d failed=%d", name, sent, failed)
