"""Read-only busy times from the user's own calendar via its secret iCal URL.

``preferences/{chat_id}.ics_url_enc`` holds the "secret address in iCal
format" of a Google, iCloud or Outlook calendar, encrypted with Cloud KMS
(``services/crypto.py``). No OAuth: the URL itself grants read access, so it is
a secret. Never log it or any part of it, and never store it in clear.
External event titles are never returned, logged or stored: every block is
labelled "Ocupado".

SSRF: the URL is user-supplied, so only https (webcal is rewritten) to an
allowlisted host, port 443, no userinfo, no IP literals, no redirects, 5 s
timeout and a 2 MB streamed body cap.
"""

from __future__ import annotations

import importlib
import ipaddress
import logging
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
from google.cloud import firestore

from assistant.config import get_worker_settings
from assistant.context import ToolContext
from assistant.services import crypto

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO; that URL is the secret. Silence it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

HOSTS = {"calendar.google.com", "outlook.office365.com", "outlook.live.com"}
ICLOUD = re.compile(r"p\d+-caldav\.icloud\.com")
TIMEOUT_S = 5.0
MAX_BYTES = 2 * 1024 * 1024
CACHE_TTL_S = 300.0
LABEL = "Ocupado"
DEFAULT_ZONE = "America/Panama"

INVALID = "Enlace no válido."
UNREADABLE = "No pude leer ese calendario."

# ponytail: per-instance cache (each Cloud Run instance fetches on its own);
# fine for one turn, move to Firestore/Redis if fetch volume ever matters.
_cache: dict[str, tuple[float, str, Any]] = {}


class BusyError(Exception):
    """Carries only a short error code, never the URL."""


@cache
def _db() -> firestore.Client:
    return firestore.Client(
        project=os.environ.get("GCP_PROJECT_ID") or None,
        database=os.environ.get("FIRESTORE_DATABASE") or None,  # staging has its own
    )


def _state() -> Any:
    return importlib.import_module("assistant.services.state")


def validar(raw: str) -> httpx.URL:
    """The fetchable https URL, or BusyError("invalid_url")."""
    try:
        url = httpx.URL(raw.strip())
        if url.scheme == "webcal":
            url = url.copy_with(scheme="https")
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise BusyError("invalid_url") from exc
    host = url.host.lower()
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise BusyError("invalid_url")
    except ValueError:
        pass
    if (
        url.scheme != "https"
        or url.userinfo
        or url.port not in (None, 443)
        or not (host in HOSTS or ICLOUD.fullmatch(host))
    ):
        raise BusyError("invalid_url")
    return url


def _fetch(url: httpx.URL) -> bytes:
    headers = {"Accept": "text/calendar"}
    try:
        with (
            httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
            if resp.status_code != 200:
                raise BusyError(f"status_{resp.status_code}")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise BusyError("too_large")
            return bytes(body)
    except httpx.HTTPError as exc:
        raise BusyError("network") from exc


def _load(raw: str) -> Any:
    body = _fetch(validar(raw))
    try:
        return icalendar.Calendar.from_ical(body)
    except Exception as exc:  # malformed feeds raise all sorts of things
        raise BusyError("parse") from exc


def _as_dt(value: date | datetime, zone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):  # all-day: midnight in the user's zone
        value = datetime.combine(value, dtime(), zone)
    elif value.tzinfo is None:  # floating time: the user's zone
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC)


def _blocks(
    cal: Any, desde: datetime, hasta: datetime, zone: ZoneInfo
) -> list[tuple[datetime, datetime, str]]:
    out = []
    for ev in recurring_ical_events.of(cal).between(desde, hasta):
        if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = ev["DTSTART"].dt
        if "DTEND" in ev:
            end = ev["DTEND"].dt
        elif "DURATION" in ev:
            end = start + ev["DURATION"].dt
        else:  # RFC 5545: all-day lasts one day, a timed event is instant
            end = start if isinstance(start, datetime) else start + timedelta(1)
        a, b = _as_dt(start, zone), _as_dt(end, zone)
        if a < hasta and b > desde:
            out.append((a, b, LABEL))
    return sorted(out)


def ocupados(
    chat_id: str, desde: datetime, hasta: datetime
) -> list[tuple[datetime, datetime, str]]:
    """Busy blocks overlapping [desde, hasta), UTC. [] if none, unset or failing."""
    try:
        enc = _state().get_preferences(chat_id).get("ics_url_enc")
        if not enc:
            return []
        user = _state().get_user(chat_id) or {}
        zone = ZoneInfo(user.get("zona_horaria") or DEFAULT_ZONE)
        now = time.monotonic()
        hit = _cache.get(chat_id)
        if hit and hit[1] == enc and now - hit[0] < CACHE_TTL_S:
            cal = hit[2]
        else:
            key = get_worker_settings().kms_key
            cal = _load(crypto.decrypt(key, enc, chat_id))
            _cache[chat_id] = (now, enc, cal)
        return _blocks(cal, desde, hasta, zone)
    except BusyError as exc:
        log.error("ics_busy_failed code=%s", exc)
    except Exception as exc:  # never raise into the conversation
        log.error("ics_busy_failed code=%s", type(exc).__name__)
    return []


def conectar(ctx: ToolContext, url: str) -> str:
    """Validate by fetching and parsing once, then store; "off" disconnects."""
    ref = _db().collection("preferences").document(ctx.chat_id)
    _cache.pop(ctx.chat_id, None)
    if url.strip().lower() == "off":
        ref.set(
            {"ics_url": firestore.DELETE_FIELD, "ics_url_enc": firestore.DELETE_FIELD},
            merge=True,
        )
        log.info("ics_disconnect")
        return "Calendario desconectado."
    key = get_worker_settings().kms_key
    if not key:  # fail closed: never store the URL in clear
        log.warning("ics_connect_rejected code=no_kms_key")
        return "Aún no disponible."
    try:
        _load(url)
    except BusyError as exc:
        log.info("ics_connect_rejected code=%s", exc)
        return INVALID if str(exc) == "invalid_url" else UNREADABLE
    enc = crypto.encrypt(key, url.strip(), ctx.chat_id)
    ref.set({"ics_url_enc": enc, "ics_url": firestore.DELETE_FIELD}, merge=True)
    log.info("ics_connect")
    return "✓ Calendario conectado."
