"""Read-only busy times from the user's own calendar via its secret iCal URL.

``preferences/{chat_id}.ics_url`` holds the "secret address in iCal format" of
a Google, iCloud or Outlook calendar. No OAuth: the URL itself grants read
access, so it is a secret. Never log it or any part of it; it lives only in
Firestore. External event titles are never returned, logged or stored: every
block is labelled "Ocupado".

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

from assistant.context import ToolContext

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
    return firestore.Client(project=os.environ.get("GCP_PROJECT_ID") or None)


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
        raw = _state().get_preferences(chat_id).get("ics_url")
        if not raw:
            return []
        user = _state().get_user(chat_id) or {}
        zone = ZoneInfo(user.get("zona_horaria") or DEFAULT_ZONE)
        now = time.monotonic()
        hit = _cache.get(chat_id)
        if hit and hit[1] == raw and now - hit[0] < CACHE_TTL_S:
            cal = hit[2]
        else:
            cal = _load(raw)
            _cache[chat_id] = (now, raw, cal)
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
        ref.set({"ics_url": firestore.DELETE_FIELD}, merge=True)
        log.info("ics_disconnect")
        return "Calendario desconectado."
    try:
        _load(url)
    except BusyError as exc:
        log.info("ics_connect_rejected code=%s", exc)
        return INVALID if str(exc) == "invalid_url" else UNREADABLE
    ref.set({"ics_url": url.strip()}, merge=True)
    log.info("ics_connect")
    return "✓ Calendario conectado."
