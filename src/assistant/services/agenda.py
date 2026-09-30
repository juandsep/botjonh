"""Bot-native agenda on Firestore, published as a private ICS feed.

Layout: ``agenda/{chat_id}/eventos/{evento_id}`` with ``titulo``, ``inicio`` and
``fin`` (ISO with the user's offset), ``inicio_utc``/``fin_utc`` (timestamps for
range queries), ``ubicacion``, ``recordatorio_min``, ``tipo``
(evento|recordatorio), ``estado`` (activo|cancelado) and ``creado``.

``evento_id`` is the update_id (``{update_id}-{n}`` for another item of the same
turn), created with ``create()``: a Pub/Sub retry hits AlreadyExists and answers
the same. Cancelling sets ``estado`` and never deletes. A reminder is a Cloud
Task with a deterministic name that POSTs to the worker at the exact time.
Doc paths and task names derive from chat_ids: never log them. When the user
linked a Google Calendar (``services/gcal.py``) each create/cancel is mirrored
there too, best effort.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

from google.api_core.exceptions import AlreadyExists, Conflict, NotFound
from google.cloud import firestore, tasks_v2
from google.cloud.firestore import FieldFilter

from assistant.config import WorkerSettings, get_worker_settings
from assistant.context import ToolContext

log = logging.getLogger(__name__)

DURACION = {"evento": timedelta(minutes=60), "recordatorio": timedelta(minutes=15)}
TASKS_MAX = timedelta(days=30)  # Cloud Tasks schedules at most 30 days ahead
# ponytail: range queries look back one day, so an item longer than a day that
# started earlier is missed; add an end-time query if multi-day events appear.
LOOKBACK = timedelta(days=1)
DIAS = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
JORNADA = (time(8), time(20))
_ID = re.compile(r"\d{1,20}(-\d{1,2})?")


@cache
def _db() -> firestore.Client:
    return firestore.Client(
        project=os.environ.get("GCP_PROJECT_ID") or None,
        database=os.environ.get("FIRESTORE_DATABASE") or None,  # staging has its own
    )


@cache
def _tasks() -> Any:
    return tasks_v2.CloudTasksClient()


def _col(chat_id: str) -> Any:
    return _db().collection("agenda").document(chat_id).collection("eventos")


def _local(ctx: ToolContext, dt: datetime) -> datetime:
    zone = ZoneInfo(ctx.zona_horaria)
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)


def _items(chat_id: str, desde: datetime, hasta: datetime) -> list[dict]:
    """Active items of one chat overlapping [desde, hasta), by start."""
    consulta = (
        _col(chat_id)
        .where(filter=FieldFilter("inicio_utc", ">=", desde.astimezone(UTC) - LOOKBACK))
        .where(filter=FieldFilter("inicio_utc", "<", hasta.astimezone(UTC)))
    )
    docs = [{**s.to_dict(), "id": s.id} for s in consulta.stream()]
    activos = [d for d in docs if d["estado"] == "activo" and d["fin_utc"] > desde]
    return sorted(activos, key=lambda d: d["inicio_utc"])


def _ocupados(
    ctx: ToolContext, desde: datetime, hasta: datetime
) -> list[tuple[datetime, datetime, str]]:
    """External busy blocks (ICS feed and linked Google Calendar, which already
    skips our own mirrored events); a missing or failing source counts as none."""
    bloques = []
    for fuente in ("busy", "gcal"):
        try:
            mod = importlib.import_module(f"assistant.services.{fuente}")
            bloques += mod.ocupados(ctx.chat_id, desde, hasta)
        except Exception as e:  # ImportError included: sources ship separately
            log.error("%s_failed error=%s", fuente, type(e).__name__)
    return [(a, b, label) for a, b, label in bloques if a < hasta and b > desde]


def _espejo(fn: str, *args: Any) -> None:
    """Best-effort mirror into the linked Google Calendar; never raises."""
    try:
        getattr(importlib.import_module("assistant.services.gcal"), fn)(*args)
    except Exception as e:
        log.error("gcal_failed error=%s", type(e).__name__)


# --- reminders (Cloud Tasks) -----------------------------------------------------


def _queue(s: WorkerSettings) -> str:
    return (
        f"projects/{s.project_id}/locations/{s.tasks_location}/queues/{s.tasks_queue}"
    )


def task_name(s: WorkerSettings, chat_id: str, evento_id: str) -> str:
    digest = hashlib.sha256(f"{chat_id}:{evento_id}".encode()).hexdigest()[:32]
    return f"{_queue(s)}/tasks/r-{digest}"


def _programar(chat_id: str, evento_id: str, d: dict, ahora: datetime) -> None:
    """Enqueue the reminder when it falls within Cloud Tasks' 30-day horizon."""
    if d.get("recordatorio_min") is None:
        return
    cuando = d["inicio_utc"] - timedelta(minutes=d["recordatorio_min"])
    if not ahora < cuando <= ahora + TASKS_MAX:
        return  # past, or too far: the daily digest enqueues it later
    s = get_worker_settings()
    if not (s.worker_url and s.worker_sa):
        log.info("reminder_skipped reason=no_worker_url")
        return
    body = {"chat_id": chat_id, "evento_id": evento_id}
    task = tasks_v2.Task(
        name=task_name(s, chat_id, evento_id),
        schedule_time=cuando,
        http_request={
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{s.worker_url}/tasks/reminder",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body).encode(),
            "oidc_token": {
                "service_account_email": s.worker_sa,
                "audience": s.worker_url,
            },
        },
    )
    try:
        _tasks().create_task(parent=_queue(s), task=task)
    except AlreadyExists:
        return  # same name: already enqueued
    except Exception as e:
        # ponytail: the item is saved; the next digest retries within 30 days,
        # so a reminder due before then is lost. Retry inline if that shows up.
        log.error("reminder_enqueue_failed error=%s", type(e).__name__)
        return
    log.info("reminder_enqueued")


