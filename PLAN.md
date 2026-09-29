# Plan: Asistente personal de finanzas y agenda (Telegram)

Un bot de Telegram que es tu asesor de finanzas y tu agenda: registra gastos e
ingresos en Google Sheets, recomienda presupuestos para ahorrar, agenda citas
(médicas, personales) en Google Calendar y te avisa de forma proactiva. Costo
objetivo **≤ US$1/mes** (solo tokens del LLM), sin exposición de datos
personales.

- **Canal:** Telegram Bot API. Gratis, sin plantillas, sin ventana de 24 h.
- **Usuarios:** tú (owner) + unos pocos beta testers. Lista blanca explícita.
- **Nube:** GCP, reutilizando la infra ya montada en `portfolio-infra`.
- **Estado:** plan aprobado = fase 0.

---

## 1. Decisiones y por qué

| Tema | Decisión | Razón |
|---|---|---|
| Canal | **Telegram Bot API** | Gratis, mensajes proactivos libres, alta en minutos. WhatsApp queda descartado (ver §8). |
| Repo | **Un solo repo** `personal-assistant-bot` | Un producto = un repo = un proyecto GCP ([[0006-one-gcp-project-per-product]]). La infra compartida ya vive en `portfolio-infra`. |
| Cómputo | **Cloud Run**, facturación por request, scale to zero | Free tier cubre de sobra un bot personal. `min-instances=0` obligatorio. |
| Desacoplamiento | **2 servicios Cloud Run + Pub/Sub** | Cloud Run no garantiza CPU entre requests: un `BackgroundTasks` se pierde. Telegram reintenta si no hay 2xx. `api` ackea en <300 ms, `worker` piensa y escribe. |
| LLM | **DeepSeek API, `deepseek-flash`** | API pagada y barata (0.30/1M in miss, 0.006 hit, 1.20/1M out), caché de prefijo automática, tool calling. Riesgo aceptado: los datos se procesan en servidores de DeepSeek (China). Se envía el mínimo contexto. |
| Observabilidad | **MLflow compartido de `jd-portfolio-shared`** ([[0002-shared-mlflow-on-cloud-run]]) | Ya existe, cuesta $0 idle. **No** se monta un e2-micro propio: menos ops, mismo costo. El texto del mensaje se guarda hasheado. |
| Finanzas | **Google Sheets** (ledger) + **Firestore** (presupuestos, preferencias) | Sheet = legible/editable en el teléfono. Presupuestos son config estructurada por usuario → Firestore. |
| Agenda | **Google Calendar dedicado** | No toca tu calendario principal. |
| Proactivo | **Cloud Scheduler → Pub/Sub → worker** | Cloud Scheduler publica directo en Pub/Sub: no hacen falta endpoints `/cron/*` con OIDC. |
| Acceso a Google | **Cuenta de servicio** + compartir UN sheet y UN calendario | Sin refresh token OAuth personal. La SA solo ve lo que le compartes. |

Modelo de datos: **Firestore** = estado operativo (usuarios, idempotencia,
confirmaciones, preferencias, presupuestos, contadores de LLM). **Sheets** =
ledger (Gastos, Ingresos). **Calendar** = eventos.

---

## 2. Arquitectura

```
Tu Telegram / beta testers
      │
      ▼
[Telegram Bot API]──webhook POST──▶ [Cloud Run: assistant-api]        [Cloud Scheduler]
      ▲                                │  verifica secret-token              │
      │                                │  lista blanca de chat_id            │ publish
      │                                │  dedup por update_id                ▼
      │                                ▼                              [Pub/Sub: assistant-cron]
      │                          [Pub/Sub: assistant-updates]                │
      │                                │ push (OIDC)                   push (OIDC)
      │                                ▼                                 ▼
      │                          [Cloud Run: assistant-worker]
      │                                │
      └──sendMessage──────────────────┤
                                       ├─▶ DeepSeek flash (tool calling)
                                       ├─▶ Firestore   (estado, usuarios, presupuestos)
                                       ├─▶ Google Sheets (Gastos, Ingresos)
                                       ├─▶ Google Calendar (calendario dedicado)
                                       └─▶ MLflow compartido (tokens, latencia, costo)

Secret Manager → token del bot, secret token, ruta del webhook, key de DeepSeek
```

### Por qué dos servicios

`assistant-api` responde 200 en <300 ms: verifica `X-Telegram-Bot-Api-Secret-Token`
en tiempo constante, chequea la lista blanca, deduplica por `update_id` y publica
en Pub/Sub. No piensa, no escribe datos. `assistant-worker` consume, llama al LLM
con las herramientas permitidas, escribe en Sheets/Calendar/Firestore y responde.
La latencia del LLM deja de ser un problema de corrección.

### Servicios

