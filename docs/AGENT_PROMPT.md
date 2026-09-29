# Prompt para agente de implementación — personal-assistant-bot

Eres un agente de ingeniería con acceso al repositorio en
`~/Code/portfolio/botjonh`. Tu trabajo: llevar el bot de Telegram de
"template" a "funcional (fases 1–5 del roadmap)". No reinventes lo que ya
existe; completa lo que falta. Donde este prompt y `PLAN.md` difieran, manda
este prompt.

## Antes de empezar

- `git init` si no hay repo; commit inicial en `main`, crea `dev` y trabaja en
  `feat/*` según `CONTRIBUTING.md` (Conventional Commits). Un commit por paso.
- Lee `PLAN.md`, `README.md`, `CONTRIBUTING.md`, `infra/main.tf`,
  `.github/workflows/deploy.yml` completos.

## Estado actual (ya existe)

- `infra/main.tf` — Firestore native, Artifact Registry, secretos, 3 cuentas de
  servicio, WIF, Pub/Sub (updates + cron), Cloud Scheduler ×3, budget-guard.
  Cloud Run lo despliega GitHub Actions, no Terraform.
- `src/assistant/channels/base.py`, `channels/telegram.py` — listos (se amplían,
  ver paso 3).
- `src/assistant/llm/prompts/system.md` — listo (se ajusta, ver paso 5).
- `src/assistant/config.py`, `api.py`, `worker.py`, `llm/tools.py` — esqueletos.
- `.github/workflows/{ci,deploy}.yml`, `Dockerfile`, `pyproject.toml` — solo se
  tocan donde este prompt lo pide.

## LLM: DeepSeek API, modelo `deepseek-flash`

- Endpoint: `POST https://api.deepseek.com/chat/completions` (formato OpenAI),
  header `Authorization: Bearer $DEEPSEEK_API_KEY`, `stream: false`. Cliente
  con `httpx` (ya instalado). **No** añadas el SDK `openai`; **quita**
  `google-genai` de `pyproject.toml`.
- `LLM_MODEL` por defecto `deepseek-flash`. `thinking: {"type": "disabled"}`
  (menos latencia y tokens; el razonamiento no hace falta para tool calling
  simple). `temperature: 0.2`.
- Tools: `tools=[{"type": "function", "function": {name, description,
  parameters, "strict": true}}]`, `tool_choice: "auto"`. La respuesta trae
  `choices[0].message.tool_calls[].function.arguments` como **string JSON**:
  `json.loads` y luego `validate_args`. JSON inválido = tool rechazada.
- Uso: `usage.prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`,
  `completion_tokens`. Costo por turno con precios en config (USD por 1M
  tokens, tarifa pico, septiembre 2026): `PRICE_IN_HIT=0.006`,
  `PRICE_IN_MISS=0.30`, `PRICE_OUT=1.20`.
- Caché de contexto: automática en DeepSeek por prefijo. Mantén el system
  prompt y las tools **idénticos y al inicio** de cada request para acertar
  caché; la fecha/hora actual va en un mensaje `system` corto **después** del
  prefijo fijo.
- Topes: `MAX_LLM_USD_PER_DAY` por chat (default 0.10) y `MAX_MSGS_PER_MINUTE`
  (default 10), en Firestore. Al superarlos, falla cerrado con un mensaje
  corto y sin llamar al LLM.
- 429 o 5xx de DeepSeek → devuelve 5xx al push de Pub/Sub para que reintente
  con backoff. Timeout del cliente: 30 s.
- Contexto mínimo: system prompt + últimos N=6 turnos del chat.

## Lo que FALTA implementar (en este orden)

