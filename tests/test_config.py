from decimal import Decimal

import pytest

from assistant.config import ApiSettings, WorkerSettings, _load_dotenv

BASE = {
    "GCP_PROJECT_ID": "p",
    "TELEGRAM_BOT_TOKEN": "t",
    "DEEPSEEK_API_KEY": "k",  # pragma: allowlist secret
}


def test_api_does_not_require_worker_secrets() -> None:
    s = ApiSettings.from_env(
        {"GCP_PROJECT_ID": "p", "WEBHOOK_SECRET_TOKEN": "s", "WEBHOOK_PATH": "x"}
    )
    assert s.updates_topic == "assistant-updates"


def test_api_requires_its_secrets() -> None:
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET_TOKEN"):
        ApiSettings.from_env({"GCP_PROJECT_ID": "p", "WEBHOOK_PATH": "x"})


def test_worker_defaults_and_overrides() -> None:
    s = WorkerSettings.from_env(BASE)
    assert s.llm_model == "deepseek-flash"
    assert s.max_llm_usd_per_day == Decimal("0.10")
    s = WorkerSettings.from_env({**BASE, "MAX_MSGS_PER_MINUTE": "3"})
    assert s.max_msgs_per_minute == 3


def test_dotenv_does_not_override(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nBOTJONH_X='a'\nGCP_PROJECT_ID=other\nnoequals\n")
    monkeypatch.setenv("GCP_PROJECT_ID", "keep")
    monkeypatch.delenv("BOTJONH_X", raising=False)
    _load_dotenv(str(env))
    import os

    assert os.environ["BOTJONH_X"] == "a"
    assert os.environ["GCP_PROJECT_ID"] == "keep"
    monkeypatch.delenv("BOTJONH_X")