def _borrar_tarea(chat_id: str, evento_id: str) -> None:
    s = get_worker_settings()
    if not (s.worker_url and s.worker_sa):
        return
    try:
        _tasks().delete_task(name=task_name(s, chat_id, evento_id))
    except NotFound:
        pass  # never enqueued, or already ran
    except Exception as e:  # the worker skips cancelled items anyway
        log.error("reminder_delete_failed error=%s", type(e).__name__)


def encolar_recordatorios(ctx: ToolContext) -> None:
    """Digest backstop: enqueue reminders that entered the 30-day horizon."""
    # ponytail: rescans 60 days per chat daily; index a reminder field at scale.
    hasta = ctx.ahora + 2 * TASKS_MAX
    for d in _items(ctx.chat_id, ctx.ahora, hasta):
        _programar(ctx.chat_id, d["id"], d, ctx.ahora)


def aviso(chat_id: str, evento_id: str) -> str | None:
    """The reminder text for an active item; None if cancelled or missing."""
    if not _ID.fullmatch(evento_id):
        return None
    snap = _col(chat_id).document(evento_id).get()
    d = snap.to_dict() if snap.exists else None
    if not d or d["estado"] != "activo":
        return None
    return f"⏰ {d['titulo']} {datetime.fromisoformat(d['inicio']):%H:%M}"


# --- tools -----------------------------------------------------------------------


def _guardar(
    ctx: ToolContext,
    tipo: str,
    titulo: str,
    inicio: datetime,
    fin: datetime | None,
    ubicacion: str | None,
    recordatorio_min: int | None,
) -> str:
    inicio = _local(ctx, inicio)
    fin = _local(ctx, fin) if fin else inicio + DURACION[tipo]
    data = {
        "titulo": titulo,
        "inicio": inicio.isoformat(),
        "fin": fin.isoformat(),
        "inicio_utc": inicio.astimezone(UTC),
        "fin_utc": fin.astimezone(UTC),
        "ubicacion": ubicacion or "",
        "recordatorio_min": recordatorio_min,
        "tipo": tipo,
        "estado": "activo",
    }
    for n in range(10):
        evento_id = f"{ctx.update_id}-{n}" if n else str(ctx.update_id)
        ref = _col(ctx.chat_id).document(evento_id)
        try:
            ref.create({**data, "creado": firestore.SERVER_TIMESTAMP})
            log.info("agenda_write tipo=%s", tipo)
        except Conflict:
            previo = ref.get().to_dict() or {}
            if (previo.get("titulo"), previo.get("inicio")) != (titulo, data["inicio"]):
                continue  # another item of the same turn
            log.info("agenda_retry")
        _programar(ctx.chat_id, evento_id, data, ctx.ahora)
        _espejo("espejo_crear", ctx, evento_id, data)
        return f"✓ {inicio:%d/%m %H:%M} {titulo} [{evento_id}]"
    raise RuntimeError("agenda_ids_exhausted")


def crear_evento(
    ctx: ToolContext,
    titulo: str,
    inicio: datetime,
    fin: datetime | None = None,
    ubicacion: str | None = None,
    recordatorio_min: int | None = None,
) -> str:
    return _guardar(ctx, "evento", titulo, inicio, fin, ubicacion, recordatorio_min)


def recordatorio(ctx: ToolContext, texto: str, cuando: datetime) -> str:
    return _guardar(ctx, "recordatorio", texto, cuando, None, None, 0)


def cancelar_evento(ctx: ToolContext, evento_id: str) -> str:
    if not _ID.fullmatch(evento_id):
        return "Evento no encontrado."
    ref = _col(ctx.chat_id).document(evento_id)
    snap = ref.get()
    if not snap.exists or (snap.to_dict() or {}).get("estado") != "activo":
        return "Evento no encontrado."
    ref.update({"estado": "cancelado"})
    _borrar_tarea(ctx.chat_id, evento_id)
    _espejo("espejo_cancelar", ctx, evento_id)
    log.info("agenda_cancel")
    return "✓ evento cancelado"


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
    """Compact lines ``dd/mm HH:MM title [id]``, one per item."""
    desde, hasta = rango_agenda(ctx, rango)
    zone = ZoneInfo(ctx.zona_horaria)
    return [
        f"{d['inicio_utc'].astimezone(zone):%d/%m %H:%M} {d['titulo']} [{d['id']}]"
        for d in _items(ctx.chat_id, desde, hasta)
    ]


