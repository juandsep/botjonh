"""Telegram Bot API adapter.

Thin wrapper over the Bot API (HTTP, via httpx). sendMessage with optional
inline keyboard. The bot token is never logged.
"""

from __future__ import annotations

import httpx

from assistant.channels.base import Channel, InboundMessage

API_BASE = "https://api.telegram.org"


def parse_update(update: object) -> InboundMessage | None:
    """A ``message`` or ``callback_query`` update; None for anything else."""
    if not isinstance(update, dict):
        return None
    try:
        if "callback_query" in update:
            cq = update["callback_query"]
            return InboundMessage(
                chat_id=str(cq["message"]["chat"]["id"]),
                text="",
                update_id=int(update["update_id"]),
                callback_data=str(cq.get("data", "")),
                callback_query_id=str(cq["id"]),
            )
        msg = update["message"]
        return InboundMessage(
            chat_id=str(msg["chat"]["id"]),
            text=str(msg.get("text", "")),
            update_id=int(update["update_id"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


class Telegram(Channel):
    def __init__(self, bot_token: str, timeout: float = 10.0) -> None:
        self._token = bot_token
        self._client = httpx.Client(timeout=timeout)

    def _post(self, method: str, **payload: object) -> dict:
        resp = self._client.post(f"{API_BASE}/bot{self._token}/{method}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def send_message(
        self,
        chat_id: str,
        text: str,
        inline_keyboard: list[list[tuple[str, str]]] | None = None,
    ) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if inline_keyboard:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row]
                    for row in inline_keyboard
                ]
            }
        self._post("sendMessage", **payload)

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self._post(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
        )
