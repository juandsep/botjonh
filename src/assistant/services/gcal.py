"""Mirror the Firestore agenda into the user's own Google Calendar, instantly.

The owner shares a calendar with the worker service account ("Make changes to
events") and links it with ``/vincular <calendar_id>``, stored in clear in
``preferences/{chat_id}.gcal_id``: the id grants nothing without the share.
Firestore stays the source of truth; every mirror call is best effort and never
raises into the turn. Google event ids derive from ``chat_id:evento_id`` so a
retry is idempotent (409 on insert and 404/410 on delete count as done).

Calendar REST v3 over httpx with the worker's ADC token, scope
``calendar.events`` only: busy times come from ``events.list`` (freeBusy needs
a broader scope), skipping our own mirrored events. Calendar ids, titles and
chat_ids are PII: log status codes and error classes only.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from google.cloud import firestore

from assistant.context import ToolContext

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO; the URL holds the calendar id.
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
SA_EMAIL = "assistant-worker@jd-botjonh.iam.gserviceaccount.com"
TIMEOUT_S = 5.0
LABEL = "Ocupado"
DEFAULT_ZONE = "America/Panama"
_CAL_ID = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MIO = re.compile(r"bj[0-9a-f]{40}")  # ids of our mirrored events

NO_ACCESS = (
    f"No tengo acceso. Comparte el calendario con {SA_EMAIL} "
    "(Hacer cambios en eventos)."
)


@cache
def _db() -> firestore.Client:
    return firestore.Client(project=os.environ.get("GCP_PROJECT_ID") or None)


@cache
def _creds() -> Any:
    import google.auth

    creds, _ = google.auth.default(scopes=[SCOPE])
    return creds


def _call(method: str, path: str, **kwargs: Any) -> httpx.Response:
    creds = _creds()
    if not creds.valid:
        import google.auth.transport.requests

        creds.refresh(google.auth.transport.requests.Request())
    headers = {"Authorization": f"Bearer {creds.token}"}
    return httpx.request(
        method, f"{API}{path}", headers=headers, timeout=TIMEOUT_S, **kwargs
    )


def _events(cal: str) -> str:
    return f"/calendars/{quote(cal, safe='')}/events"


def _linked(chat_id: str) -> str | None:
    state = importlib.import_module("assistant.services.state")
    return state.get_preferences(chat_id).get("gcal_id") or None


def evento_gid(chat_id: str, evento_id: str) -> str:
    """Google event id: base32hex (0-9a-v), so hex digits are valid as is."""
    return "bj" + hashlib.sha256(f"{chat_id}:{evento_id}".encode()).hexdigest()[:40]


def vincular(ctx: ToolContext, calendar_id: str) -> str:
    """Prove write access with a throwaway event, then store; "off" unlinks."""
    cal = calendar_id.strip()
    ref = _db().collection("preferences").document(ctx.chat_id)
    if cal.lower() == "off":
        ref.set({"gcal_id": firestore.DELETE_FIELD}, merge=True)
        log.info("gcal_unlink")
        return "Google Calendar desvinculado."
    if len(cal) > 200 or not _CAL_ID.fullmatch(cal):
        return "Id de calendario no válido."
    inicio = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    probe = {
        "summary": "botjonh",
        "start": {"dateTime": inicio.isoformat()},
        "end": {"dateTime": (inicio + timedelta(minutes=1)).isoformat()},
        "transparency": "transparent",
        "visibility": "private",
    }
    try:
        resp = _call("POST", _events(cal), json=probe)
        if resp.status_code in (403, 404):
            log.info("gcal_link_rejected code=%s", resp.status_code)
            return NO_ACCESS
        resp.raise_for_status()
        _call("DELETE", f"{_events(cal)}/{resp.json()['id']}")
    except Exception as exc:
        log.error("gcal_link_failed code=%s", type(exc).__name__)
        return "No pude verificar el calendario, intenta luego."
    ref.set({"gcal_id": cal}, merge=True)
    log.info("gcal_link")
    return "✓ Google Calendar vinculado."


def _best_effort(
    op: str, chat_id: str, method: str, sub: str, ok: tuple[int, ...], **kw: Any
) -> None:
    """Call the linked calendar; 2xx and the ``ok`` idempotent codes are done."""
    try:
        cal = _linked(chat_id)
        if not cal:
            return
        code = _call(method, f"{_events(cal)}{sub}", **kw).status_code
        if code < 300 or code in ok:
            log.info("gcal_%s", op)
        else:
            log.error("gcal_%s_failed code=%s", op, code)
    except Exception as exc:  # never raise into the turn
        log.error("gcal_%s_failed code=%s", op, type(exc).__name__)


def espejo_crear(ctx: ToolContext, evento_id: str, evento: dict) -> None:
    body: dict[str, Any] = {
        "id": evento_gid(ctx.chat_id, evento_id),
        "summary": evento["titulo"],
        "start": {"dateTime": evento["inicio"]},
        "end": {"dateTime": evento["fin"]},
    }
    if evento.get("ubicacion"):
        body["location"] = evento["ubicacion"]
    if evento.get("recordatorio_min") is not None:
        popup = {"method": "popup", "minutes": evento["recordatorio_min"]}
        body["reminders"] = {"useDefault": False, "overrides": [popup]}
    _best_effort("mirror", ctx.chat_id, "POST", "", (409,), json=body)


def espejo_cancelar(ctx: ToolContext, evento_id: str) -> None:
    sub = f"/{evento_gid(ctx.chat_id, evento_id)}"
    _best_effort("unmirror", ctx.chat_id, "DELETE", sub, (404, 410))


def _as_dt(value: dict, zone: ZoneInfo) -> datetime:
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"]).astimezone(UTC)
    dia = date.fromisoformat(value["date"])  # all-day: midnight in the user's zone
    return datetime.combine(dia, time(), zone).astimezone(UTC)


def ocupados(
    chat_id: str, desde: datetime, hasta: datetime
) -> list[tuple[datetime, datetime, str]]:
    """Busy blocks of the linked calendar minus our mirrors, UTC; [] on failure."""
    try:
        cal = _linked(chat_id)
        if not cal:
            return []
        params = {
            "timeMin": desde.astimezone(UTC).isoformat(),
            "timeMax": hasta.astimezone(UTC).isoformat(),
            "singleEvents": "true",
            "maxResults": 250,
            "fields": "items(id,status,transparency,start,end)",
        }
        resp = _call("GET", _events(cal), params=params)
        if resp.status_code != 200:
            log.error("gcal_busy_failed code=%s", resp.status_code)
            return []
        user = importlib.import_module("assistant.services.state").get_user(chat_id)
        zone = ZoneInfo((user or {}).get("zona_horaria") or DEFAULT_ZONE)
        return sorted(
            (_as_dt(ev["start"], zone), _as_dt(ev["end"], zone), LABEL)
            for ev in resp.json().get("items", [])
            if not _MIO.fullmatch(ev.get("id", ""))
            and ev.get("status") != "cancelled"
            and ev.get("transparency") != "transparent"
        )
    except Exception as exc:  # never raise into the conversation
        log.error("gcal_busy_failed code=%s", type(exc).__name__)
    return []
