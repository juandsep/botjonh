"""Tool schemas (allowlist), validation and dispatch.

The LLM emits JSON; one pydantic model per tool validates it (strict, no extra
keys) and only then the code calls the service. Anything not in ``TOOLS`` is
rejected before any side effect. Services are imported lazily by name so this
module imports without credentials or the service modules themselves.
"""

from __future__ import annotations

import copy
import importlib
import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    WithJsonSchema,
)

from assistant.config import get_worker_settings
from assistant.context import CATEGORIES, ToolContext


class ToolRejected(ValueError):
    """A tool call that never reaches a service. The message is a short code."""


def _categoria(value: str) -> str:
    if value not in CATEGORIES:
        raise ValueError("categoria fuera del enum")
    return value


Monto = Annotated[
    Decimal, Field(gt=0), WithJsonSchema({"type": "number", "exclusiveMinimum": 0})
]
Moneda = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="ISO 4217, ej. USD")]
Categoria = Annotated[
    str,
    AfterValidator(_categoria),
    WithJsonSchema({"type": "string", "enum": list(CATEGORIES)}),
]


class _Args(BaseModel):
    # strict: JSON strings for dates/datetimes, exact decimals, no coercion.
    model_config = ConfigDict(extra="forbid", strict=True)


class Item(_Args):
    monto: Monto
    categoria: Categoria
    nota: str | None = None


class RegistrarGasto(_Args):
    """Registra uno o varios gastos (append-only)."""

    items: list[Item] = Field(min_length=1, max_length=20)
    moneda: Moneda
    fecha: date


class RegistrarIngreso(_Args):
    """Registra un ingreso (append-only)."""

    monto: Monto
    moneda: Moneda
    fuente: str
    fecha: date
    nota: str | None = None


class ResumenFinanzas(_Args):
    """Resumen de gastos e ingresos del periodo."""

    periodo: Literal["hoy", "semana", "mes"]


class RecomendarPresupuesto(_Args):
    """Compara gasto por categoría contra el presupuesto y sugiere ahorro."""

    periodo: Literal["mes"]


class CrearEvento(_Args):
    """Crea un evento en la agenda (hora local del usuario)."""

    titulo: str
    inicio: datetime
    fin: datetime | None = None
    ubicacion: str | None = None
    recordatorio_min: int | None = Field(default=None, ge=0, le=40320)


class ListarAgenda(_Args):
    """Lista los eventos del rango."""

    rango: Literal["hoy", "manana", "semana"]


class CancelarEvento(_Args):
    """Cancela un evento por id (el usuario confirma con un botón)."""

    evento_id: str


class Recordatorio(_Args):
    """Crea un recordatorio a una hora (hora local del usuario)."""

    texto: str
    cuando: datetime


class VerLibres(_Args):
    """Huecos libres de 08:00 a 20:00 de un día (agenda y calendario conectado)."""

    fecha: date


class Deshacer(_Args):
    """Deshace un lote de registros; sin batch_id, el último (con confirmación)."""

    batch_id: str | None = None


Indice = Annotated[int, Field(ge=1, le=50, description="1 = el más reciente")]


class UltimosMovimientos(_Args):
    """Últimos movimientos numerados (1 = el más reciente)."""

    n: int = Field(default=5, ge=1, le=20)


class EditarMovimiento(_Args):
    """Corrige un movimiento por índice; solo cambia los campos no nulos."""

    indice: Indice
    monto: Monto | None = None
    moneda: Moneda | None = None
    categoria: Categoria | None = None
    nota: str | None = None


class AnularMovimiento(_Args):
    """Anula un movimiento por índice (el usuario confirma con un botón)."""

    indice: Indice


class InvitarBeta(_Args):
    """Solo owner: genera un código de invitación para un beta tester."""

    nombre: str


class ListarUsuarios(_Args):
    """Solo owner: lista los usuarios permitidos."""


