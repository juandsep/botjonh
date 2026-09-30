"""Deterministic quick entry: ``2 usd cafe``, ``1000usd ingreso``. No LLM.

``parse`` accepts a short message with exactly one amount in any word order and
returns an ``Entry``; anything it is not sure about (two amounts, questions,
dates, times, edits, reminders) returns None so the LLM handles it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

CODES = frozenset(
    {"USD", "COP", "EUR", "MXN", "PEN", "CLP", "ARS", "BRL", "GBP", "CAD", "PAB"}
)
_CUR = {c.lower(): c for c in CODES} | {
    "$": "USD",
    "€": "EUR",
    "dolar": "USD",
    "dolares": "USD",
    "euro": "EUR",
    "euros": "EUR",
}
_TOKEN = re.compile(r"([$€])?([-+]?\d[\d.,]*)([$€]|[a-z]{3})?")
_TIME = re.compile(r"\d{1,2}:\d{2}|\b\d{1,2}\s*(am|pm|a\.m|p\.m)\b|\ba las\b")
# Words that mean a date, a question, an edit or a calendar action: LLM.
_SKIP = frozenset(
    "hoy manana ayer anteayer pasado lunes martes miercoles jueves viernes "
    "sabado domingo semana mes cuanto cuanta que cual como cuando donde "
    "resumen presupuesto ultimo ultima era cambia cambiar corrige corregir "
    "edita editar anula anular borra borrar deshaz deshacer cancela cancelar "
    "recuerda recuerdame recordar recordatorio cita reunion evento agenda".split()
)
_STRIP = frozenset({"gasto", "gaste", "ingreso"})
_LEAD = frozenset({"en", "de", "por", "para"})
_KEYWORDS = {
    "restaurantes": "cafe almuerzo restaurante",
    "transporte": "uber taxi bus gasolina",
    "supermercado": "mercado super",
    "suscripciones": "netflix spotify",
    "vivienda": "arriendo",
    "servicios": "luz agua internet celular",
    "salud": "farmacia medico",
    "entretenimiento": "cine",
    "compras": "ropa",
}
_CATEGORIA = {w: cat for cat, words in _KEYWORDS.items() for w in words.split()}
MAX_WORDS = 8  # ponytail: longer messages are prose; let the LLM read them
NOT_POSITIVE = "El monto debe ser mayor que 0."


@dataclass(frozen=True)
class Entry:
    tipo: str  # gasto | ingreso
    monto: Decimal
    moneda: str
    nota: str
    categoria: str  # gastos only; "" for ingresos
    error: str | None = None


def norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def number(raw: str) -> Decimal | None:
    """``2``, ``2,5``, ``2.000`` (thousands), ``1.234,56``, ``1,234.56``."""
    sign = -1 if raw.startswith("-") else 1
    s = raw.lstrip("+-")
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", s):
        return None
    seps = [c for c in s if c in ".,"]
    if len(set(seps)) == 2:
        dec = seps[-1]
        whole, frac = s.rsplit(dec, 1)
        if dec in whole or not re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", whole):
            return None
        s = re.sub(r"[.,]", "", whole) + "." + frac
    elif len(seps) > 1:
        if not re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):
            return None
        s = re.sub(r"[.,]", "", s)
    elif seps:
        whole, frac = s.split(seps[0])
        s = whole + ("" if len(frac) == 3 else ".") + frac
    return sign * Decimal(s)


def _amount(tok: str) -> tuple[Decimal, str | None] | None:
    """An amount token with its attached currency, if any."""
    m = _TOKEN.fullmatch(tok)
    if not m:
        return None
    sym = m[1] or m[3]
    if sym and sym not in _CUR:
        return None  # 2abc: unknown code glued to the number
    value = number(m[2])
    return None if value is None else (value, _CUR[sym] if sym else None)


def amount(text: str) -> tuple[Decimal, str | None] | None:
    """``3usd``, ``2000 cop``, ``usd 2``, ``$3`` or ``3``: for /editar."""
    toks = norm(text).split()
    code = None
    if len(toks) == 2 and toks[1] in _CUR:
        code = toks.pop()
    elif len(toks) == 2 and toks[0] in _CUR:
        code = toks.pop(0)
    found = _amount(toks[0]) if len(toks) == 1 else None
    if found is None or (code and found[1]):
        return None
    return found[0], found[1] or (_CUR[code] if code else None)


def parse(text: str) -> Entry | None:
    raw = text.split()
    toks = [norm(t).strip(".,;:!") for t in raw]
    joined = " ".join(toks)
    if (
        not raw
        or len(raw) > MAX_WORDS
        or text.lstrip().startswith("/")
        or "?" in text
        or "¿" in text
        or _TIME.search(joined)
        or _SKIP.intersection(toks)
    ):
        return None
    amounts = [i for i, t in enumerate(toks) if _amount(t)]
    digits = [i for i, t in enumerate(toks) if any(c.isdigit() for c in t)]
    if len(amounts) != 1 or digits != amounts:
        return None
    i = amounts[0]
    value, moneda = _amount(toks[i]) or (Decimal(0), None)
    used = {i}
    if moneda is None:
        for j in (i + 1, i - 1):
            if 0 <= j < len(toks) and toks[j] in _CUR:
                moneda, used = _CUR[toks[j]], {i, j}
                break
    tipo = "ingreso" if "ingreso" in toks else "gasto"
    words = [
        (raw[k].strip(".,;:!"), toks[k])
        for k in range(len(raw))
        if k not in used and toks[k] not in _STRIP and toks[k]
    ]
    if words and words[0][1] in _LEAD:
        words = words[1:]
    nota = " ".join(w for w, _ in words)
    categoria = ""
    if tipo == "gasto":
        found = (_CATEGORIA.get(n) for _, n in words)
        categoria = next((c for c in found if c), "otros")
    return Entry(
        tipo=tipo,
        monto=value,
        moneda=moneda or "USD",
        nota=nota,
        categoria=categoria,
        error=None if value > 0 else NOT_POSITIVE,
    )
