"""Jobs. Cloud Scheduler publishes ``tick`` every hour (UTC); each user gets, in
their own time zone, the digest at 07:00 (reminders, agenda, yesterday's spend),
the checkin at 22:00 (the day's list) and on Sunday at 22:00 the checkin plus
the weekly summary in one message. Once a day, at 12:00 UTC, the tick exports
the ledger CSV, and on Sunday also runs the backup. ``digest``, ``checkin`` and
``weekly`` stay runnable by name for manual use.

The assistant is concise: a job messages a chat only when there is something to
say. One failing chat never stops the others.
"""

from __future__ import annotations

import calendar as cal
import importlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
        lineas.append(f"Ayer: {gastado} USD.")
    return "\n".join(lineas) or None


def _checkin(ctx: ToolContext) -> str | None:
    """22:00: every movement of the day and the day's spend."""
    movs = ledger.del_dia(ctx.chat_id, ledger.hoy(ctx))
    if not movs:
        return "Hoy no registraste gastos."
    gastos = [d for d in movs if d["tipo_mov"] == "gasto"]
    total = sum((ledger.q(d["monto"]) for d in gastos), Decimal("0.00"))
    lineas = [f"Hoy ({len(movs)}):", *(ledger.texto(d) for d in movs)]
    lineas.append(f"Total gastos: {total} USD")
    return "\n".join(lineas)


def _weekly(ctx: ToolContext) -> str | None:
    """Sunday 22:00, after the checkin: the week vs income, minus 20% saved."""
    dia = ledger.hoy(ctx)
    desde, hasta = ledger.rango("semana", dia)
    gastos = ledger.gastos_por_categoria(ctx.chat_id, desde, hasta)
    total = sum(gastos.values(), Decimal("0.00"))
    inicio_mes = dia.replace(day=1)
    ingresos = ledger.total_ingresos(ctx.chat_id, inicio_mes, dia)
    if not total and not ingresos:
        return None
    lineas = [f"Semana {desde:%d/%m}–{hasta:%d/%m}: {total} USD"]
    top = sorted((kv for kv in gastos.items() if kv[1] > 0), key=lambda kv: -kv[1])
    if top:
        lineas.append(
            "Top: " + " · ".join(f"{ledger.etiqueta(c)} {v}" for c, v in top[:3])
        )
    if ingresos <= 0:
        lineas.append("Sin ingresos este mes: registra uno (1000usd ingreso).")
        return "\n".join(lineas)
    mes = ledger.gastos_por_categoria(ctx.chat_id, inicio_mes, dia)
    gastado = sum(mes.values(), Decimal("0.00"))
    ahorro = ledger.q(ingresos * Decimal("0.20"))
    libre = ledger.q(ingresos - ahorro - gastado)
    dias = cal.monthrange(dia.year, dia.month)[1] - dia.day
    semanas = max(Decimal(dias) / 7, Decimal(1))
    lineas.append(f"Mes: ingresos {ingresos}, gastos {gastado} USD.")
    if libre >= 0:
        lineas.append(
            f"Ahorra {ahorro} (20%). Te quedan {libre} USD para el mes "
            f"(~{ledger.q(libre / semanas)}/semana)."
        )
    else:
        lineas.append(f"Te pasaste {-libre} USD: el ahorro de {ahorro} está en riesgo.")
    semana = Decimal(7) / cal.monthrange(dia.year, dia.month)[1]
    exceso = budgets.linea_exceso(ctx, gastos, semana)
    if exceso and exceso.startswith("Exceso"):
        lineas.append(exceso)
    return "\n".join(lineas)


JOBS: dict[str, Callable[[ToolContext], str | None]] = {
    "digest": _digest,
    "checkin": _checkin,
    "weekly": _weekly,
}


REPORT_DASH_TTL = timedelta(hours=24)


def _tick(ctx: ToolContext, state: Any, api_url: str) -> str | None:
    """The message due at the user's local hour, or None (ledger left unread).

    ponytail: :30/:45 offsets (India, Nepal) get the local hour the tick lands
    in (07:30, 22:30); add half-hour ticks if those users want the exact time.
    """
    if ctx.ahora.hour == 7:
        return _digest(ctx)
    if ctx.ahora.hour != 22:
        return None
    partes = [_checkin(ctx)]
    if ctx.ahora.weekday() == 6:  # Sunday: one message, not two
        partes.append(_weekly(ctx))
    texto = "\n\n".join(p for p in partes if p)
    if not texto or not api_url:
        return texto or None
    try:
        token = state.dash_token(ctx.chat_id, ttl=REPORT_DASH_TTL)
    except Exception as e:
        log.warning("report_dash_failed error=%s", type(e).__name__)
        return texto
    return f"{texto}\nTablero: {api_url}/tablero/{token}"


def _ctx(
    chat_id: str, user: dict[str, Any], default_tz: str, ahora: datetime
) -> ToolContext:
    zona = user.get("zona_horaria") or default_tz
    return ToolContext(
        chat_id=chat_id,
        rol=user.get("rol", "beta"),
        moneda=user.get("moneda", "USD"),
        zona_horaria=zona,
        update_id=0,
        ahora=ahora.astimezone(ZoneInfo(zona)),
    )


def _once(state: Any, key: str, fn: Callable[[], object]) -> None:
    """Run a global side effect once per key; a failure leaves the key unset,
    so the next hourly tick retries it. Never blocks the users' messages."""
    if state.cron_done(key):
        return
    try:
        fn()
    except Exception as e:
        log.error(
            "cron_failed job=%s error=%s", key.partition(":")[0], type(e).__name__
        )
        return
    state.mark_cron(key)


def run_job(name: str) -> None:
    if name != "tick" and name not in JOBS:
        raise ValueError(f"unknown job: {name}")
    settings = get_worker_settings()
    ahora = datetime.now(UTC)
    state = importlib.import_module("assistant.services.state")
    if name == "tick" and ahora.hour >= 12:
        # From 12:00 UTC on, until each one succeeds once (export daily, backup
        # on Sundays): a failure or a missed tick is retried an hour later.
        _once(state, f"export:{ahora:%Y-%m-%d}", lambda: backup.export_ledger(settings))
        if ahora.weekday() == 6:
            semana = ahora.isocalendar()
            _once(
                state,
                f"backup:{semana.year}-W{semana.week}",
                lambda: backup.run(settings),
            )
    if name == "weekly":
        backup.run(settings)  # manual run: a failure raises
    if name == "digest":
        _once(state, f"export:{ahora:%Y-%m-%d}", lambda: backup.export_ledger(settings))
    telegram = Telegram(settings.telegram_bot_token)
    sent = failed = 0
    for chat_id in state.list_chat_ids():
        try:
            user = state.get_user(chat_id)
            if not user:
                continue
            ctx = _ctx(chat_id, user, settings.default_timezone, ahora)
            if name == "tick":
                texto = _tick(ctx, state, settings.api_url)
            else:
                texto = JOBS[name](ctx)
            if texto:
                telegram.send_message(chat_id, texto)
                sent += 1
        except Exception as e:  # one chat never blocks the rest
            failed += 1
            log.warning("job_chat_failed job=%s error=%s", name, type(e).__name__)
    log.info("job_done job=%s sent=%d failed=%d", name, sent, failed)
