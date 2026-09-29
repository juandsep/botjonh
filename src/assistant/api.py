"""Webhook entrypoint (assistant-api).

Verifies the secret token and the secret route before parsing, checks the
allowlist, deduplicates by update_id and publishes to Pub/Sub. Returns 2xx fast;
never calls the LLM. The only state it writes is the dedup marker and, for
``/start <code>`` from an unknown chat, the invite redemption.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

from assistant.channels.telegram import parse_update
from assistant.config import get_api_settings
from assistant.services import pubsub, state

logger = logging.getLogger(__name__)
app = FastAPI(title="assistant-api")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tg/{path}")
async def webhook(path: str, request: Request) -> Response:
    settings = get_api_settings()

    # 1. Constant-time checks before parsing the body. Telegram does not sign
    # the webhook, so both the header and the route are secrets.
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(provided, settings.webhook_secret_token):
        return Response(status_code=403)
    if not hmac.compare_digest(path, settings.webhook_path):
        return Response(status_code=403)

    # 2. Parse; anything we do not handle is acknowledged and dropped.
    try:
        update = json.loads(await request.body())
    except ValueError:
        return Response(status_code=200)
    return await run_in_threadpool(_accept, update, settings.updates_topic)


def _start_code(text: str) -> str:
    cmd, _, code = text.strip().partition(" ")
    return code.strip() if cmd == "/start" else ""


def _accept(update: Any, topic: str) -> Response:
    msg = parse_update(update)
    if msg is None:
        return Response(status_code=200)

    # 3. Allowlist. The one exception: /start <code> redeems an invite. A
    #    stranger is dropped here without spending tokens.
    if state.get_user(msg.chat_id) is None:
        code = _start_code(msg.text)
        if not code or not state.redeem_invite(code, msg.chat_id):
            logger.info("dropped update_id=%s reason=unknown_chat", msg.update_id)
            return Response(status_code=200)
        logger.info("invite_redeemed update_id=%s", msg.update_id)

    # 4. Dedup (Telegram retries on non-2xx), then hand off to the worker.
    if not state.mark_processed(msg.update_id):
        return Response(status_code=200)
    try:
        pubsub.publish(topic, update)
    except Exception as exc:
        # Let Telegram retry instead of losing the message.
        logger.error(
            "publish_failed update_id=%s error=%s", msg.update_id, type(exc).__name__
        )
        state.unmark_processed(msg.update_id)
        return Response(status_code=500)
    return Response(status_code=200)