def listar_agenda(ctx: ToolContext, rango: str) -> str:
    return "\n".join(agenda(ctx, rango)) or "Sin eventos."


def conflictos(ctx: ToolContext, inicio: datetime, fin: datetime) -> list[str]:
    """Events and external busy blocks overlapping [inicio, fin), as short lines."""
    inicio, fin = _local(ctx, inicio), _local(ctx, fin)
    zone = ZoneInfo(ctx.zona_horaria)
    bloques = [
        (d["inicio_utc"], d["fin_utc"], d["titulo"])
        for d in _items(ctx.chat_id, inicio, fin)
        if d["tipo"] == "evento"
    ]
    bloques += _ocupados(ctx, inicio, fin)
    return [
        f"{label} {a.astimezone(zone):%H:%M}–{b.astimezone(zone):%H:%M}"
        for a, b, label in sorted(bloques, key=lambda b: b[0])
    ]


def libres(ctx: ToolContext, dia: date) -> list[str]:
    """Free slots between 08:00 and 20:00 of ``dia`` (agenda + busy blocks)."""
    zone = ZoneInfo(ctx.zona_horaria)
    cursor = datetime.combine(dia, JORNADA[0], zone)
    fin = datetime.combine(dia, JORNADA[1], zone)
    bloques = [
        (d["inicio_utc"], d["fin_utc"])
        for d in _items(ctx.chat_id, cursor, fin)
        if d["tipo"] == "evento"
    ]
    bloques += [(a, b) for a, b, _ in _ocupados(ctx, cursor, fin)]
    huecos = []
    for a, b in sorted(bloques):
        if a > cursor:
            huecos.append((cursor, min(a, fin)))
        cursor = max(cursor, b)
    if cursor < fin:
        huecos.append((cursor, fin))
    return [
        f"{a.astimezone(zone):%H:%M}–{b.astimezone(zone):%H:%M}"
        for a, b in huecos
        if b > a
    ]


def ver_libres(ctx: ToolContext, fecha: date) -> str:
    return ", ".join(libres(ctx, fecha)) or "Sin huecos libres."


def semana(ctx: ToolContext) -> str:
    """/calendario: one line per day with items in the next 7 days."""
    desde, hasta = rango_agenda(ctx, "semana")
    zone = ZoneInfo(ctx.zona_horaria)
    entradas = [
        (d["inicio_utc"], d["titulo"]) for d in _items(ctx.chat_id, desde, hasta)
    ]
    entradas += [(a, label) for a, _, label in _ocupados(ctx, desde, hasta)]
    dias: dict[date, list[str]] = {}
    for inicio, titulo in sorted(entradas, key=lambda e: e[0]):
        local = max(inicio, desde).astimezone(zone)
        dias.setdefault(local.date(), []).append(f"{local:%H:%M} {titulo}")
    lineas = [
        " · ".join([f"{DIAS[dia.weekday()]} {dia.day}", *items])
        for dia, items in dias.items()
    ]
    return "\n".join(lineas) or "Sin nada en 7 días."


# --- ICS feed (RFC 5545) ---------------------------------------------------------


def _escape(texto: str) -> str:
    texto = texto.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return texto.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(linea: str) -> str:
    """Fold at 75 octets without splitting a UTF-8 character."""
    partes, actual, octetos = [], "", 0
    for ch in linea:
        n = len(ch.encode())
        if octetos + n > 75:
            partes.append(actual)
            actual, octetos = " ", 1
        actual += ch
        octetos += n
    partes.append(actual)
    return "\r\n".join(partes)


def _utc_ics(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def ics(chat_id: str, ahora: datetime) -> str:
    """VCALENDAR with active items from 30 days ago to 365 days ahead."""
    lineas = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//botjonh//agenda//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:botjonh",
    ]
    desde, hasta = ahora - timedelta(days=30), ahora + timedelta(days=365)
    for d in _items(chat_id, desde, hasta):
        titulo = _escape(d["titulo"])
        lineas += [
            "BEGIN:VEVENT",
            f"UID:{d['id']}@botjonh",
            f"DTSTAMP:{_utc_ics(ahora)}",
            f"DTSTART:{_utc_ics(d['inicio_utc'])}",
            f"DTEND:{_utc_ics(d['fin_utc'])}",
            f"SUMMARY:{titulo}",
        ]
        if d.get("ubicacion"):
            lineas.append(f"LOCATION:{_escape(d['ubicacion'])}")
        if d.get("recordatorio_min") is not None:
            lineas += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{titulo}",
                f"TRIGGER:-PT{d['recordatorio_min']}M",
                "END:VALARM",
            ]
        lineas.append("END:VEVENT")
    lineas.append("END:VCALENDAR")
    return "".join(f"{_fold(linea)}\r\n" for linea in lineas)
