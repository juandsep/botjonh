"""Web dashboard of one chat's month, rendered server-side as plain HTML.

Served by assistant-api at ``/tablero/{token}`` (see ``state.dash_token``). No
JS: bars are CSS widths/heights. Every value from the ledger goes through
``html.escape``. Amounts are the ledger's USD.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from html import escape

from assistant.services import ledger

META = Decimal("0.20")  # savings target, same 20% as the weekly job
ULTIMOS = 15
MESES = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre "
    "noviembre diciembre"
).split()

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#667085;--bar:#3b82f6;
--in:#16a34a;--out:#dc2626;--line:#e4e7ec}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#181c24;
--fg:#e6e8ec;--muted:#98a2b3;--bar:#60a5fa;--in:#4ade80;--out:#f87171;
--line:#2a303b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
h1{font-size:1.3rem;margin:0 0 4px}h2{font-size:1rem;margin:0 0 12px}
nav{display:flex;justify-content:space-between;margin-bottom:16px}
a{color:var(--bar)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.kpis div{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:12px}
.kpis small,.muted{color:var(--muted)}.kpis b{display:block;font-size:1.2rem}
.in{color:var(--in)}.out{color:var(--out)}
.row{display:grid;grid-template-columns:7.5rem 1fr 5.5rem;gap:8px;
align-items:center;margin:6px 0}
.row span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{background:var(--line);border-radius:4px;height:10px}
.fill{background:var(--bar);border-radius:4px;height:10px}
.num{text-align:right;font-variant-numeric:tabular-nums}
.days{display:flex;align-items:flex-end;gap:2px;height:120px}
.days div{flex:1;background:var(--bar);border-radius:2px 2px 0 0;min-height:1px}
.axis{display:flex;justify-content:space-between;color:var(--muted);
font-size:.75rem;margin-top:4px}
ul{list-style:none;margin:0;padding:0}
li{display:flex;justify-content:space-between;gap:8px;padding:8px 0;
border-top:1px solid var(--line)}li:first-child{border-top:0}
@media (min-width:560px){.kpis{grid-template-columns:repeat(4,1fr)}}
"""


def _usd(valor: Decimal) -> str:
    return f"{valor:,.2f}"


def _pct(parte: Decimal, total: Decimal) -> Decimal:
    """Bar length 0-100 for CSS; never negative."""
    return max(Decimal(0), parte) * 100 / total if total > 0 else Decimal(0)


def render(chat_id: str, mes: date) -> str:
    """The month of ``mes`` (any day in it) as a full HTML page."""
    desde = mes.replace(day=1)
    dias = calendar.monthrange(desde.year, desde.month)[1]
    hasta = desde.replace(day=dias)
    movs = ledger.vigentes(chat_id, desde, hasta)

    ingresos = gastos = Decimal("0.00")
    por_cat: dict[str, Decimal] = defaultdict(Decimal)
    por_dia: dict[int, Decimal] = defaultdict(Decimal)
    for d in movs:
        monto = ledger.q(d["monto"])
        if d["tipo_mov"] == "ingreso":
            ingresos += monto
        else:
            gastos += monto
            por_cat[d.get("categoria") or "otros"] += monto
            por_dia[date.fromisoformat(d["fecha"]).day] += monto
    ahorro = ingresos - gastos
    meta = ledger.q(ingresos * META)
    if ingresos > 0:
        tasa = f"{ledger.q(ahorro * 100 / ingresos)}%"
        cumple = "in" if ahorro >= meta else "out"
        meta_txt = f"Meta 20%: {_usd(meta)} USD"
    else:
        tasa, cumple, meta_txt = "—", "muted", "Sin ingresos este mes"

    cats = sorted(por_cat.items(), key=lambda kv: -kv[1])
    tope_cat = max((v for _, v in cats), default=Decimal(0))
    filas_cat = (
        "".join(
            f'<div class="row"><span>{escape(c)}</span><div class="track">'
            f'<div class="fill" style="width:{_pct(v, tope_cat):.1f}%"></div></div>'
            f'<span class="num">{_usd(v)}</span></div>'
            for c, v in cats
        )
        or '<p class="muted">Sin gastos.</p>'
    )

    tope_dia = max(por_dia.values(), default=Decimal(0))
    barras = "".join(
        f'<div style="height:{_pct(por_dia[n], tope_dia):.1f}%" '
        f'title="{n}: {_usd(por_dia[n])} USD"></div>'
        for n in range(1, dias + 1)
    )

    recientes = sorted(
        movs, key=lambda d: (d["fecha"], str(d.get("creado", ""))), reverse=True
    )[:ULTIMOS]
    filas_mov = (
        "".join(
            f"<li><span>{date.fromisoformat(d['fecha']):%d/%m} "
            f"{escape(d.get('nota') or d.get('categoria') or d.get('fuente') or '')}"
            f'</span><span class="num {"in" if d["tipo_mov"] == "ingreso" else "out"}">'
            f"{'+' if d['tipo_mov'] == 'ingreso' else '−'}"
            f"{_usd(abs(ledger.q(d['monto'])))}</span></li>"
            for d in recientes
        )
        or '<li class="muted">Sin movimientos.</li>'
    )

    titulo = f"{MESES[desde.month - 1]} {desde.year}"
    antes = (desde - timedelta(days=1)).strftime("%Y-%m")
    despues = (hasta + timedelta(days=1)).strftime("%Y-%m")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Tablero · {titulo}</title><style>{CSS}</style></head>
<body><main>
<h1>Tablero de {titulo}</h1><p class="muted">Montos en USD.</p>
<nav><a href="?mes={antes}">← Anterior</a><a href="?mes={despues}">Siguiente →</a></nav>
<div class="kpis">
<div><small>Ingresos</small><b class="in">{_usd(ingresos)}</b></div>
<div><small>Gastos</small><b class="out">{_usd(gastos)}</b></div>
<div><small>Ahorro</small><b>{_usd(ahorro)}</b></div>
<div><small>Tasa de ahorro</small><b class="{cumple}">{tasa}</b>
<small>{meta_txt}</small></div>
</div>
<section><h2>Gastos por categoría</h2>{filas_cat}</section>
<section><h2>Gasto diario</h2><div class="days">{barras}</div>
<div class="axis"><span>1</span><span>{dias}</span></div></section>
<section><h2>Últimos {ULTIMOS} movimientos</h2><ul>{filas_mov}</ul></section>
</main></body></html>
"""
