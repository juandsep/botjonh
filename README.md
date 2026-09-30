# botjonh

A single-user Telegram bot that is a finance advisor and a calendar: it logs
expenses and income to a Firestore ledger, recommends budgets to save, schedules
appointments (medical, personal) on a dedicated Google Calendar, and sends
proactive reminders. It runs for one owner plus a small allowlist of beta
testers, on GCP for about $1–2/month (LLM tokens only).

- **Channel:** Telegram Bot API (webhook → Cloud Run).
- **LLM:** DeepSeek `deepseek-flash` over its HTTP API (tool calling,
  automatic prefix cache). Minimal context is sent: never the full ledger.
- **State:** Firestore (users, idempotency, preferences, budgets, and the
  append-only finance ledger).
- **Reporting:** the ledger is exported daily as CSV to GCS; a BigQuery external
  table over those files feeds a Looker Studio dashboard, at about $0.
- **Observability:** the shared MLflow server in `jd-portfolio-shared`
  (prompt hash, tokens, latency, cost per turn; message text hashed).

## Architecture

```
Telegram ──webhook──▶ assistant-api (Cloud Run) ──▶ Pub/Sub assistant-updates ──▶ assistant-worker (Cloud Run)
Cloud Scheduler ─────────────────────────────────▶ Pub/Sub assistant-cron ───────▶ assistant-worker
                                                                                         │
                                            DeepSeek · Firestore · Calendar · MLflow
```

Two Cloud Run services on purpose: Cloud Run does not guarantee CPU between
requests, so `assistant-api` acknowledges the webhook in under 300 ms and only
publishes to Pub/Sub; `assistant-worker` consumes, calls the LLM, writes and
replies. Idempotency by `update_id` in a Firestore transaction.

See [PLAN.md](PLAN.md) (Spanish) for the full plan, costs and roadmap.

## Stack

FastAPI, httpx (DeepSeek, Telegram), `google-cloud-*` (Firestore, Pub/Sub,
Storage), `google-api-python-client` (Calendar), `mlflow-skinny`, uv + ruff + mypy
+ pytest, Terraform, GitHub Actions.

## Run locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

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
   read -rs KEY   && printf '%s' "$KEY"  | gcloud secrets versions add assistant-deepseek-key --data-file=-
   ```

3. Share one dedicated calendar with the `assistant-worker` service account,
   then add its id as the `CALENDAR_ID` repository variable.

4. Configure GitHub: set the values from `terraform output github_variables`
   plus the environment-specific variables, then create the `staging`
   (branch `dev`) and `production` (branch `main`)
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
merging `dev` into `main` deploys production.

## Finance ledger and reporting

Firestore is the source of truth: `ledger/{chat_id}/movimientos/{doc_id}`, one
append-only document per movement with `fecha` (ISO date), `monto` (string,
2 decimals), `moneda`, `categoria` (gasto) or `fuente` (ingreso), `tipo_mov`
(`gasto`|`ingreso`), `nota`, `batch_id`, `update_id`, `tipo`
(`registro`|`reverso`) and `creado`. Doc ids make writes idempotent: gasto
`{update_id}-{i}`, ingreso `{update_id}-i0`, undo `{batch_id}-r{i}` (negative
`reverso` copies; nothing is edited or deleted).

Every morning the `digest` job exports the previous day's writes (America/Panama)
to `gs://$BACKUP_BUCKET/ledger/mes=YYYY-MM/YYYY-MM-DD.csv` with the header
`fecha,chat_id,tipo_mov,categoria,monto,moneda,nota,batch_id,tipo`. Files are
create-only and kept forever; the weekly JSON backup lives under `backup/` with a
90-day lifecycle. Sum `monto` to net out undos (`reverso` rows are negative).

Terraform creates the BigQuery external table `botjonh.ledger` over those files
(`terraform output bigquery_ledger_table`). To build the dashboard:

1. Open [lookerstudio.google.com](https://lookerstudio.google.com) → **Create** →
   **Data source** → **BigQuery** → project `jd-botjonh` → dataset `botjonh` →
   table `ledger` → **Connect**.
2. Add a calculated field `bucket` for the 50/30/20 rule:

   ```
   CASE
     WHEN categoria IN ("vivienda","servicios","supermercado","transporte","salud","deudas") THEN "necesidades"
     WHEN categoria IN ("ahorro","inversion") THEN "ahorro"
     ELSE "ocio"
   END
   ```

3. Suggested charts (filter `tipo_mov = gasto` unless noted): monthly spend
   trend (time series, `fecha` by month, SUM `monto`); spend by `categoria`
   (bar); 50/30/20 split by `bucket` (pie) next to income (`tipo_mov = ingreso`).

## Configuration

All secrets come from Secret Manager; settings from environment variables.

| Variable | Description |
|---|---|
| `GCP_PROJECT_ID` | GCP project |
| `TELEGRAM_BOT_TOKEN` | Secret `assistant-bot-token` |
| `WEBHOOK_SECRET_TOKEN` | Secret `assistant-webhook-secret` (X-Telegram-Bot-Api-Secret-Token) |
| `WEBHOOK_PATH` | Secret `assistant-webhook-path` (webhook route) |
| `DEEPSEEK_API_KEY` | Secret `assistant-deepseek-key` |
| `CALENDAR_ID` | Dedicated calendar |
| `MLFLOW_TRACKING_URI` | Shared MLflow server |
| `LLM_MODEL` | Default `deepseek-flash` |
| `BACKUP_BUCKET` | Weekly JSON backup and daily ledger CSV (from `terraform output`) |
| `MAX_MSGS_PER_MINUTE` | Per-chat rate limit (default 10) |
| `MAX_LLM_USD_PER_DAY` | Daily LLM spend cap per chat, default 0.10 (fails closed) |

## Contributing

Changes go on a `feat/`, `fix/` or `chore/` branch cut from `dev` and merge
into `dev` through a pull request; merging `dev` into `main` releases. See
[CONTRIBUTING.md](CONTRIBUTING.md).