# name -> (args model, "module:function"). Order is the order sent to the LLM.
TOOLS: dict[str, tuple[type[_Args], str]] = {
    "registrar_gasto": (RegistrarGasto, "assistant.services.ledger:registrar_gasto"),
    "registrar_ingreso": (
        RegistrarIngreso,
        "assistant.services.ledger:registrar_ingreso",
    ),
    "resumen_finanzas": (
        ResumenFinanzas,
        "assistant.services.ledger:resumen_finanzas",
    ),
    "recomendar_presupuesto": (
        RecomendarPresupuesto,
        "assistant.services.budgets:recomendar_presupuesto",
    ),
    "crear_evento": (CrearEvento, "assistant.services.agenda:crear_evento"),
    "listar_agenda": (ListarAgenda, "assistant.services.agenda:listar_agenda"),
    "cancelar_evento": (CancelarEvento, "assistant.services.agenda:cancelar_evento"),
    "recordatorio": (Recordatorio, "assistant.services.agenda:recordatorio"),
    "ver_libres": (VerLibres, "assistant.services.agenda:ver_libres"),
    "deshacer": (Deshacer, "assistant.services.ledger:deshacer"),
    "ultimos_movimientos": (
        UltimosMovimientos,
        "assistant.services.ledger:ultimos_texto",
    ),
    "editar_movimiento": (EditarMovimiento, "assistant.services.ledger:editar"),
    "anular_movimiento": (AnularMovimiento, "assistant.services.ledger:anular"),
    "invitar_beta": (InvitarBeta, "assistant.services.state:invitar_beta"),
    "listar_usuarios": (ListarUsuarios, "assistant.services.state:listar_usuarios"),
}
OWNER_ONLY = frozenset({"invitar_beta", "listar_usuarios"})

# DeepSeek strict mode supports neither these keywords nor date formats.
_DROP = {"title", "default", "format", "minLength", "maxLength", "minItems", "maxItems"}
_DATE_PATTERNS = {
    "date": r"^\d{4}-\d{2}-\d{2}$",
    "date-time": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
}


def _strict_schema(node: Any, defs: dict[str, Any]) -> Any:
    """Inline $refs, drop unsupported keywords, make every property required."""
    if isinstance(node, list):
        return [_strict_schema(n, defs) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _strict_schema(copy.deepcopy(defs[node["$ref"].split("/")[-1]]), defs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            out[key] = {k: _strict_schema(v, defs) for k, v in value.items()}
        elif key == "format" and value in _DATE_PATTERNS:
            out["pattern"] = _DATE_PATTERNS[value]
        elif key not in _DROP and key != "$defs":
            out[key] = _strict_schema(value, defs)
    if out.get("type") == "object":
        out.setdefault("properties", {})
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _spec(name: str, model: type[_Args]) -> dict[str, Any]:
    schema = model.model_json_schema()
    params = _strict_schema(schema, schema.get("$defs", {}))
    params.pop("description", None)  # already the function description
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (model.__doc__ or "").strip(),
            # No "strict": DeepSeek only honours it on the /beta endpoint; the
            # pydantic models are the real guard.
            "parameters": params,
        },
    }


# Built once at import (pure, deterministic): identical bytes on every request
# so DeepSeek's prefix cache hits.
TOOL_SPECS: list[dict[str, Any]] = [_spec(n, m) for n, (m, _) in TOOLS.items()]
TOOLS_JSON = json.dumps(TOOL_SPECS, ensure_ascii=False, sort_keys=True)


def validate_args(name: str, raw: str) -> _Args:
    """Parse and validate a tool call. Raises ToolRejected; never runs anything."""
    if name not in TOOLS:
        raise ToolRejected("tool_not_allowlisted")
    try:
        return TOOLS[name][0].model_validate_json(raw or "{}")
    except ValidationError as exc:
        invalid_json = exc.errors()[0]["type"] == "json_invalid"
        raise ToolRejected("invalid_json" if invalid_json else "invalid_args") from exc


def _state() -> Any:
    return importlib.import_module("assistant.services.state")


def _run(ctx: ToolContext, name: str, args: _Args) -> str:
    if name in OWNER_ONLY and ctx.rol != "owner":
        raise ToolRejected("owner_only")
    module, _, func = TOOLS[name][1].partition(":")
    fn: Callable[..., str] = getattr(importlib.import_module(module), func)
    return fn(ctx, **args.model_dump())