1. **Config e infra**
   - `config.py`: separa `ApiSettings` (`GCP_PROJECT_ID`,
     `WEBHOOK_SECRET_TOKEN`, `WEBHOOK_PATH`) y `WorkerSettings` (el resto). El
     api no debe exigir secretos que no recibe. Renombra
     `gemini_*` → `DEEPSEEK_API_KEY`, `LLM_MODEL`, `PRICE_IN_HIT`,
     `PRICE_IN_MISS`, `PRICE_OUT`, `MAX_LLM_USD_PER_DAY`, `MAX_MSGS_PER_MINUTE`, `BACKUP_BUCKET`,
     `DEFAULT_TIMEZONE` (default `America/Panama`). `confirm_above_usd` →
     `confirm_above` (en la moneda del usuario).
   - `infra/main.tf`:
     - SA `webhook`: añade `roles/datastore.user` y `secretAccessor` sobre los
       secretos que usa el api (secret token y ruta). Hoy no puede leerlos ni
       deduplicar.
     - Secreto `assistant-gemini-key` → `assistant-deepseek-key`.
     - Bucket GCS de respaldo (uniform access, versioning, lifecycle 90 días) +
       `roles/storage.objectCreator` para la SA `worker` en ese bucket.
   - `deploy.yml` y `README.md`: refleja los nombres nuevos.
   - `terraform validate` y `terraform fmt -check` deben pasar (si `terraform`
     está instalado; si no, dilo en el resumen).
2. **`services/state.py`** — Firestore:
   - `users/{chat_id}`: `{nombre, rol: owner|beta, moneda, zona_horaria}`.
   - `processed/{update_id}`: idempotencia en **transacción** (si existe, se
     descarta). TTL de 7 días.
   - `invites/{codigo}`: código aleatorio (`secrets.token_urlsafe(16)`), un
     solo uso, expira en 24 h, lleva `nombre`.
   - `preferences/{chat_id}`: presupuesto por categoría, moneda, zona horaria.
   - Contadores por chat: USD de LLM del día y mensajes por minuto.
   - `pending/{token}`: confirmaciones pendientes (botones inline), expiran en
     10 min.
   - `history/{chat_id}`: últimos 6 turnos para contexto.
3. **`services/pubsub.py`** + **`api.py`** + **`worker.py`** en modo eco
   (Fase 1 del roadmap). Luego se amplía.
   - `api.py`: secret token + ruta en tiempo constante **antes** de parsear;
     acepta `message` y `callback_query`; allowlist; dedup por `update_id`;
     publica en `assistant-updates`; 200 en <300 ms.
     - Excepción a la allowlist: `/start <codigo>` desde un chat desconocido.
       El api valida y consume el código en transacción, crea `users/{chat_id}`
       con rol `beta` y publica el update. Código inválido → 200 sin hacer nada.
       Nunca llama al LLM.
   - `worker.py`: decodifica el envelope push, distingue `update` de `job`,
     ejecuta el turno, responde por el canal, loguea traza. Ack con 2xx; 5xx
     para reintentar (429/5xx del LLM, errores transitorios).
   - `channels/base.py`: añade `callback_data` y `callback_query_id` opcionales
     a `InboundMessage`.
4. **`services/sheets.py`** — ledger **append-only** (`Gastos`, `Ingresos`),
   `google-api-python-client` + ADC.
   - Columnas: `fecha, monto, moneda, categoria, nota, batch_id, update_id,
     tipo` (`registro` | `reverso`).
   - Montos con `Decimal`, redondeo a 2 decimales. Nunca `float` en la
     escritura.
   - Idempotencia por `(update_id, índice de ítem)`: un reintento no duplica
     filas.
   - "Deshacer" = añadir filas `reverso` con monto negativo y el mismo
     `batch_id`. Nunca se borra ni se edita una fila.
   - `resumen_finanzas` suma incluyendo reversos.
5. **`llm/tools.py`** + **`llm/client.py`**
   - Firma única del dispatch: `fn(ctx: ToolContext, **args) -> str`.
     `ToolContext` lleva `chat_id`, `rol`, `moneda`, `zona_horaria`,
     `update_id`, `ahora`. Las comprobaciones de rol (owner) las hace el
     **código**, nunca el LLM.
   - `validate_args`: un modelo pydantic por tool con `extra="forbid"`. Montos
     > 0, fechas ISO válidas, enums estrictos. Un argumento inválido nunca
     llega al servicio.
   - Cambios de esquema:
     - `registrar_gasto` acepta `items: [{monto, categoria, nota?}]` + `moneda`
       + `fecha`. Varios gastos por mensaje ("pan 2, leche 3").
     - `categoria` es un **enum fijo** mapeado a 50/30/20:
       - necesidades: `vivienda, servicios, supermercado, transporte, salud, deudas`
       - ocio: `restaurantes, entretenimiento, compras, viajes, suscripciones, otros`
       - ahorro: `ahorro, inversion`
     - `agregar_beta(chat_id, nombre)` → `invitar_beta(nombre)`: devuelve el
       código para que el owner se lo pase al beta.
     - `deshacer(batch_id?)`: sin argumento, deshace el último lote del chat.
   - Si el mensaje no tiene monto > 0, el LLM pide aclaración y no se escribe
     nada.
   - `client.py`: bucle de tool calling de máximo 3 rondas. Inyecta en el
     system prompt la fecha y hora actuales en la zona del usuario. El texto
     del usuario va **solo** como mensaje `user`, nunca interpolado en el
     system prompt. Si el modelo pide una tool fuera de la allowlist, se
     rechaza y se registra.
   - Ajusta `system.md` a los cambios de tools y sube `PROMPT_VERSION`.
