# Module contracts

Three branches build the worker in parallel. Each module imports the others
lazily (inside functions) and tests them with mocks, so each branch passes CI
on its own. Signatures here are the contract; change them only in this file.

All tool implementations: `fn(ctx: ToolContext, **args) -> str`. The string is
short data for the LLM to summarize, never PII in logs. Settings come from
`assistant.config.get_worker_settings()`; shared types from
`assistant.context`.

## services/state.py (Firestore)

```python
def get_user(chat_id: str) -> dict | None            # users/{chat_id}
def upsert_user(chat_id: str, nombre: str, rol: str, moneda: str = "USD",
                zona_horaria: str = "America/Panama") -> None
def mark_processed(update_id: int) -> bool           # True if new; transaction, 7-day TTL field
def unmark_processed(update_id: int) -> None         # api: undo when publish fails
def redeem_invite(code: str, chat_id: str) -> bool   # transaction: single use, 24 h, creates beta user
def check_rate(chat_id: str, limit_per_minute: int) -> bool   # True if allowed
def llm_spend_today(chat_id: str) -> Decimal
def add_llm_spend(chat_id: str, usd: Decimal) -> None
def get_preferences(chat_id: str) -> dict            # {"presupuesto": {categoria: str(Decimal)}, ...}
def create_pending(chat_id: str, action: dict) -> str   # token, expires in 10 min
def pop_pending(chat_id: str, token: str) -> dict | None
def get_history(chat_id: str) -> list[dict]          # last 6 turns, OpenAI message format
def append_history(chat_id: str, messages: list[dict]) -> None
def set_last_batch(chat_id: str, batch_id: str) -> None
def last_batch(chat_id: str) -> str | None
def list_chat_ids() -> list[str]                     # for cron jobs
# tools
def invitar_beta(ctx, nombre: str) -> str            # owner only, checked in code
def listar_usuarios(ctx) -> str                      # owner only
```

## services/ledger.py, budgets.py, calendar.py

```python
# ledger: Firestore ledger/{chat_id}/movimientos, append-only, Decimal amounts as
# strings, idempotent by doc id ({update_id}-{i}, {update_id}-i0, {batch_id}-r{i})
def registrar_gasto(ctx, items: list[dict], moneda: str, fecha: date) -> str
def registrar_ingreso(ctx, monto: Decimal, moneda: str, fuente: str, fecha: date,
                      nota: str | None = None) -> str
def resumen_finanzas(ctx, periodo: str) -> str       # hoy|semana|mes
def deshacer(ctx, batch_id: str | None = None) -> str   # appends reverso rows
def gastos_por_categoria(chat_id: str, desde: date, hasta: date) -> dict[str, Decimal]
def total_ingresos(chat_id: str, desde: date, hasta: date) -> Decimal
def movimientos(chat_id: str, campo: str, desde, hasta) -> list[dict]  # desde <= campo < hasta
# budgets: pure rules, no LLM
def recomendar_presupuesto(ctx, periodo: str = "mes") -> str
# calendar: dedicated CALENDAR_ID, user's zone
def crear_evento(ctx, titulo: str, inicio: datetime, fin: datetime | None = None,
                 ubicacion: str | None = None, recordatorio_min: int | None = None) -> str
def listar_agenda(ctx, rango: str) -> str            # hoy|manana|semana
def cancelar_evento(ctx, evento_id: str) -> str
def recordatorio(ctx, texto: str, cuando: datetime) -> str
```

## jobs/__init__.py

```python
def run_job(name: str) -> None   # digest|checkin|weekly; digest exports yesterday's
                                 # ledger CSV, weekly backs up JSON to GCS
```

## llm/client.py, llm/tools.py

```python
class LLMUnavailable(Exception): ...   # 429/5xx/timeout: worker returns 5xx to Pub/Sub

@dataclass
class TurnResult:
    reply: str
    keyboard: list[list[tuple[str, str]]] | None   # confirmation buttons
    messages: list[dict]                           # new turns to append to history
    tools: list[str]
    prompt_version: str
    model: str
    tokens_hit: int
    tokens_miss: int
    tokens_out: int
    cost_usd: Decimal
    rejected: int                                  # tool calls rejected by validation

def run_turn(ctx: ToolContext, text: str, history: list[dict]) -> TurnResult
def execute_pending(ctx: ToolContext, token: str) -> str   # tools.py; after "ok:<token>" button
```

Confirmation buttons use `callback_data` `ok:<token>` and `no:<token>`.

## observability/trace.py

```python
def record_turn(result: TurnResult, latency_ms: int, text: str) -> None
```

Always emits one structured JSON log line (no PII; `text` only as sha256).
Then sends the run to MLflow best-effort: 3 s timeout, identity token for the
private Cloud Run URL, any error only logged. Skipped when
`MLFLOW_TRACKING_URI` is empty. Never raises.

## Worker order per update

1. Rate limit and daily cap (no LLM call when exceeded).
2. `run_turn`; `LLMUnavailable` -> 503.
3. Send the reply via Telegram.
4. `add_llm_spend`, `append_history`.
5. `record_turn`.
6. Return 2xx.