1. **assistant-api** — webhook en `/tg/<ruta-aleatoria>`. 403 si el header no
   coincide; descarta sin gastar tokens si el `chat_id` no está en la lista
   blanca; dedup por `update_id` en transacción Firestore; publica en
   `assistant-updates`.
2. **assistant-worker** — suscriptor push de `assistant-updates` (mensajes de
   usuario) y `assistant-cron` (jobs proactivos). Llama al LLM, valida
   argumentos, ejecuta la escritura, responde por la Bot API, registra la traza
   en MLflow.
3. **Cloud Scheduler** — 3 jobs que publican en `assistant-cron`: resumen
   matutino (07:30), cierre del día (21:00) y revisión semanal (domingo 19:00).

### Flujo típico

- "Gasté 45 en el almuerzo con Ana" → worker → tool `registrar_gasto` → append en
  `Gastos` → "✓ 45 → almuerzo" con botón deshacer.
- "Agéndame dentista jueves 4pm" → `crear_evento` → calendario dedicado.
- "¿Cómo voy este mes?" → `resumen_finanzas` → cifras en 2 líneas.
- "¿Dónde puedo ahorrar?" → `recomendar_presupuesto` → comparación categoría vs
  presupuesto + sugerencia.

### Proactivo

Sin restricciones de ventana ni plantillas. Tres jobs de Cloud Scheduler generan
mensajes normales y gratis. Condición inicial única: cada usuario pulsa `/start`
una vez para que el bot guarde su `chat_id`.

---

## 3. Lista blanca de usuarios (owner + beta)

Colección Firestore `users/{chat_id}`: `{nombre, rol: owner|beta, moneda,
zona_horaria}`. Solo el `owner` puede ejecutar `invitar_beta` y `listar_usuarios`. El beta
entra con `/start <código>` (un solo uso, 24 h).
Cualquier `chat_id` fuera de la colección se descarta **sin gastar tokens**. Esto
mantiene la garantía de lista blanca del plan original y admite beta testers.

---

## 4. Herramientas del LLM (lista blanca)

| Herramienta | Argumentos | Efecto |
|---|---|---|
| `registrar_gasto` | items[{monto, categoria, nota?}], moneda, fecha | append en `Gastos` |
| `registrar_ingreso` | monto, moneda, fuente, fecha, nota? | append en `Ingresos` |
| `resumen_finanzas` | periodo (hoy/mes/semana) | lectura agregada del ledger |
| `recomendar_presupuesto` | periodo (mes) | categoría vs presupuesto + sugerencia de ahorro |
| `crear_evento` | titulo, inicio, fin?, ubicacion?, recordatorio_min? | insert en calendario dedicado |
| `listar_agenda` | rango (hoy/manana/semana) | lectura del calendario |
| `cancelar_evento` | evento_id, confirmado | borrado, requiere confirmación previa |
| `recordatorio` | texto, cuando | evento con aviso en el calendario dedicado |
| `deshacer` | batch_id? | filas de reverso (requiere confirmación) |
| `invitar_beta` | nombre | owner only: genera código de invitación |
| `listar_usuarios` | — | owner only: lista la allowlist |

`recomendar_presupuesto` es **basado en reglas**: el código agrega gastos por
categoría, los compara contra el presupuesto por categoría en
`preferences/{chat_id}`, y el LLM solo resume en ≤2 líneas. Sin presupuesto
configurado, se usa la regla 50/30/20 (necesidades/ocio/ahorro). Así el gasto de
tokens se mantiene bajo: el modelo no tiene que "calcular" el presupuesto, solo
presentarlo.

El system prompt y los esquemas viven en el repo como artefacto versionado
(`src/assistant/llm/prompts/system.md`); cada cambio sube la versión y queda
registrado en MLflow.

### Estilo de respuesta (requisito de producto)

El system prompt obliga a respuestas **muy concisas**:

- Máximo 2 líneas cortas. Sin saludos, sin frases de cierre, sin relleno.
- Cifras solo si el usuario las pide; redondeadas a 2 decimales.
- Máximo 1 emoji por respuesta; nunca tablas Markdown (Telegram no las renderiza).
- Confirma con el resultado en una línea: "✓ 45 → almuerzo".

---

## 5. Costos (tarifas verificadas, septiembre 2026)

| Componente | Franquicia / tarifa | Costo |
|---|---|---|
| Telegram Bot API | Gratis | $0.00 |
| Cloud Run ×2 | 2M requests, 180k vCPU-s, 360k GiB-s gratis/mes | $0.00 |
| Firestore | 1 GiB, 50k lecturas/día, 20k escrituras/día gratis | $0.00 |
| Pub/Sub ×2 topics | 10 GiB/mes gratis | $0.00 |
| Cloud Scheduler ×3 | 3 jobs gratis por cuenta de facturación | $0.00 |
| Secret Manager | 6 versiones activas, 10k accesos/mes gratis | $0.00 |
| Google Sheets / Calendar | Gratis (cuenta de servicio) | $0.00 |
| MLflow compartido | Cloud Run scale-to-zero + Neon free tier | $0.00 |
| DeepSeek flash | 0.30/1M in (0.006 con caché), 1.20/1M out | ~$0.5–1 |
| **Total** | | **≈ $0.5–1/mes** |

