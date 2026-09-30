"""Settings from environment variables.

Cloud Run injects Secret Manager values as environment variables (see
.github/workflows/deploy.yml), so this module reads plain env vars. Locally, a
git-ignored `.env` is loaded for development.

The api and the worker get separate settings: the api must not require secrets
it never receives.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import cache


def _load_dotenv(path: str = ".env") -> None:
    """Best-effort .env loader for local development only."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(env: Mapping[str, str] | None) -> dict[str, str]:
    if env is None:
        _load_dotenv()
        return dict(os.environ)
    return dict(env)


def _require(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class ApiSettings:
    project_id: str
    webhook_secret_token: str
    webhook_path: str
    updates_topic: str = "assistant-updates"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApiSettings:
        values = _env(env)
        return cls(
            project_id=_require(values, "GCP_PROJECT_ID"),
            webhook_secret_token=_require(values, "WEBHOOK_SECRET_TOKEN"),
            webhook_path=_require(values, "WEBHOOK_PATH"),
            updates_topic=values.get("UPDATES_TOPIC", "assistant-updates"),
        )


@dataclass(frozen=True)
class WorkerSettings:
    project_id: str
    telegram_bot_token: str
    deepseek_api_key: str
    # Reminders: Cloud Tasks POST to {worker_url}/tasks/reminder with an OIDC
    # token for worker_sa. Empty worker_url or worker_sa: reminders are skipped.
    worker_url: str = ""
    worker_sa: str = ""
    tasks_queue: str = "assistant-reminders"
    tasks_location: str = "us-central1"
    # Public assistant-api URL, for the ICS subscription link.
    api_url: str = ""
    # Cloud KMS key that encrypts each user's secret iCal URL. Empty: /conectar
    # is refused, so the URL is never stored in clear.
    kms_key: str = ""
    mlflow_tracking_uri: str = ""
    backup_bucket: str = ""
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    # USD per 1M tokens (DeepSeek peak rate, September 2026).
    price_in_hit: Decimal = Decimal("0.006")
    price_in_miss: Decimal = Decimal("0.30")
    price_out: Decimal = Decimal("1.20")
    max_llm_usd_per_day: Decimal = Decimal("0.10")
    max_msgs_per_minute: int = 10
    # In the user's currency; above it a write needs a confirmation turn.
    confirm_above: Decimal = Decimal("100")
    default_timezone: str = "America/Panama"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerSettings:
        values = _env(env)

        def get(name: str, default: str) -> str:
            return values.get(name, "").strip() or default

        return cls(
            project_id=_require(values, "GCP_PROJECT_ID"),
            telegram_bot_token=_require(values, "TELEGRAM_BOT_TOKEN"),
            deepseek_api_key=_require(values, "DEEPSEEK_API_KEY"),
            worker_url=get("WORKER_URL", "").rstrip("/"),
            worker_sa=get("WORKER_SA", ""),
            tasks_queue=get("TASKS_QUEUE", "assistant-reminders"),
            tasks_location=get("TASKS_LOCATION", "us-central1"),
            api_url=get("API_URL", "").rstrip("/"),
            kms_key=get("KMS_KEY", ""),
            mlflow_tracking_uri=get("MLFLOW_TRACKING_URI", ""),
            backup_bucket=get("BACKUP_BUCKET", ""),
            llm_model=get("LLM_MODEL", "deepseek-flash"),
            llm_base_url=get("LLM_BASE_URL", "https://api.deepseek.com"),
            price_in_hit=Decimal(get("PRICE_IN_HIT", "0.006")),
            price_in_miss=Decimal(get("PRICE_IN_MISS", "0.30")),
            price_out=Decimal(get("PRICE_OUT", "1.20")),
            max_llm_usd_per_day=Decimal(get("MAX_LLM_USD_PER_DAY", "0.10")),
            max_msgs_per_minute=int(get("MAX_MSGS_PER_MINUTE", "10")),
            confirm_above=Decimal(get("CONFIRM_ABOVE", "100")),
            default_timezone=get("DEFAULT_TIMEZONE", "America/Panama"),
        )


@cache
def get_api_settings() -> ApiSettings:
    return ApiSettings.from_env()


@cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings.from_env()
