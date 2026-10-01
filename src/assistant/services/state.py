"""Firestore state: users, dedup, invites, counters, pending confirmations.

Collections (Firestore native):

- ``users/{chat_id}``: nombre, rol (owner|beta), moneda, zona_horaria, last_batch.
- ``processed/{update_id}``: dedup marker; ``expire_at`` drives a 7-day TTL.
- ``invites/{code}``: nombre, used, ``expire_at`` (24 h, single use).
- ``rate/{chat_id}_{minute}``: messages in that minute.
- ``spend/{chat_id}_{day}``: LLM USD that UTC day, a Decimal stored as string.
- ``preferences/{chat_id}``: presupuesto por categoría, etc.
- ``pending/{token}``: chat_id, action, ``expire_at`` (10 min).
- ``history/{chat_id}``: last turns, each ``{"messages": [...]}`` (Firestore has
  no nested arrays).
- ``ics_tokens/{token}``: chat_id of a private ICS feed; ``users.ics_token``
  points back so the link can be shown again or rotated. Never log tokens.
- ``dash/{token}``: chat_id, ``expire_at`` (1 h) of a web dashboard link.
  Never log tokens.
- ``gif_catalog/{tipo}`` (gasto|ingreso): one shared, owner-curated map
  ``{clave: [file_id, ...]}`` (max 20 each, newest last). ``clave`` is a gasto
  categoria, an ingreso fuente or ``general`` (the fallback). A random one is
  sent as a reaction after a registration. File ids are per bot, so staging and
  production keep separate catalogs. Never log file_ids.

Set a Firestore TTL policy on ``expire_at`` for processed, invites, rate, spend,
pending and dash. Doc ids contain chat_ids: never log them.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Any

from google.cloud import firestore

from assistant.context import ToolContext

HISTORY_TURNS = 6
PENDING_TTL = timedelta(minutes=10)
INVITE_TTL = timedelta(hours=24)
PROCESSED_TTL = timedelta(days=7)
DASH_TTL = timedelta(hours=1)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")  # secrets.token_urlsafe(16)
GIF_MAX = 20
_ICS_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")  # secrets.token_urlsafe(24)


@cache
def _db() -> firestore.Client:
    return firestore.Client(
        project=os.environ.get("GCP_PROJECT_ID") or None,
        database=os.environ.get("FIRESTORE_DATABASE") or None,  # staging has its own
    )


def _doc(collection: str, doc_id: str) -> Any:
    return _db().collection(collection).document(doc_id)


def _now() -> datetime:
    return datetime.now(UTC)


def _data(snap: Any) -> dict | None:
    return snap.to_dict() if snap.exists else None


# --- users -------------------------------------------------------------------


def get_user(chat_id: str) -> dict | None:
    return _data(_doc("users", chat_id).get())


def upsert_user(
    chat_id: str,
    nombre: str,
    rol: str,
    moneda: str = "USD",
    zona_horaria: str = "America/Panama",
) -> None:
    _doc("users", chat_id).set(
        {"nombre": nombre, "rol": rol, "moneda": moneda, "zona_horaria": zona_horaria},
        merge=True,
    )


def cron_done(key: str) -> bool:
    return _doc("cron", key).get().exists


def mark_cron(key: str) -> None:
    _doc("cron", key).set({"expire_at": _now() + timedelta(days=30)})


def set_zona(chat_id: str, zona: str) -> None:
    _doc("users", chat_id).set({"zona_horaria": zona}, merge=True)


def list_chat_ids() -> list[str]:
    return [snap.id for snap in _db().collection("users").stream()]


def set_last_batch(chat_id: str, batch_id: str) -> None:
    _doc("users", chat_id).set({"last_batch": batch_id}, merge=True)


def last_batch(chat_id: str) -> str | None:
    user = get_user(chat_id) or {}
    return user.get("last_batch")


def get_preferences(chat_id: str) -> dict:
    return _data(_doc("preferences", chat_id).get()) or {}


# --- dedup and invites (transactions) ------------------------------------------


@firestore.transactional
def _claim(tx: Any, ref: Any, data: dict) -> bool:
    if ref.get(transaction=tx).exists:
        return False
    tx.set(ref, data)
    return True


def mark_processed(update_id: int) -> bool:
    """True if the update is new. A retried update returns False."""
    ref = _doc("processed", str(update_id))
    return _claim(_db().transaction(), ref, {"expire_at": _now() + PROCESSED_TTL})


def unmark_processed(update_id: int) -> None:
    """Undo mark_processed when publishing failed, so Telegram's retry lands."""
    _doc("processed", str(update_id)).delete()