Trampas de facturación a evitar desde el día uno:

- `min-instances=1` en Cloud Run: acaba con el free tier. Debe ser `0`.
- IP estática / disco SSD en cualquier VM: se cobran. (No hay VMs en este diseño.)
- Egress >1 GB/mes se cobra. Irrelevante para un bot de texto.
- Alertas de presupuesto $1 y $5 **antes** de desplegar. Budget guard a $3.

---

## 6. Seguridad

**Autenticidad de entrada**
- `X-Telegram-Bot-Api-Secret-Token` comparado en tiempo constante *antes* de
  parsear. Telegram **no firma** el cuerpo; por eso la ruta del webhook es además
  secreta (`/tg/<32 aleatorios>`).
- Lista blanca de `chat_id` (colección `users`). Cualquier otro se descarta sin
  gastar tokens.
- Dedup por `update_id` en transacción Firestore (un reintento no duplica una fila).
- Push de Pub/Sub al worker con OIDC obligatorio (`--no-allow-unauthenticated`).

**Credenciales**
- Todo secreto en Secret Manager; cero secretos en repo o env en claro.
- Cuentas de servicio separadas: `assistant-webhook` (solo `pubsub.publisher`),
  `assistant-worker` (`firestore.user` + `secretAccessor` sobre secretos nombrados
  + acceso a UN sheet y UN calendario), `assistant-deploy` (GitHub WIF).
- Sin refresh token OAuth personal. La SA tiene Editor sobre un spreadsheet y
  escritura sobre un calendario dedicado; nunca Drive completo.

**Fuga por logs / LLM**
- Cero PII en Cloud Logging (ni `chat_id`, ni montos, ni títulos). Se registra id
  de documento y contador. Retención 30 días.
- MLflow guarda hash del prompt versionado + git sha + tokens + latencia +
  herramienta. El texto se guarda hasheado, no en claro.
- Se envía el mínimo contexto (nunca el ledger completo); el modelo emite JSON
  validado y el código ejecuta (nunca URLs o código generado por el modelo).
- Escrituras financieras append-only; operaciones por encima de un monto
  configurable o borrados de eventos exigen turno de confirmación.
- Tope diario de gasto de LLM (USD) y de mensajes/minuto (token bucket en Firestore).

**En reposo y respaldo**
- Firestore, Sheets, GCS y Calendar cifran en reposo. Respaldo semanal: vuelca
  colecciones a JSON en GCS + copia del spreadsheet.
- Rotación de token del bot y key de DeepSeek cada 90 días.

---

## 7. Estructura del repositorio (un solo repo)

```
personal-assistant-bot/
├── PLAN.md
├── README.md                 # arquitectura + run local + deploy
├── CONTRIBUTING.md           # flujo de ramas (idéntico a uplift)
├── pyproject.toml            # uv + ruff + mypy + pytest
├── Dockerfile                # una imagen, dos entrypoints (api / worker)
├── .github/
│   ├── workflows/ci.yml      # lint, mypy, pytest (cov ≥80), detect-secrets, gitleaks, pip-audit, build
│   ├── workflows/deploy.yml  # dev→staging, main→prod, WIF, SHAs fijados
│   └── pull_request_template.md
├── infra/
│   ├── main.tf               # APIs, buckets, SA, WIF, secrets, scheduler, budget guard
│   └── terraform.tfvars.example
├── src/assistant/
│   ├── config.py             # settings desde Secret Manager
│   ├── api.py                # webhook: secret token, allowlist, dedup, publish
│   ├── worker.py             # consumidor Pub/Sub (updates + cron)
│   ├── channels/
│   │   ├── base.py           # interfaz común de canal
│   │   └── telegram.py       # sendMessage, botones inline, /start
│   ├── llm/
│   │   ├── client.py         # DeepSeek flash, tope de gasto
│   │   ├── tools.py          # esquemas + validación (lista blanca)
│   │   └── prompts/system.md # prompt versionado (estilo conciso)
│   ├── services/
│   │   ├── sheets.py         # append-only, cuenta de servicio
│   │   ├── calendar.py       # calendario dedicado
│   │   ├── state.py          # Firestore: usuarios, idempotencia, preferencias
│   │   └── budgets.py        # reglas 50/30/20 + comparación vs presupuesto
│   ├── jobs/                 # digest, checkin, weekly, backup
│   └── observability/trace.py# MLflow compartido (texto hasheado)
└── tests/
```

