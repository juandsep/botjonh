"""Shared types between the LLM layer and the services.

Every tool implementation has the signature ``fn(ctx: ToolContext, **args) ->
str``. Role checks (owner-only tools) use ``ctx.rol`` in code, never the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Rol = Literal["owner", "beta"]

# Fixed category enum mapped to the 50/30/20 rule.
BUCKETS: dict[str, tuple[str, ...]] = {
    "necesidades": (
        "vivienda",
        "servicios",
        "supermercado",
        "transporte",
        "salud",
        "deudas",
    ),
    "ocio": (
        "restaurantes",
        "entretenimiento",
        "compras",
        "viajes",
        "suscripciones",
        "otros",
    ),
    "ahorro": ("ahorro", "inversion"),
}
CATEGORIES: tuple[str, ...] = tuple(c for cats in BUCKETS.values() for c in cats)
BUCKET_OF: dict[str, str] = {c: b for b, cats in BUCKETS.items() for c in cats}


@dataclass(frozen=True)
class ToolContext:
    chat_id: str
    rol: Rol
    moneda: str
    zona_horaria: str
    update_id: int
    ahora: datetime  # timezone-aware, in the user's zone