@firestore.transactional
def _redeem(tx: Any, invite: Any, user: Any) -> bool:
    data = _data(invite.get(transaction=tx))
    if not data or data.get("used") or data["expire_at"] <= _now():
        return False
    tx.update(invite, {"used": True})
    tx.set(
        user,
        {
            "nombre": data["nombre"],
            "rol": "beta",
            "moneda": "USD",
            "zona_horaria": "America/Panama",
        },
    )
    return True


def redeem_invite(code: str, chat_id: str) -> bool:
    """Consume a single-use invite and create the beta user. False if invalid."""
    if not _TOKEN.fullmatch(code):
        return False
    return _redeem(_db().transaction(), _doc("invites", code), _doc("users", chat_id))


# --- counters ------------------------------------------------------------------


@firestore.transactional
def _bump(tx: Any, ref: Any, limit: int, expire_at: datetime) -> bool:
    count = (_data(ref.get(transaction=tx)) or {}).get("n", 0)
    if count >= limit:
        return False
    tx.set(ref, {"n": count + 1, "expire_at": expire_at})
    return True


def check_rate(chat_id: str, limit_per_minute: int) -> bool:
    """True if this message is allowed within the per-minute limit."""
    now = _now()
    ref = _doc("rate", f"{chat_id}_{now:%Y%m%d%H%M}")
    return _bump(_db().transaction(), ref, limit_per_minute, now + timedelta(hours=1))


def _spend_ref(chat_id: str) -> Any:
    return _doc("spend", f"{chat_id}_{_now():%Y%m%d}")


def llm_spend_today(chat_id: str) -> Decimal:
    data = _data(_spend_ref(chat_id).get()) or {}
    return Decimal(data.get("usd", "0"))


@firestore.transactional
def _add(tx: Any, ref: Any, usd: Decimal) -> None:
    data = _data(ref.get(transaction=tx)) or {}
    total = Decimal(data.get("usd", "0")) + usd
    tx.set(ref, {"usd": str(total), "expire_at": _now() + timedelta(days=2)})


def add_llm_spend(chat_id: str, usd: Decimal) -> None:
    _add(_db().transaction(), _spend_ref(chat_id), usd)


# --- pending confirmations -----------------------------------------------------


def create_pending(chat_id: str, action: dict) -> str:
    token = secrets.token_urlsafe(16)
    _doc("pending", token).set(
        {"chat_id": chat_id, "action": action, "expire_at": _now() + PENDING_TTL}
    )
    return token


@firestore.transactional
def _pop(tx: Any, ref: Any, chat_id: str) -> dict | None:
    data = _data(ref.get(transaction=tx))
    if not data or data.get("chat_id") != chat_id:
        return None
    tx.delete(ref)
    return data["action"] if data["expire_at"] > _now() else None


def pop_pending(chat_id: str, token: str) -> dict | None:
    """Single-use: returns the action once, only to the chat that created it."""
    if not _TOKEN.fullmatch(token):
        return None
    return _pop(_db().transaction(), _doc("pending", token), chat_id)


# --- history -------------------------------------------------------------------


def get_history(chat_id: str) -> list[dict]:
    turns = (_data(_doc("history", chat_id).get()) or {}).get("turns", [])
    return [m for turn in turns for m in turn["messages"]]


def append_history(chat_id: str, messages: list[dict]) -> None:
    ref = _doc("history", chat_id)
    turns = (_data(ref.get()) or {}).get("turns", [])
    turns = [*turns, {"messages": messages}][-HISTORY_TURNS:]
    ref.set({"turns": turns})


# --- ICS feed tokens ------------------------------------------------------------


def ics_token(chat_id: str, rotate: bool = False) -> str:
    """The chat's feed token, created if missing; rotate revokes the old one."""
    user = _doc("users", chat_id)
    actual = (_data(user.get()) or {}).get("ics_token")
    if actual and not rotate:
        return str(actual)
    if actual:
        _doc("ics_tokens", actual).delete()
    token = secrets.token_urlsafe(24)
    _doc("ics_tokens", token).set({"chat_id": chat_id})
    user.set({"ics_token": token}, merge=True)
    return token


def chat_for_ics_token(token: str) -> str | None:
    """Owner chat of a feed token. The format is checked before any lookup."""
    if not _ICS_TOKEN.fullmatch(token):
        return None
    return (_data(_doc("ics_tokens", token).get()) or {}).get("chat_id")


