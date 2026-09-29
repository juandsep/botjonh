"""DeepSeek chat client with a bounded tool-calling loop.

Request layout, for DeepSeek's automatic prefix cache: the fixed system prompt
and the tools come first and are byte-identical on every call; then a short
``system`` message with the current date, time and currency; then the recent
history; then the user text, only ever as a ``user`` message.

``PROMPT_VERSION`` is the sha256 (first 12 hex chars) of ``system.md`` plus the
tools JSON, so any prompt or schema change bumps it automatically.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from assistant.config import get_worker_settings
from assistant.context import ToolContext
from assistant.llm.tools import (
    TOOL_SPECS,
    TOOLS_JSON,
    ToolRejected,
    buttons,
    handle_call,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text("utf-8")
PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + TOOLS_JSON).encode()).hexdigest()[:12]
MAX_ROUNDS = 3
HISTORY_MESSAGES = 12  # 6 turns of user + assistant
TIMEOUT_S = 30.0
FALLBACK_REPLY = "No pude completarlo, intenta de nuevo."
_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MILLION = Decimal(1_000_000)


class LLMUnavailable(Exception):
    """429, 5xx or timeout from the LLM: the worker answers 5xx so Pub/Sub retries."""


@dataclass
class TurnResult:
    reply: str
    keyboard: list[list[tuple[str, str]]] | None
    messages: list[dict[str, Any]]
    tools: list[str]
    prompt_version: str
    model: str
    tokens_hit: int
    tokens_miss: int
    tokens_out: int
    cost_usd: Decimal
    rejected: int


def _context_message(ctx: ToolContext) -> dict[str, str]:
    now = ctx.ahora
    return {
        "role": "system",
        "content": (
            f"Ahora: {_DIAS[now.weekday()]} {now:%Y-%m-%d %H:%M} "
            f"({ctx.zona_horaria}). Moneda: {ctx.moneda}."
        ),
    }


def _post(client: httpx.Client, url: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = client.post(url, json=body)
    except httpx.TransportError as exc:  # timeouts and connection errors
        raise LLMUnavailable(type(exc).__name__) from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise LLMUnavailable(f"http_{resp.status_code}")
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


def run_turn(ctx: ToolContext, text: str, history: list[dict[str, Any]]) -> TurnResult:
    settings = get_worker_settings()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        _context_message(ctx),
        *history[-HISTORY_MESSAGES:],
        {"role": "user", "content": text},
    ]
    result = TurnResult(
        reply=FALLBACK_REPLY,
        keyboard=None,
        messages=[],
        tools=[],
        prompt_version=PROMPT_VERSION,
        model=settings.llm_model,
        tokens_hit=0,
        tokens_miss=0,
        tokens_out=0,
        cost_usd=Decimal(0),
        rejected=0,
    )
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}
    with httpx.Client(timeout=TIMEOUT_S, headers=headers) as client:
        for _ in range(MAX_ROUNDS):
            data = _post(
                client,
                url,
                {
                    "model": settings.llm_model,
                    "messages": messages,
                    "tools": TOOL_SPECS,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                    "thinking": {"type": "disabled"},
                    "stream": False,
                },
            )
            usage = data.get("usage") or {}
            result.tokens_hit += int(usage.get("prompt_cache_hit_tokens", 0))
            result.tokens_miss += int(usage.get("prompt_cache_miss_tokens", 0))
            result.tokens_out += int(usage.get("completion_tokens", 0))
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                result.reply = (message.get("content") or "").strip() or FALLBACK_REPLY
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
            if _run_calls(ctx, calls, messages, result):
                break  # a confirmation turn ends here, without another LLM call
        # ponytail: a 3rd round that still asks for tools gets FALLBACK_REPLY.
    result.cost_usd = (
        result.tokens_hit * settings.price_in_hit
        + result.tokens_miss * settings.price_in_miss
        + result.tokens_out * settings.price_out
    ) / _MILLION
    # History keeps only the text turns: short, and never a dangling tool call.
    result.messages = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": result.reply},
    ]
    return result


def _run_calls(
    ctx: ToolContext,
    calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    result: TurnResult,
) -> bool:
    """Run one round of tool calls. True if it ended in a confirmation turn."""
    for call in calls:
        fn = call.get("function") or {}
        name = str(fn.get("name", ""))
        try:
            content, token = handle_call(ctx, name, str(fn.get("arguments") or ""))
        except ToolRejected as exc:
            result.rejected += 1
            log.warning('{"event": "tool_rejected", "code": "%s"}', exc)
            content, token = f"error: {exc}", None
        else:
            result.tools.append(name)
        if token:
            result.reply, result.keyboard = content, buttons(token)
            return True
        messages.append(
            {"role": "tool", "tool_call_id": call.get("id", ""), "content": content}
        )
    return False