¿Más de un repo? **No.** Un producto = un repo (misma regla que uplift). La
infra compartida (MLflow, módulo `budget-guard`) ya vive en `portfolio-infra` y se
consume como módulo pineado por commit. El vault de Obsidian no es un repo de
producto, es la base de conocimiento.

---

## 8. Alternativas descartadas

- **WhatsApp Cloud API**: plantillas facturadas (US$0.0113/msg a Panamá),
  alta de 1–3 semanas, ventana de 24 h, chip +507 que mantener vivo. Para un
  asistente personal que avisa "cuando necesite", Telegram elimina todo eso.
- **e2-micro para todo (long polling, $0)**: válido, no expone endpoint, pero
  obliga a gestionar reinicios/parches con 1 GB compartido. Plan C si Cloud Run
  diera problemas.
- **MLflow propio en e2-micro**: descartado; el compartido ya existe y cuesta $0.
- **Cloudflare Workers / AWS Lambda**: sin Secret Manager/Firestore equivalentes,
  y Sheets/Calendar/Gemini quedarían fuera del proveedor. Más piezas, no menos.

---

## 9. Roadmap

**Fase 0 — Cimientos (1 h)** — Bot con @BotFather, token + secret token + ruta
en Secret Manager, `/start` captura `chat_id`, proyecto GCP nuevo, presupuestos
$1/$5, budget guard a $3, `git init`.
*AC:* `getMe` responde; `getWebhookInfo` muestra `pending_update_count: 0`.

**Fase 1 — Eco (1 día)** — `assistant-api` (secret token + ruta + allowlist +
dedup) y `assistant-worker` que responde texto fijo.
*AC:* responde <5 s; POST sin header → 403; `update_id` repetido no duplica.

**Fase 2 — Finanzas (2 días)** — SA + compartir spreadsheet; `registrar_gasto`,
`registrar_ingreso`, `resumen_finanzas`, idempotencia real, botón deshacer.
*AC:* "gasté 45 en almuerzo" crea exactamente una fila; "¿cuánto llevo este mes?"
cuadra con la suma del Sheet.

**Fase 3 — Presupuesto (1 día)** — `preferences` con presupuesto por categoría,
`recomendar_presupuesto` (reglas + resumen del LLM), fallback 50/30/20.
*AC:* "¿dónde ahorro?" devuelve ≤2 líneas con la categoría que más excede.

**Fase 4 — Calendario (1 día)** — Calendario dedicado compartido con la SA,
`crear_evento`/`listar_agenda`.
*AC:* "dentista jueves 4pm" aparece en el calendario dedicado con recordatorio.

**Fase 5 — Proactivo + beta (1 día)** — 3 jobs de Cloud Scheduler → Pub/Sub,
tope de gasto de LLM, `invitar_beta`/`listar_usuarios`, botones de confirmación.
*AC:* 07:30 llega el resumen sin intervención; un beta tester con `/start <código>` queda
en la allowlist; un `chat_id` ajeno se descarta sin gastar tokens.

**Fase 6 — Observabilidad (1 día)** — Traza MLflow compartido (hash de prompt,
tokens, latencia, costo, herramienta) + logging sin PII.
*AC:* una traza por turno; comparación entre dos versiones de prompt en MLflow.

**Fase 7 — Endurecimiento (1 día)** — Rotación de claves, respaldo semanal a GCS,
retención de logs, revisión IAM, prueba casera (header ausente, ruta equivocada,
`chat_id` ajeno, intento de inyección).
*AC:* los cuatro intentos fallan cerrado y quedan registrados.

---

## 10. Riesgos

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Telegram no firma webhooks | Inyección si se descubre la URL | Ruta aleatoria + secret token + allowlist; sin los tres no se procesa |
| Latencia del LLM → reintentos | Filas duplicadas | Ack rápido + Pub/Sub + idempotencia por `update_id` |
| Cloud Run congela CPU entre requests | Tareas en background perdidas | Nada de hilos; todo vía Pub/Sub |
| 429/5xx de DeepSeek | Respuestas demoradas | 5xx al push de Pub/Sub para reintentar con backoff; modelo configurable |
| Datos financieros procesados en China | Fuga regulatoria/privacidad | Contexto mínimo, sin nombres ni ledger completo; cambiar `LLM_MODEL`/proveedor si pasa a importar |
| Free tier roto por opción mal elegida | Factura inesperada | Trampas en §5 + presupuestos + budget guard $3 |
| Fuga de datos financieros | Irreversible | Allowlist, SA (no OAuth personal), tier pagado, cero PII, append-only |
| Bloqueo del bot / pérdida de `chat_id` | El bot no puede escribirte | `chat_id` en Firestore con respaldo semanal; `/start` lo recupera |
