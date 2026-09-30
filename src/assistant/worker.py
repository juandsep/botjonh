"""Worker entrypoint (assistant-worker).

Push subscriber for assistant-updates (user messages) and assistant-cron
(scheduled jobs), and target of the Cloud Tasks reminders. The service is
private: Cloud Run validates the OIDC token (Pub/Sub, Cloud Tasks) before a
request reaches these routes, so no unauthenticated caller gets here.

Any 2xx acks the message; a 5xx makes Pub/Sub retry with backoff.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import json
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from assistant.channels.base import InboundMessage
from assistant.channels.telegram import Telegram, parse_update
from assistant.config import WorkerSettings, get_worker_settings
from assistant.context import ToolContext
from assistant.services import agenda, state

logger = logging.getLogger(__name__)
app = FastAPI(title="assistant-worker")

ACK = 204
LIMIT_REPLY = "Llegaste al límite por ahora. Intenta más tarde."
WELCOME = "Hola. Escríbeme gastos, ingresos o citas y yo los registro."
TEXT_ONLY = "Por ahora solo entiendo texto."
FAILED_REPLY = "No pude hacerlo, intenta de nuevo."
GOOGLE_HINT = "Google Calendar: Otros calendarios → + → Desde URL, y pega el enlace."


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/push")
async def push(request: Request) -> Response:
    try:
        envelope = json.loads(await request.body())
        payload = json.loads(base64.b64decode(envelope["message"]["data"]))
    except (ValueError, KeyError, TypeError, binascii.Error):
        # Malformed: ack so Pub/Sub does not retry it forever.
        logger.warning("malformed_envelope")
        return Response(status_code=ACK)
    return Response(status_code=await run_in_threadpool(_route, payload))


@app.post("/tasks/reminder")
async def reminder(request: Request) -> Response:
    """Cloud Tasks at the reminder time. Always 2xx unless Firestore fails."""
    try:
        body = json.loads(await request.body())
        chat_id, evento_id = str(body["chat_id"]), str(body["evento_id"])
    except (ValueError, KeyError, TypeError):
        logger.warning("malformed_task")
        return Response(status_code=ACK)
    await run_in_threadpool(_remind, chat_id, evento_id)
    return Response(status_code=ACK)


def _remind(chat_id: str, evento_id: str) -> None:
    texto = agenda.aviso(chat_id, evento_id)
    if texto is None:  # cancelled or missing
        return
    try:
        Telegram(get_worker_settings().telegram_bot_token).send_message(chat_id, texto)
    except httpx.HTTPError:
        logger.warning("reminder_send_failed")
        return
    logger.info("reminder_sent")


def _route(payload: Any) -> int:
    if isinstance(payload, dict) and "job" in payload:
        from assistant.jobs import run_job

        run_job(str(payload["job"]))
        return ACK
    msg = parse_update(payload)
    if msg is None:
        logger.warning("unsupported_update")
        return ACK
    return handle_update(msg, get_worker_settings())


def _context(user: dict, msg: InboundMessage, settings: WorkerSettings) -> ToolContext:
    zona = user.get("zona_horaria") or settings.default_timezone
    return ToolContext(
        chat_id=msg.chat_id,
        rol=user.get("rol", "beta"),
        moneda=user.get("moneda", "USD"),
        zona_horaria=zona,
        update_id=msg.update_id,
        ahora=datetime.now(ZoneInfo(zona)),
    )


def handle_update(msg: InboundMessage, settings: WorkerSettings) -> int:
    user = state.get_user(msg.chat_id)
    if user is None:  # removed after the api accepted it
        return ACK
    ctx = _context(user, msg, settings)
    channel = Telegram(settings.telegram_bot_token)
    if msg.callback_query_id:
        return _callback(ctx, msg, msg.callback_query_id, channel)
    if msg.text.startswith("/start"):
        _send(channel, msg, WELCOME)
        return ACK
    if not msg.text.strip():
        _send(channel, msg, TEXT_ONLY)
        return ACK
    if msg.text.startswith(("/calendario", "/conectar")):
        reply = _command(ctx, msg, settings)
        if msg.text.startswith("/conectar ") and msg.message_id is not None:
            # The message holds the secret iCal URL: drop it from the chat.
            try:
                channel.delete_message(msg.chat_id, msg.message_id)
                reply += "\nBorré tu mensaje con el enlace."
            except httpx.HTTPError:
                logger.warning("delete_failed update_id=%s", msg.update_id)
        _send(channel, msg, reply)
        return ACK

    # 1. Rate limit and daily cap: fail closed without calling the LLM.
    if not state.check_rate(msg.chat_id, settings.max_msgs_per_minute) or (
        state.llm_spend_today(msg.chat_id) >= settings.max_llm_usd_per_day
    ):
        logger.info("limit_reached update_id=%s", msg.update_id)
        _send(channel, msg, LIMIT_REPLY)
        return ACK

    # 2. The turn. LLMUnavailable -> 503 so Pub/Sub retries with backoff.
    from assistant.llm import client

    started = time.monotonic()
    try:
        result = client.run_turn(ctx, msg.text, state.get_history(msg.chat_id))
    except client.LLMUnavailable:
        logger.warning("llm_unavailable update_id=%s", msg.update_id)
        return 503
    except Exception as exc:
        # Any other failure is acknowledged: a Pub/Sub retry would pay for the
        # turn again and could repeat a non-idempotent write.
        logger.error(
            "turn_failed update_id=%s error=%s", msg.update_id, type(exc).__name__
        )
        _send(channel, msg, FAILED_REPLY)
        return ACK

    # 3-5. Reply, account, trace.
    _send(channel, msg, result.reply, result.keyboard)
    state.add_llm_spend(msg.chat_id, result.cost_usd)
    state.append_history(msg.chat_id, result.messages)
    from assistant.observability import trace

    trace.record_turn(result, int((time.monotonic() - started) * 1000), msg.text)
    return ACK


def _command(ctx: ToolContext, msg: InboundMessage, settings: WorkerSettings) -> str:
    """/calendario [enlace|nuevo] and /conectar <url>, without the LLM."""
    cmd, _, arg = msg.text.strip().partition(" ")
    cmd, arg = cmd.split("@")[0], arg.strip()
    try:
        if cmd == "/conectar":
            if not arg:
                return "Uso: /conectar <url del calendario .ics>"
            try:
                busy = importlib.import_module("assistant.services.busy")
            except ImportError:
                return "Aún no disponible."
            return str(busy.conectar(ctx, arg))
        if arg in ("enlace", "nuevo"):
            if not settings.api_url:
                return "Enlace no configurado."
            token = state.ics_token(ctx.chat_id, rotate=arg == "nuevo")
            return f"{settings.api_url}/ics/{token}.ics\n{GOOGLE_HINT}"
        return agenda.semana(ctx)
    except Exception as exc:
        logger.error(
            "command_failed update_id=%s error=%s", msg.update_id, type(exc).__name__
        )
        return FAILED_REPLY


def _send(
    channel: Telegram, msg: InboundMessage, text: str, keyboard: Any = None
) -> None:
    """Best effort: a Telegram error must not make Pub/Sub rerun a paid turn."""
    try:
        channel.send_message(msg.chat_id, text, keyboard)
    except httpx.HTTPError:
        logger.warning("send_failed update_id=%s", msg.update_id)


def _callback(
    ctx: ToolContext, msg: InboundMessage, query_id: str, channel: Telegram
) -> int:
    try:  # best effort: stops the button spinner; fails on a stale query
        channel.answer_callback(query_id, "")
    except httpx.HTTPError:
        logger.warning("answer_callback_failed update_id=%s", msg.update_id)
    action, _, token = (msg.callback_data or "").partition(":")
    if action == "ok":
        from assistant.llm.tools import execute_pending

        try:
            reply = execute_pending(ctx, token)
        except Exception as exc:
            logger.error(
                "pending_failed update_id=%s error=%s",
                msg.update_id,
                type(exc).__name__,
            )
            reply = FAILED_REPLY
        _send(channel, msg, reply)
    elif action == "no":
        state.pop_pending(msg.chat_id, token)
        _send(channel, msg, "Cancelado.")
    return ACK