def _confirm_question(name: str, args: _Args) -> str | None:
    """The question to ask before running this call, or None to run it now."""
    if name == "cancelar_evento":
        return "¿Cancelo el evento?"
    if name == "deshacer":
        return "¿Deshago el último registro?"
    if isinstance(args, AnularMovimiento):
        return f"¿Anulo el movimiento {args.indice}?"
    if isinstance(args, RegistrarGasto):
        total = sum((i.monto for i in args.items), Decimal(0))
        if total > get_worker_settings().confirm_above:
            return f"¿Registro {total:.2f} {args.moneda}?"
    return None


def _conflict_question(ctx: ToolContext, args: _Args) -> str | None:
    """Ask before scheduling over an event or an external busy block."""
    if isinstance(args, CrearEvento):
        inicio, fin, tipo = args.inicio, args.fin, "evento"
    elif isinstance(args, Recordatorio):
        inicio, fin, tipo = args.cuando, None, "recordatorio"
    else:
        return None
    agenda = importlib.import_module("assistant.services.agenda")
    choques = agenda.conflictos(ctx, inicio, fin or inicio + agenda.DURACION[tipo])
    return f"Choca con {', '.join(choques)}. ¿Agendo igual?" if choques else None


def buttons(token: str) -> list[list[tuple[str, str]]]:
    return [[("Confirmar", f"ok:{token}"), ("Cancelar", f"no:{token}")]]


def handle_call(ctx: ToolContext, name: str, raw: str) -> tuple[str, str | None]:
    """Validate and run one tool call.

    Returns ``(result, None)`` after running it, or ``(question, token)`` when
    the call needs a confirmation turn and was stored as pending instead.
    Raises ToolRejected for anything invalid or not allowed.
    """
    args = validate_args(name, raw)
    if name in OWNER_ONLY and ctx.rol != "owner":
        raise ToolRejected("owner_only")
    question = _confirm_question(name, args) or _conflict_question(ctx, args)
    if question is None:
        return _run(ctx, name, args), None
    token: str = _state().create_pending(
        ctx.chat_id, {"tool": name, "args": args.model_dump(mode="json")}
    )
    return question, token


def ask_tipo(
    ctx: ToolContext, monto: Decimal, moneda: str
) -> tuple[str, list[list[tuple[str, str]]]]:
    """A bare amount: store it and ask with Gasto / Ingreso buttons."""
    args = {
        "monto": str(monto),
        "moneda": moneda,
        "fecha": ctx.ahora.date().isoformat(),
    }
    token: str = _state().create_pending(
        ctx.chat_id, {"tool": "elegir_tipo", "args": args}
    )
    pregunta = f"¿{monto:.2f} {moneda}: gasto o ingreso?"
    return pregunta, [[("Gasto", f"g:{token}"), ("Ingreso", f"i:{token}")]]


def execute_tipo(ctx: ToolContext, token: str, tipo: str) -> str:
    """Register the amount stored by ``ask_tipo`` as the chosen type."""
    action = _state().pop_pending(ctx.chat_id, token)
    if not action or action.get("tool") != "elegir_tipo":
        return "La confirmación expiró."
    a = action.get("args", {})
    if tipo == "gasto":
        name = "registrar_gasto"
        raw = {
            "items": [{"monto": a.get("monto"), "categoria": "otros"}],
            "moneda": a.get("moneda"),
            "fecha": a.get("fecha"),
        }
    else:
        name = "registrar_ingreso"
        raw = {
            "monto": a.get("monto"),
            "moneda": a.get("moneda"),
            "fuente": "",
            "fecha": a.get("fecha"),
        }
    # Stored data crosses a trust boundary: validate like any tool call.
    return _run(ctx, name, validate_args(name, json.dumps(raw)))


def execute_pending(ctx: ToolContext, token: str) -> str:
    """Run the call stored for an ``ok:<token>`` button (single use).

    Runs it directly, without the checks of ``handle_call``: the user already
    confirmed, so conflicts are not checked again.
    """
    action = _state().pop_pending(ctx.chat_id, token)
    if not action:
        return "La confirmación expiró."
    # Stored data crosses a trust boundary too: validate again before running.
    name = str(action.get("tool", ""))
    return _run(ctx, name, validate_args(name, json.dumps(action.get("args", {}))))
