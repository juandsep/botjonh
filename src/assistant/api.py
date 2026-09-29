"""Webhook entrypoint (assistant-api).

Verifies the secret token and the secret route before parsing, checks the
allowlist, deduplicates by update_id and publishes to Pub/Sub. Returns 2xx fast;
never calls the LLM or writes state.
"""

from __future__ import annotations

import hmac

from fastapi import FastAPI, Request, Response

from assistant.config import get_api_settings

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

    # 2. Parse, allowlist check and dedup by update_id in a Firestore
    #    transaction, then publish to assistant-updates. A non-allowlisted
    #    chat_id is dropped here without spending tokens.
    #    TODO: implement via services.state and services.pubsub.

    return Response(status_code=200)
