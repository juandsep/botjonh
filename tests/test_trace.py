import dataclasses
import hashlib
import json
import logging
import os
import sys
import types
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from assistant.config import WorkerSettings
from assistant.llm.client import TurnResult
from assistant.observability import trace

TEXT = "gasté 45 en almuerzo con Ana"  # must never appear in logs
RESULT = TurnResult(
    reply="✓ 45 USD → restaurantes",
    keyboard=None,
    messages=[],
    tools=["registrar_gasto"],
    prompt_version="abc",
    model="deepseek-flash",
    tokens_hit=100,
    tokens_miss=10,
    tokens_out=5,
    cost_usd=Decimal("0.0000096"),
    rejected=0,
)


@pytest.fixture
def mlflow_uri(monkeypatch):
    def use(uri: str) -> None:
        settings = dataclasses.replace(
            WorkerSettings.from_env(
                {
                    "GCP_PROJECT_ID": "p",
                    "TELEGRAM_BOT_TOKEN": "t",
                    "DEEPSEEK_API_KEY": "k",
                }
            ),
            mlflow_tracking_uri=uri,
        )
        monkeypatch.setattr(trace, "get_worker_settings", lambda: settings)

    monkeypatch.setattr(trace, "_token", None)
    for var in ("TOKEN", "HTTP_REQUEST_TIMEOUT", "HTTP_REQUEST_MAX_RETRIES"):
        monkeypatch.delenv(f"MLFLOW_{var}", raising=False)  # restored after
    return use


def test_logs_one_json_line_without_pii(mlflow_uri, caplog) -> None:
    mlflow_uri("")
    with caplog.at_level(logging.INFO, logger=trace.__name__):
        trace.record_turn(RESULT, 812, TEXT)
    [record] = caplog.records
    line = json.loads(record.getMessage())
    assert line["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert line["latency_ms"] == 812 and line["tokens_hit"] == 100
    assert "chat_id" not in line and "reply" not in line
    assert "Ana" not in record.getMessage() and "45" not in record.getMessage()


def test_mlflow_failure_never_raises(mlflow_uri, monkeypatch, caplog) -> None:
    mlflow_uri("http://localhost:5000")
    fake = types.ModuleType("mlflow")
    fake.set_tracking_uri = MagicMock()
    fake.set_experiment = MagicMock(side_effect=ConnectionError("down " + TEXT))
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    with caplog.at_level(logging.INFO, logger=trace.__name__):
        trace.record_turn(RESULT, 5, TEXT)
    assert json.loads(caplog.records[-1].getMessage()) == {
        "event": "trace_error",
        "code": "ConnectionError",
    }
    assert all("Ana" not in r.getMessage() for r in caplog.records)


def test_mlflow_run_with_cached_identity_token(mlflow_uri, monkeypatch) -> None:
    uri = "https://mlflow-xyz.a.run.app"
    mlflow_uri(uri)
    fetch = MagicMock(return_value="id-token")
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", fetch)
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    trace.record_turn(RESULT, 5, TEXT)
    trace.record_turn(RESULT, 5, TEXT)

    assert fetch.call_count == 1 and fetch.call_args.args[1] == uri
    assert os.environ["MLFLOW_TRACKING_TOKEN"] == "id-token"
    assert os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"] == "3"
    assert os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"] == "0"
    fake.set_experiment.assert_called_with("botjonh")
    fake.log_params.assert_called_with(
        {"prompt_version": "abc", "model": "deepseek-flash"}
    )
    assert fake.log_metrics.call_args.args[0]["cost_usd"] == pytest.approx(9.6e-6)
    assert fake.set_tags.call_args.args[0]["text_sha256"] == (
        hashlib.sha256(TEXT.encode()).hexdigest()
    )
