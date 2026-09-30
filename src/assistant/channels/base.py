"""Channel abstraction.

Telegram is the only channel today, but the worker talks to this interface so a
future channel (WhatsApp, web) does not touch the core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InboundMessage:
    chat_id: str
    text: str
    update_id: int
    callback_data: str | None = None
    callback_query_id: str | None = None
    message_id: int | None = None


class Channel(Protocol):
    def send_message(
        self,
        chat_id: str,
        text: str,
        inline_keyboard: list[list[tuple[str, str]]] | None = None,
    ) -> None: ...

    def answer_callback(self, callback_query_id: str, text: str) -> None: ...

    def delete_message(self, chat_id: str, message_id: int) -> None: ...
