"""Google Calendar: one dedicated calendar (``CALENDAR_ID``), user's zone.

The calendar is shared by every user, so each event carries the owner's chat_id
as a private extended property; listing and cancelling filter by it.
Confirmation before cancelling is the LLM layer's job (pending + button).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from assistant.config import get_worker_settings
from assistant.context import ToolContext

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar"


@cache
def _service() -> Any:
    creds, _ = google.auth.default(scopes=[SCOPE])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _cal() -> str:
    return get_worker_settings().calendar_id


def _local(ctx: ToolContext, dt: datetime) -> datetime:
    zone = ZoneInfo(ctx.zona_horaria)
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)


def crear_evento(
    ctx: ToolContext,
    titulo: str,
    inicio: datetime,
    fin: datetime | None = None,
    ubicacion: str | None = None,
    recordatorio_min: int | None = None,
) -> str:
    inicio = _local(ctx, inicio)
    fin = _local(ctx, fin) if fin else inicio + timedelta(minutes=60)
    body: dict[str, Any] = {
        "summary": titulo,
        "start": {"dateTime": inicio.isoformat(), "timeZone": ctx.zona_horaria},
        "end": {"dateTime": fin.isoformat(), "timeZone": ctx.zona_horaria},
        "extendedProperties": {"private": {"chat_id": ctx.chat_id}},
        "reminders": {"useDefault": True},
    }
    if ubicacion:
        body["location"] = ubicacion
    if recordatorio_min is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": recordatorio_min}],
        }
    evento = _service().events().insert(calendarId=_cal(), body=body).execute()
    log.info("calendar_insert event=%s", evento["id"])
    return f"✓ {inicio:%d/%m %H:%M} {titulo} [{evento['id']}]"


def rango_agenda(ctx: ToolContext, rango: str) -> tuple[datetime, datetime]:
    """[start, end) in the user's zone for hoy|manana|semana (next 7 days)."""
    zone = ZoneInfo(ctx.zona_horaria)
    dia = ctx.ahora.astimezone(zone).date()
    inicio = datetime(dia.year, dia.month, dia.day, tzinfo=zone)
    dias = {"hoy": (0, 1), "manana": (1, 2), "semana": (0, 7)}.get(rango)
    if dias is None:
        raise ValueError("rango")
    return inicio + timedelta(days=dias[0]), inicio + timedelta(days=dias[1])


def agenda(ctx: ToolContext, rango: str) -> list[str]:
    """Compact lines ``dd/mm HH:MM title [id]``, one per event."""
    desde, hasta = rango_agenda(ctx, rango)
    resp = (
        _service()
        .events()
        .list(
            calendarId=_cal(),
            timeMin=desde.isoformat(),
            timeMax=hasta.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            timeZone=ctx.zona_horaria,
            privateExtendedProperty=f"chat_id={ctx.chat_id}",
        )
        .execute()
    )
    lineas = []
    for ev in resp.get("items", []):
        start = ev.get("start", {})
        if "dateTime" in start:
            cuando = (
                f"{_local(ctx, datetime.fromisoformat(start['dateTime'])):%d/%m %H:%M}"
            )
        else:
            cuando = f"{datetime.fromisoformat(start['date']):%d/%m} todo el día"
        lineas.append(f"{cuando} {ev.get('summary', '')} [{ev['id']}]")
    return lineas


def listar_agenda(ctx: ToolContext, rango: str) -> str:
    return "\n".join(agenda(ctx, rango)) or "Sin eventos."


def cancelar_evento(ctx: ToolContext, evento_id: str) -> str:
    events = _service().events()
    try:
        ev = events.get(calendarId=_cal(), eventId=evento_id).execute()
        owner = ev.get("extendedProperties", {}).get("private", {}).get("chat_id")
        if owner != ctx.chat_id or ev.get("status") == "cancelled":
            return "Evento no encontrado."
        events.delete(calendarId=_cal(), eventId=evento_id).execute()
    except HttpError as e:
        if e.resp.status in (404, 410):
            return "Evento no encontrado."
        raise
    log.info("calendar_delete event=%s", evento_id)
    return "✓ evento cancelado"


def recordatorio(ctx: ToolContext, texto: str, cuando: datetime) -> str:
    # ponytail: reminder via Calendar; move to Cloud Tasks if the bot must message
    # on Telegram at an exact time.
    inicio = _local(ctx, cuando)
    return crear_evento(
        ctx, texto, inicio, inicio + timedelta(minutes=15), recordatorio_min=0
    )
