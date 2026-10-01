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
import dataclasses
import importlib
import json
import logging
import time
from datetime import datetime
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from assistant.channels.base import InboundMessage
from assistant.channels.telegram import Telegram, parse_update
from assistant.config import WorkerSettings, get_worker_settings
from assistant.context import ToolContext
from assistant.services import agenda, quick, state

logger = logging.getLogger(__name__)
app = FastAPI(title="assistant-worker")

ACK = 204
LIMIT_REPLY = "Llegaste al límite por ahora. Intenta más tarde."
WELCOME = "Hola. Escríbeme gastos, ingresos o citas y yo los registro."
TEXT_ONLY = "Por ahora solo entiendo texto."
FAILED_REPLY = "No pude hacerlo, intenta de nuevo."
GOOGLE_HINT = "Google Calendar: Otros calendarios → + → Desde URL, y pega el enlace."
GIF_USAGE = (
    "Envía un GIF con el texto gasto o ingreso, o responde a uno con /gif gasto."
    " Tienes {gasto} de gasto y {ingreso} de ingreso."
)
EDIT_USAGE = "Uso: /editar <n> <monto>[moneda], ej. /editar 1 3usd"
ANULAR_USAGE = "Uso: /anular <n>, ej. /anular 1"
CONECTAR_HINT = (
    "Envíame el enlace iCal secreto de tu calendario. Google: Configuración → "
    "tu calendario → Integrar el calendario → Dirección secreta en formato iCal."
)
VINCULAR_HINT = (
    "Comparte tu Google Calendar con "
    "assistant-worker@jd-botjonh.iam.gserviceaccount.com (Hacer cambios en "
    "eventos) y envía /vincular <id>. En una cuenta personal el id es tu Gmail."
)
OWNER_COMMANDS = ("/invitar", "/usuarios")
INVITAR_USAGE = "Uso: /invitar <nombre>. Crea un enlace de un uso, válido 24 h."
LEDGER_COMMANDS = ("/ultimos", "/editar", "/anular", "/gif")
REGISTROS = {"registrar_gasto": "gasto", "registrar_ingreso": "ingreso"}


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


def _warm(settings: WorkerSettings) -> None:
    """Scheduler ping in waking hours: the push keeps this worker's instance
    alive and the GET keeps the api's (a cold start of both costs ~10 s)."""
    if not settings.api_url:
        return
    try:
        httpx.get(f"{settings.api_url}/health", timeout=15)
    except httpx.HTTPError:
        logger.warning("warm_api_failed")


def _route(payload: Any) -> int:
    if isinstance(payload, dict) and payload.get("job") == "warm":
        _warm(get_worker_settings())
        return ACK
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
    if msg.animation_file_id:
        _send(channel, msg, _save_gif(msg, msg.caption, msg.animation_file_id))
        return ACK
    if not msg.text.strip():
        _send(channel, msg, TEXT_ONLY)
        return ACK
    if _is_ical_url(msg.text):  # the link sent on its own, after /conectar
        msg = dataclasses.replace(msg, text=f"/conectar {msg.text.strip()}")
    if msg.text.startswith(("/calendario", "/conectar", "/vincular")):
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
    if msg.text.startswith(OWNER_COMMANDS):
        _send(channel, msg, *_owner_command(ctx, msg, settings))
        return ACK
    if msg.text.startswith(LEDGER_COMMANDS):
        _send(channel, msg, *_ledger_command(ctx, msg))
        return ACK
    entry = quick.parse(msg.text)
    if entry is not None:  # deterministic: no LLM, no spend, no history
        _quick(ctx, msg, entry, channel)
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
    registros = [REGISTROS[t] for t in result.tools if t in REGISTROS]
    if registros and not result.keyboard:
        _gif(channel, msg, registros[-1])
    state.add_llm_spend(msg.chat_id, result.cost_usd)
    state.append_history(msg.chat_id, result.messages)
    from assistant.observability import trace

    trace.record_turn(result, int((time.monotonic() - started) * 1000), msg.text)
    return ACK