6. **`services/budgets.py`** — presupuesto por categoría desde `preferences`
   o, si no hay, regla 50/30/20 sobre los ingresos del mes. Devuelve la
   categoría con mayor exceso. Puro cálculo, sin LLM.
7. **`services/calendar.py`** — crear/listar/cancelar eventos en
   `CALENDAR_ID`, con la zona del usuario.
   - `recordatorio` = evento de 15 min con recordatorio popup en `cuando`.
     Notifica la app de Google Calendar.
     `# ponytail: recordatorio via Calendar; migra a Cloud Tasks si el bot debe
     escribir por Telegram a una hora exacta.`
   - `cancelar_evento` y `deshacer` exigen un turno de confirmación
     (`pending` + botón inline). Igual para gastos por encima de `confirm_above`.
8. **`observability/trace.py`** — MLflow por turno: `prompt_version`, modelo,
   tool, latencia, tokens (cache hit, cache miss, salida), costo USD, resultado
   de validación. Texto del mensaje **hasheado (sha256)**, nunca en claro.
9. **`jobs/`** — `digest` (07:30), `checkin` (21:00), `weekly` (domingo 19:00).
   - `weekly` además hace el respaldo: colecciones Firestore a JSON en GCS, y
     los valores de las hojas `Gastos`/`Ingresos` (vía Sheets API) a JSON en
     GCS.
   - No uses `files.copy` de Drive: las SA no tienen cuota de Drive.
   - No crees un 4.º job de Scheduler.

## Reglas duras (no negociables)

- Cero PII en logs: ni `chat_id`, ni montos, ni títulos de eventos, ni texto
  del usuario, ni respuestas del LLM. Solo ids de documento, contadores y
  códigos de error.
- Escrituras financieras append-only; borrados, cancelaciones y deshacer
  exigen un turno de confirmación previo.
- El LLM solo emite JSON validado contra el esquema; el código ejecuta. Nunca
  URLs ni código generado por el modelo.
- API keys solo en headers, nunca en URLs.
- Clientes externos (`firestore.Client`, `build("sheets", ...)`, `pubsub_v1`,
  `storage.Client`, el `httpx.Client` de DeepSeek) se crean **lazy, dentro de
  funciones**, nunca en `import`, para que `pytest` corra sin credenciales.
- Todo lo testeable lleva tests con mocks (`respx` para DeepSeek y Telegram).
  Incluye tests de: header ausente → 403; ruta equivocada → 403; `chat_id`
  ajeno descartado sin llamar al LLM; `update_id` repetido no duplica filas;
  tool fuera de allowlist rechazada; argumentos extra rechazados; invitación
  usada dos veces rechazada; `arguments` con JSON inválido rechazado; 429 del
  LLM → 5xx; tope diario superado → no se llama al LLM.

## Fuera de alcance

- Foto de recibo (multimodal) y voz (Whisper): fase futura.
- Keel (`codejunkie99/keel`): herramienta de escritorio macOS para orquestar
  agentes de código. Es tooling del desarrollador, **no** dependencia del bot.
  No la añadas al repo.

## Definición de hecho

```bash
cd ~/Code/portfolio/botjonh
uv sync
uv run pytest --cov --cov-fail-under=80
uv run mypy src
uv run ruff check . && uv run ruff format --check .
```

Todo verde, y los módulos 1–9 existen y están cableados. No despliegues nada ni
apliques Terraform; deja código, tests y un resumen final con: qué quedó,
qué se saltó y por qué.
