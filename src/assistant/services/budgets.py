"""Budget advice: pure rules over the ledger, no LLM.

A per-category budget from ``preferences/{chat_id}`` wins; without one, the
50/30/20 rule over the month's income caps ``necesidades`` and ``ocio``.
"""

from __future__ import annotations

import calendar as cal
import importlib
from collections.abc import Mapping
from decimal import Decimal

from assistant.context import BUCKET_OF, ToolContext
from assistant.services import ledger

# 50/30/20: ahorro (20%) is a floor, not a cap, so saving more is never an excess.
CAPS = {"necesidades": Decimal("0.50"), "ocio": Decimal("0.30")}


def mayor_exceso(
    gastos: Mapping[str, Decimal],
    presupuesto: Mapping[str, str] | None,
    ingresos: Decimal,
    factor: Decimal = Decimal(1),
) -> tuple[str, Decimal, Decimal] | None:
    """Key with the largest spend over its cap, as ``(key, spent, cap)``.

    Keys are categories when ``presupuesto`` is set, else 50/30/20 buckets.
    ``factor`` pro-rates monthly caps (e.g. 7/30 for a week). None if no excess.
    """
    if presupuesto:
        gasto = dict(gastos)
        caps = {k: Decimal(v) for k, v in presupuesto.items()}
    else:
        gasto = {}
        for cat, monto in gastos.items():
            bucket = BUCKET_OF.get(cat, "ocio")
            gasto[bucket] = gasto.get(bucket, Decimal(0)) + monto
        caps = {b: ingresos * pct for b, pct in CAPS.items()}
    peor = max(
        (
            (k, gasto.get(k, Decimal(0)), ledger.q(cap * factor))
            for k, cap in caps.items()
        ),
        key=lambda t: t[1] - t[2],
        default=None,
    )
    return peor if peor is not None and peor[1] > peor[2] else None


def linea_exceso(
    ctx: ToolContext, gastos: Mapping[str, Decimal], factor: Decimal
) -> str | None:
    """One line on the largest excess; None when there is nothing to compare to."""
    dia = ledger.hoy(ctx)
    prefs = importlib.import_module("assistant.services.state").get_preferences(
        ctx.chat_id
    )
    presupuesto = (prefs or {}).get("presupuesto")
    ingresos = ledger.total_ingresos(ctx.chat_id, dia.replace(day=1), dia)
    if not presupuesto and ingresos <= 0:
        return None
    exceso = mayor_exceso(gastos, presupuesto, ingresos, factor)
    if exceso is None:
        return "Dentro del presupuesto."
    key, gastado, cap = exceso
    regla = "" if presupuesto else " (50/30/20)"
    return (
        f"Exceso en {key}{regla}: {gastado} de {cap} USD "
        f"(+{gastado - cap}). Recorta ahí primero."
    )


def recomendar_presupuesto(ctx: ToolContext, periodo: str = "mes") -> str:
    dia = ledger.hoy(ctx)
    desde, hasta = ledger.rango(periodo, dia)
    gastos = ledger.gastos_por_categoria(ctx.chat_id, desde, hasta)
    dias_mes = cal.monthrange(dia.year, dia.month)[1]
    factor = (
        Decimal(1) if periodo == "mes" else Decimal((hasta - desde).days + 1) / dias_mes
    )
    linea = linea_exceso(ctx, gastos, factor)
    return linea or "Sin presupuesto ni ingresos del mes para comparar."
