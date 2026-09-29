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

Set a Firestore TTL policy on ``expire_at`` for processed, invites, rate, spend
and pending. Doc ids contain chat_ids: never log them.
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
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")  # secrets.token_urlsafe(16)


@cache
def _db() -> firestore.Client:
    return firestore.Client(project=os.environ.get("GCP_PROJECT_ID") or None)


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


# --- owner tools -----------------------------------------------------------------

OWNER_ONLY = "Solo el owner puede hacer eso."


def invitar_beta(ctx: ToolContext, nombre: str) -> str:
    if ctx.rol != "owner":
        return OWNER_ONLY
    code = secrets.token_urlsafe(16)
    _doc("invites", code).set(
        {"nombre": nombre, "used": False, "expire_at": _now() + INVITE_TTL}
    )
    return f"Invitación para {nombre}: /start {code} (un uso, válida 24 h)."


def listar_usuarios(ctx: ToolContext) -> str:
    if ctx.rol != "owner":
        return OWNER_ONLY
    users = [s.to_dict() or {} for s in _db().collection("users").stream()]
    return "\n".join(f"{u.get('nombre', '?')} ({u.get('rol', '?')})" for u in users)