def _is_ical_url(text: str) -> bool:
    """A lone https/webcal link to an allowlisted calendar host."""
    text = text.strip()
    if " " in text or not text.lower().startswith(("https://", "webcal://")):
        return False
    try:
        importlib.import_module("assistant.services.busy").validar(text)
    except Exception:
        return False
    return True


def _command(ctx: ToolContext, msg: InboundMessage, settings: WorkerSettings) -> str:
    """/calendario [enlace|nuevo], /conectar <url> and /vincular <id|off>."""
    cmd, _, arg = msg.text.strip().partition(" ")
    cmd, arg = cmd.split("@")[0], arg.strip()
    try:
        if cmd == "/conectar":
            if not arg:
                return CONECTAR_HINT
            try:
                busy = importlib.import_module("assistant.services.busy")
            except ImportError:
                return "Aún no disponible."
            return str(busy.conectar(ctx, arg))
        if cmd == "/vincular":
            if not arg:
                return VINCULAR_HINT
            gcal = importlib.import_module("assistant.services.gcal")
            return str(gcal.vincular(ctx, arg))
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


def _quick(
    ctx: ToolContext, msg: InboundMessage, entry: quick.Entry, channel: Telegram
) -> None:
    if entry.error:
        _send(channel, msg, entry.error)
        return
    if not entry.tipo:  # a bare amount: ask, register on the button
        from assistant.llm import tools

        try:
            pregunta, teclado = tools.ask_tipo(ctx, entry.monto, entry.moneda)
        except Exception as exc:
            logger.error(
                "quick_failed update_id=%s error=%s", msg.update_id, type(exc).__name__
            )
            _send(channel, msg, FAILED_REPLY)
            return
        _send(channel, msg, pregunta, teclado)
        return
    fecha = ctx.ahora.date()
    try:
        ledger = importlib.import_module("assistant.services.ledger")
        if entry.tipo == "ingreso":
            reply = ledger.registrar_ingreso(
                ctx,
                monto=entry.monto,
                moneda=entry.moneda,
                fuente=entry.nota,
                fecha=fecha,
            )
        else:
            item = {
                "monto": entry.monto,
                "categoria": entry.categoria,
                "nota": entry.nota or None,
            }
            reply = ledger.registrar_gasto(
                ctx, items=[item], moneda=entry.moneda, fecha=fecha
            )
    except Exception as exc:
        logger.error(
            "quick_failed update_id=%s error=%s", msg.update_id, type(exc).__name__
        )
        _send(channel, msg, FAILED_REPLY)
        return
    logger.info("quick_entry update_id=%s", msg.update_id)
    _registro(channel, msg, str(reply))


def _registro(channel: Telegram, msg: InboundMessage, reply: str) -> None:
    """A registration answers with the reaction GIF only; the text is the
    fallback when no GIF is stored, and the answer to anything else (errors
    such as a missing rate)."""
    tipo = {"−": "gasto", "+": "ingreso"}.get(reply[:1])
    if not (tipo and _gif(channel, msg, tipo)):
        _send(channel, msg, reply)


@cache
def _bot_username(bot_token: str) -> str:
    return Telegram(bot_token).username()


def _owner_command(
    ctx: ToolContext, msg: InboundMessage, settings: WorkerSettings
) -> tuple[str, list[list[tuple[str, str]]] | None]:
    """/invitar <nombre> (t.me deep link) and /usuarios (revoke buttons)."""
    if ctx.rol != "owner":
        return state.OWNER_ONLY, None
    cmd, _, nombre = msg.text.strip().partition(" ")
    nombre = nombre.strip()
    if cmd.split("@")[0] == "/invitar":
        if not nombre or len(nombre) > 40:
            return INVITAR_USAGE, None
        code = state.crear_invitacion(nombre)
        logger.info("invite_created update_id=%s", msg.update_id)
        link = f"https://t.me/{_bot_username(settings.telegram_bot_token)}?start={code}"
        return f"Invitación para {nombre} (un uso, 24 h). Reenvíale:\n{link}", None
    filas = state.usuarios()
    texto = "\n".join(f"{u.get('nombre', '?')} ({u.get('rol', '?')})" for _, u in filas)
    botones = [
        [(f"Revocar a {u.get('nombre', '?')}", f"rv:{chat_id}")]
        for chat_id, u in filas
        if u.get("rol") == "beta"
    ]
    return texto or "Sin usuarios.", botones or None