# --- web dashboard tokens -------------------------------------------------------


def dash_token(chat_id: str, ttl: timedelta = DASH_TTL) -> str:
    """A new link to the chat's dashboard (1 h default); the TTL policy deletes it."""
    token = secrets.token_urlsafe(24)
    _doc("dash", token).set({"chat_id": chat_id, "expire_at": _now() + ttl})
    return token


def chat_for_dash_token(token: str) -> str | None:
    """Owner chat of a live dashboard token. The format is checked first."""
    if not _ICS_TOKEN.fullmatch(token):
        return None
    data = _data(_doc("dash", token).get())
    if not data or data["expire_at"] <= _now():  # TTL deletion lags up to a day
        return None
    return str(data["chat_id"])


# --- reaction GIF catalog -------------------------------------------------------

GIF_TIPOS = ("gasto", "ingreso")
GIF_GENERAL = "general"
_CLAVE = re.compile(r"\w{1,24}")  # letters (ñ, accents), digits and _


def valid_clave(clave: str) -> bool:
    return bool(_CLAVE.fullmatch(clave)) and clave == clave.lower()


def gif_catalog(tipo: str) -> dict[str, list[str]]:
    data = _data(_doc("gif_catalog", tipo).get()) or {}
    return {k: list(v) for k, v in data.items()}


def add_gif(tipo: str, clave: str, file_id: str) -> None:
    """Newest last; a repeated file_id moves to the end; keeps the last 20."""
    catalog = gif_catalog(tipo)
    ids = [f for f in catalog.get(clave, []) if f != file_id] + [file_id]
    # ponytail: read-modify-write without a transaction; one curator (the owner).
    _doc("gif_catalog", tipo).set({**catalog, clave: ids[-GIF_MAX:]})


def remove_gif(file_id: str) -> int:
    """Drop a file_id from every tipo and clave; returns how many were removed."""
    removed = 0
    for tipo in GIF_TIPOS:
        catalog = gif_catalog(tipo)
        kept = {k: [f for f in v if f != file_id] for k, v in catalog.items()}
        n = sum(map(len, catalog.values())) - sum(map(len, kept.values()))
        if n:
            _doc("gif_catalog", tipo).set({k: v for k, v in kept.items() if v})
            removed += n
    return removed


def random_gif(tipo: str, clave: str) -> str | None:
    """A GIF of the movement's clave, else of ``general``; None when both empty."""
    catalog = gif_catalog(tipo)
    ids = catalog.get(clave) or catalog.get(GIF_GENERAL) or []
    return secrets.choice(ids) if ids else None


def migrate_gifs(chat_id: str) -> int:
    """Copy a chat's old ``gifs/{chat_id}`` lists into ``general``."""
    old = _data(_doc("gifs", chat_id).get()) or {}
    n = 0
    for tipo in GIF_TIPOS:
        for file_id in old.get(tipo, []):
            add_gif(tipo, GIF_GENERAL, file_id)
            n += 1
    return n


# --- owner tools -----------------------------------------------------------------

OWNER_ONLY = "Solo el owner puede hacer eso."


def invitar_beta(ctx: ToolContext, nombre: str) -> str:
    if ctx.rol != "owner":
        return OWNER_ONLY
    code = crear_invitacion(nombre)
    return f"Invitación para {nombre}: /start {code} (un uso, válida 24 h)."


def crear_invitacion(nombre: str) -> str:
    """Single-use code, valid 24 h; also the payload of a t.me deep link."""
    code = secrets.token_urlsafe(16)
    _doc("invites", code).set(
        {"nombre": nombre, "used": False, "expire_at": _now() + INVITE_TTL}
    )
    return code


def usuarios() -> list[tuple[str, dict]]:
    return [(s.id, s.to_dict() or {}) for s in _db().collection("users").stream()]


def revocar(chat_id: str) -> bool:
    """Remove a beta from the allowlist; the owner cannot be revoked. Their
    data stays (ledger, agenda), so a new invite restores access."""
    ref = _doc("users", chat_id)
    if (_data(ref.get()) or {}).get("rol") != "beta":
        return False
    ref.delete()
    return True


def listar_usuarios(ctx: ToolContext) -> str:
    if ctx.rol != "owner":
        return OWNER_ONLY
    users = [s.to_dict() or {} for s in _db().collection("users").stream()]
    return "\n".join(f"{u.get('nombre', '?')} ({u.get('rol', '?')})" for u in users)
