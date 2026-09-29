# personal-assistant-bot

A single-user Telegram bot that is a finance advisor and a calendar: it logs
expenses and income to Google Sheets, recommends budgets to save, schedules
appointments (medical, personal) on a dedicated Google Calendar, and sends
proactive reminders. It runs for one owner plus a small allowlist of beta
testers, on GCP for about $1–2/month (LLM tokens only).

- **Channel:** Telegram Bot API (webhook → Cloud Run).
- **LLM:** Gemini 2.5 Flash on the paid tier (the free tier may train on the
  data).
- **State:** Firestore (users, idempotency, preferences, budgets); the finance
  ledger lives in Google Sheets so it stays readable on the phone.
- **Observability:** the shared MLflow server in `jd-portfolio-shared`
  (prompt hash, tokens, latency, cost per turn; message text hashed).

## Architecture

```
Telegram ──webhook──▶ assistant-api (Cloud Run) ──▶ Pub/Sub assistant-updates ──▶ assistant-worker (Cloud Run)
Cloud Scheduler ─────────────────────────────────▶ Pub/Sub assistant-cron ───────▶ assistant-worker
                                                                                         │
                                            Gemini · Firestore · Sheets · Calendar · MLflow
```

Two Cloud Run services on purpose: Cloud Run does not guarantee CPU between
requests, so `assistant-api` acknowledges the webhook in under 300 ms and only
publishes to Pub/Sub; `assistant-worker` consumes, calls the LLM, writes and
replies. Idempotency by `update_id` in a Firestore transaction.

See [PLAN.md](PLAN.md) (Spanish) for the full plan, costs and roadmap.

## Stack

FastAPI, `google-genai` (Gemini), `google-cloud-*` (Firestore, Pub/Sub, Secret
Manager), `google-api-python-client` (Sheets/Calendar), MLflow, uv + ruff + mypy
+ pytest, Terraform, GitHub Actions.

## Run locally

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pre-commit install
uv run pytest
uv run mypy src
uv run ruff check . && uv run ruff format --check .
```

Local runs use a `.env` (git-ignored) with the same variables as the deploy;
see [Configuration](#configuration).

## Reproduce on GCP

1. Create a GCP project (globally unique id) and link it to the billing
   account, then apply the base infrastructure:

   ```bash
   gcloud auth application-default login
   cd infra
   cp terraform.tfvars.example terraform.tfvars   # set project_id, billing_account, github_repo
   terraform init
   terraform apply
   terraform output github_variables
   ```

2. Create the bot with @BotFather and store the secrets without echoing them:

   ```bash
   read -rs TOKEN && printf '%s' "$TOKEN" | gcloud secrets versions add assistant-bot-token --data-file=- --project "$PROJECT_ID"
   openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add assistant-webhook-secret --data-file=-
   openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add assistant-webhook-path --data-file=-
   read -rs KEY   && printf '%s' "$KEY"  | gcloud secrets versions add assistant-gemini-key --data-file=-
   ```

3. Share one spreadsheet and one dedicated calendar with the `assistant-worker`
   service account, then add their ids as `SPREADSHEET_ID` and `CALENDAR_ID`
   repository variables.

4. Configure GitHub: set the values from `terraform output github_variables`
   plus the environment-specific variables, then create the `staging`
   (branch `dev`) and `production` (branch `main`, required reviewer)
   environments.

5. Add yourself as the owner (your chat id from @userinfobot), with ADC
   pointed at the project. Owners invite beta users from the chat; a beta joins
   with `/start <code>`:

   ```bash
   GCP_PROJECT_ID="$PROJECT_ID" uv run python -m assistant.admin add-owner <chat_id> <nombre>
   for c in processed invites rate spend pending; do
     gcloud firestore fields ttls update expire_at --collection-group="$c" --enable-ttl --async
   done
   ```

   The loop enables TTL cleanup of the dedup markers, invites, counters and
   pending confirmations.

6. Register the webhook and start using the bot:

   ```bash
   curl "https://api.telegram.org/bot$TOKEN/setWebhook?url=$API_URL/tg/$PATH&secret_token=$SECRET"
   ```

Every merge into `dev` deploys `assistant-api-staging` / `assistant-worker-staging`;
merging `dev` into `main` deploys production after approval.

## Configuration

All secrets come from Secret Manager; settings from environment variables.

| Variable | Description |
|---|---|
| `GCP_PROJECT_ID` | GCP project |
| `TELEGRAM_BOT_TOKEN` | Secret `assistant-bot-token` |
| `WEBHOOK_SECRET_TOKEN` | Secret `assistant-webhook-secret` (X-Telegram-Bot-Api-Secret-Token) |
| `WEBHOOK_PATH` | Secret `assistant-webhook-path` (webhook route) |
| `GEMINI_API_KEY` | Secret `assistant-gemini-key` |
| `SPREADSHEET_ID` | Finance ledger (Gastos, Ingresos) |
| `CALENDAR_ID` | Dedicated calendar |
| `MLFLOW_TRACKING_URI` | Shared MLflow server |
| `GEMINI_MODEL` | Default `gemini-2.5-flash` |
| `MAX_LLM_USD_PER_DAY` | Daily LLM spend cap (fails closed) |

## Contributing

Changes go on a `feat/`, `fix/` or `chore/` branch cut from `dev` and merge
into `dev` through a pull request; merging `dev` into `main` releases. See
[CONTRIBUTING.md](CONTRIBUTING.md).