def _ledger_command(
    ctx: ToolContext, msg: InboundMessage
) -> tuple[str, list[list[tuple[str, str]]] | None]:
    """/ultimos, /editar n monto, /anular n (buttons), /gif: no LLM."""
    from assistant.llm import tools

    cmd, _, arg = msg.text.strip().partition(" ")
    cmd, arg = cmd.split("@")[0], arg.strip()
    if cmd == "/gif":
        return _save_gif(msg, arg, msg.reply_animation_file_id), None
    n, _, rest = arg.partition(" ")
    usage = ANULAR_USAGE if cmd == "/anular" else EDIT_USAGE
    args: dict[str, Any] = {"indice": int(n)} if n.isdecimal() else {}
    found = quick.amount(rest) if rest else None
    if cmd == "/ultimos":
        name, args = "ultimos_movimientos", {"n": 5}
    elif cmd == "/editar" and args and found:
        name, args = (
            "editar_movimiento",
            {**args, "monto": found[0], "moneda": found[1]},
        )
    elif cmd == "/anular" and args and not rest:
        name = "anular_movimiento"
    else:
        return usage, None
    try:  # same validation as the LLM path; Decimal goes as an exact string
        reply, token = tools.handle_call(ctx, name, json.dumps(args, default=str))
    except tools.ToolRejected:  # monto <= 0, índice fuera de rango
        return usage, None
    except Exception as exc:
        logger.error(
            "command_failed update_id=%s error=%s", msg.update_id, type(exc).__name__
        )
        return FAILED_REPLY, None
    return reply, tools.buttons(token) if token else None


def _save_gif(msg: InboundMessage, tipo: str, file_id: str | None) -> str:
    tipo = quick.norm(tipo.strip())
    if file_id and tipo in ("gasto", "ingreso"):
        state.add_gif(msg.chat_id, tipo, file_id)
        logger.info("gif_saved update_id=%s", msg.update_id)
        return f"✓ GIF guardado para {tipo}."
    counts = {t: len(ids) for t, ids in state.gifs(msg.chat_id).items()}
    return GIF_USAGE.format(**counts)


def _gif(channel: Telegram, msg: InboundMessage, tipo: str) -> bool:
    """Best effort reaction GIF after a registration; False when none was sent."""
    try:
        file_id = state.random_gif(msg.chat_id, tipo)
        if file_id:
            channel.send_animation(msg.chat_id, file_id)
            return True
    except Exception as exc:
        logger.warning(
            "gif_failed update_id=%s error=%s", msg.update_id, type(exc).__name__
        )
    return False


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
    if action in ("g", "i"):  # a bare amount: gasto or ingreso
        from assistant.llm.tools import execute_tipo

        try:
            tipo = "gasto" if action == "g" else "ingreso"
            _registro(channel, msg, execute_tipo(ctx, token, tipo))
        except Exception as exc:
            logger.error(
                "pending_failed update_id=%s error=%s",
                msg.update_id,
                type(exc).__name__,
            )
            _send(channel, msg, FAILED_REPLY)
        return ACK
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
        _registro(channel, msg, reply)
    elif action == "no":
        state.pop_pending(msg.chat_id, token)
        _send(channel, msg, "Cancelado.")
    elif action == "rv":  # /usuarios revoke button
        ok = ctx.rol == "owner" and state.revocar(token)
        logger.info("user_revoked update_id=%s ok=%s", msg.update_id, ok)
        _send(channel, msg, "✓ Acceso revocado." if ok else "No se pudo revocar.")
    return ACK
