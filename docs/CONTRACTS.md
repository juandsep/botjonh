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
def ics_token(chat_id: str, rotate: bool = False) -> str   # ics_tokens/{token}; rotate revokes
def chat_for_ics_token(token: str) -> str | None     # format checked before any lookup
# tools
def invitar_beta(ctx, nombre: str) -> str            # owner only, checked in code
def listar_usuarios(ctx) -> str                      # owner only
```

## services/ledger.py, budgets.py, agenda.py, busy.py

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
# agenda: Firestore agenda/{chat_id}/eventos/{evento_id}, user's zone.
# evento_id = {update_id} ({update_id}-{n} for another item of the same turn),
# create() so a retry answers the same; cancel sets estado=cancelado (never
# deletes). A reminder (recordatorio: at cuando; evento: inicio - recordatorio_min)
# within 30 days is a Cloud Task r-<sha256(chat_id:evento_id)[:32]>; the digest
# enqueues later ones once they enter the 30-day horizon.
def crear_evento(ctx, titulo: str, inicio: datetime, fin: datetime | None = None,
                 ubicacion: str | None = None, recordatorio_min: int | None = None) -> str
def listar_agenda(ctx, rango: str) -> str            # hoy|manana|semana
def agenda(ctx, rango: str) -> list[str]             # lines for the digest
def cancelar_evento(ctx, evento_id: str) -> str
def recordatorio(ctx, texto: str, cuando: datetime) -> str   # 15 min, reminder at cuando
def ver_libres(ctx, fecha: date) -> str              # tool over libres()
def conflictos(ctx, inicio: datetime, fin: datetime) -> list[str]  # "Dentista 09:00–10:00"
def libres(ctx, dia: date) -> list[str]              # free slots 08:00–20:00, "11:00–19:00"
def semana(ctx) -> str                               # /calendario, no LLM
def encolar_recordatorios(ctx) -> None               # digest: reminders now within 30 days
def aviso(chat_id: str, evento_id: str) -> str | None   # "⏰ <titulo> <HH:MM>"; None if gone
def ics(chat_id: str, ahora: datetime) -> str        # VCALENDAR, -30 d to +365 d
# busy (separate branch): external ICS busy blocks. Loaded with importlib; when
# missing or raising, the agenda ignores it (logs the error class only).
def ocupados(chat_id: str, desde: datetime, hasta: datetime) -> list[tuple[datetime, datetime, str]]
def conectar(ctx, url: str) -> str                   # /conectar <url>
```

Conflicts: `llm/tools.py` calls `agenda.conflictos` before `crear_evento` and
`recordatorio` (only `tipo=evento` items and busy blocks count). On a conflict
the call is stored as pending and the user gets "Choca con … ¿Agendo igual?"
with Confirmar/Cancelar; `execute_pending` runs it without checking again.

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

## Worker routes without the LLM

- `/calendario` (next 7 days), `/calendario enlace` (ICS URL, token created if
  missing), `/calendario nuevo` (rotate), `/conectar <url>` (`busy.conectar`).
- `POST /tasks/reminder` `{"chat_id", "evento_id"}` from Cloud Tasks: sends
  `agenda.aviso`; always 2xx except a Firestore failure.
- api `GET /ics/{token}.ics`: public, 404 for a bad or unknown token.

## Worker order per update

1. Rate limit and daily cap (no LLM call when exceeded).
2. `run_turn`; `LLMUnavailable` -> 503.
3. Send the reply via Telegram.
4. `add_llm_spend`, `append_history`.
5. `record_turn`.
6. Return 2xx.
