"""Per-turn trace: one structured log line, then MLflow best-effort.

No PII: no chat_id, no message text (only its sha256), no reply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from assistant.config import get_worker_settings

if TYPE_CHECKING:
    from assistant.llm.client import TurnResult

log = logging.getLogger(__name__)

EXPERIMENT = "botjonh"
_TOKEN_TTL_S = 50 * 60  # Google identity tokens live 60 min
_token: tuple[str, str, float] | None = None  # (audience, token, fetched_at)


def record_turn(result: TurnResult, latency_ms: int, text: str) -> None:
    """Never raises."""
    try:
        fields: dict[str, Any] = {
            "prompt_version": result.prompt_version,
            "model": result.model,
            "tools": list(result.tools),
            "latency_ms": int(latency_ms),
            "tokens_hit": result.tokens_hit,
            "tokens_miss": result.tokens_miss,
            "tokens_out": result.tokens_out,
            "cost_usd": float(result.cost_usd),
            "rejected": result.rejected,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        log.info(json.dumps({"event": "llm_turn", **fields}))
        uri = get_worker_settings().mlflow_tracking_uri
        if uri:
            _log_mlflow(uri, fields)
    except Exception as exc:  # observability must never break a turn
        log.error(json.dumps({"event": "trace_error", "code": type(exc).__name__}))


def _id_token(audience: str) -> str:
    global _token
    if _token and _token[0] == audience and time.monotonic() - _token[2] < _TOKEN_TTL_S:
        return _token[1]
    import google.auth.transport.requests
    from google.oauth2 import id_token

    request = google.auth.transport.requests.Request()
    token: str = id_token.fetch_id_token(request, audience)
    _token = (audience, token, time.monotonic())
    return token


def _log_mlflow(uri: str, fields: dict[str, Any]) -> None:
    os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"] = "3"
    os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"] = "0"
    if uri.startswith("https://"):  # private Cloud Run; local http needs no token
        os.environ["MLFLOW_TRACKING_TOKEN"] = _id_token(uri)
    import mlflow

    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run():
        mlflow.log_params(
            {"prompt_version": fields["prompt_version"], "model": fields["model"]}
        )
        mlflow.log_metrics(
            {
                k: float(fields[k])
                for k in (
                    "latency_ms",
                    "tokens_hit",
                    "tokens_miss",
                    "tokens_out",
                    "cost_usd",
                    "rejected",
                )
            }
        )
        mlflow.set_tags(
            {"text_sha256": fields["text_sha256"], "tools": ",".join(fields["tools"])}
        )
