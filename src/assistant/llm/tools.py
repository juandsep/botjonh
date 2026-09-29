"""Tool schemas (allowlist) and dispatch.

The LLM emits JSON validated against these schemas; the code performs the write.
Only the tools registered in TOOLS are callable — anything else is rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Gemini function declarations (tool calling). Keep in sync with the system
# prompt and the dispatch table below.
TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "registrar_gasto",
        "description": "Registra un gasto en el ledger (append-only).",
        "parameters": {
            "type": "object",
            "properties": {
                "monto": {"type": "number"},
                "moneda": {
                    "type": "string",
                    "description": "Código ISO 4217, p.ej. USD.",
                },
                "categoria": {
                    "type": "string",
                    "description": "almuerzo, transporte, salud, ocio, vivienda, otro.",
                },
                "fecha": {
                    "type": "string",
                    "description": "Fecha ISO 8601 (YYYY-MM-DD).",
                },
                "nota": {"type": "string", "description": "Nota opcional."},
            },
            "required": ["monto", "moneda", "categoria", "fecha"],
        },
    },
    {
        "name": "registrar_ingreso",
        "description": "Registra un ingreso en el ledger.",
        "parameters": {
            "type": "object",
            "properties": {
                "monto": {"type": "number"},
                "moneda": {"type": "string"},
                "fuente": {"type": "string"},
                "fecha": {"type": "string"},
                "nota": {"type": "string"},
            },
            "required": ["monto", "moneda", "fuente", "fecha"],
        },
    },
    {
        "name": "resumen_finanzas",
        "description": "Resumen agregado del ledger en un periodo.",
        "parameters": {
            "type": "object",
            "properties": {
                "periodo": {"type": "string", "enum": ["hoy", "mes", "semana"]},
            },
            "required": ["periodo"],
        },
    },
    {
        "name": "recomendar_presupuesto",
        "description": (
            "Compara gasto por categoría contra el presupuesto y sugiere dónde ahorrar."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "periodo": {"type": "string", "enum": ["mes"]},
            },
            "required": ["periodo"],
        },
    },
    {
        "name": "crear_evento",
        "description": "Crea un evento en el calendario dedicado.",
        "parameters": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string"},
                "inicio": {"type": "string", "description": "ISO 8601 con hora."},
                "fin": {"type": "string"},
                "ubicacion": {"type": "string"},
                "recordatorio_min": {"type": "integer"},
            },
            "required": ["titulo", "inicio"],
        },
    },
    {
        "name": "listar_agenda",
        "description": "Lista eventos del calendario en un rango.",
        "parameters": {
            "type": "object",
            "properties": {
                "rango": {"type": "string", "enum": ["hoy", "manana", "semana"]},
            },
            "required": ["rango"],
        },
    },
    {
        "name": "cancelar_evento",
        "description": "Borra un evento (requiere confirmación previa).",
        "parameters": {
            "type": "object",
            "properties": {
                "evento_id": {"type": "string"},
                "confirmado": {"type": "boolean"},
            },
            "required": ["evento_id", "confirmado"],
        },
    },
    {
        "name": "recordatorio",
        "description": "Programa un mensaje de recordatorio.",
        "parameters": {
            "type": "object",
            "properties": {
                "texto": {"type": "string"},
                "cuando": {"type": "string", "description": "ISO 8601 con hora."},
            },
            "required": ["texto", "cuando"],
        },
    },
    {
        "name": "agregar_beta",
        "description": "Añade un beta tester a la lista blanca. Solo owner.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "nombre": {"type": "string"},
            },
            "required": ["chat_id", "nombre"],
        },
    },
    {
        "name": "listar_usuarios",
        "description": "Lista la lista blanca de usuarios. Solo owner.",
        "parameters": {"type": "object", "properties": {}},
    },
]

# Dispatch table: tool name -> callable. Implementations live in services/.
# A name not present here is rejected before any side effect.
TOOLS: dict[str, Callable[..., Any]] = {
    # "registrar_gasto": services.sheets.registrar_gasto,
    # "registrar_ingreso": services.sheets.registrar_ingreso,
    # "resumen_finanzas": services.sheets.resumen_finanzas,
    # "recomendar_presupuesto": services.budgets.recomendar_presupuesto,
    # "crear_evento": services.calendar.crear_evento,
    # "listar_agenda": services.calendar.listar_agenda,
    # "cancelar_evento": services.calendar.cancelar_evento,
    # "recordatorio": services.state.recordatorio,
    # "agregar_beta": services.state.agregar_beta,
    # "listar_usuarios": services.state.listar_usuarios,
}


def is_allowlisted(name: str) -> bool:
    return name in TOOLS


def validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against the declaration. Returns cleaned args.

    Raises ValueError on invalid input. Placeholder: fill in per-tool coercion.
    """
    if not is_allowlisted(name):
        raise ValueError(f"tool not allowlisted: {name}")
    return args
