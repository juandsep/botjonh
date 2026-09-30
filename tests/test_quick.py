from decimal import Decimal

import pytest

from assistant.services.quick import NOT_POSITIVE, amount, parse


@pytest.mark.parametrize(
    ("text", "tipo", "monto", "moneda", "nota", "categoria"),
    [
        ("gasto 2 usd cafe", "gasto", "2", "USD", "cafe", "restaurantes"),
        ("2 usd cafe", "gasto", "2", "USD", "cafe", "restaurantes"),
        ("cafe 2000cop gasto", "gasto", "2000", "COP", "cafe", "restaurantes"),
        ("2000 cop cafe", "gasto", "2000", "COP", "cafe", "restaurantes"),
        ("1000usd ingreso", "ingreso", "1000", "USD", "", ""),
        ("ingreso 1000 salario", "ingreso", "1000", "USD", "salario", ""),
        ("cafe 5", "gasto", "5", "USD", "cafe", "restaurantes"),
        ("Café 2.5", "gasto", "2.5", "USD", "Café", "restaurantes"),
        ("almuerzo 2,5", "gasto", "2.5", "USD", "almuerzo", "restaurantes"),
        ("2,000 cop taxi", "gasto", "2000", "COP", "taxi", "transporte"),
        ("mercado 2.000 cop", "gasto", "2000", "COP", "mercado", "supermercado"),
        ("1.234,56 cop arriendo", "gasto", "1234.56", "COP", "arriendo", "vivienda"),
        ("1,234.56 mxn luz", "gasto", "1234.56", "MXN", "luz", "servicios"),
        ("1.000.000 cop", "gasto", "1000000", "COP", "", "otros"),
        ("$3.50 uber", "gasto", "3.50", "USD", "uber", "transporte"),
        ("5€ cine", "gasto", "5", "EUR", "cine", "entretenimiento"),
        ("usd 2 netflix", "gasto", "2", "USD", "netflix", "suscripciones"),
        ("2 xyz farmacia", "gasto", "2", "USD", "xyz farmacia", "salud"),
        ("ropa 20 PEN", "gasto", "20", "PEN", "ropa", "compras"),
        ("gasté 5 en pan", "gasto", "5", "USD", "pan", "otros"),
        ("INGRESO 2,000 Freelance", "ingreso", "2000", "USD", "Freelance", ""),
    ],
)
def test_parse_accepts(text, tipo, monto, moneda, nota, categoria) -> None:
    e = parse(text)
    assert e is not None and e.error is None
    assert (e.tipo, e.monto, e.moneda, e.nota, e.categoria) == (
        tipo,
        Decimal(monto),
        moneda,
        nota,
        categoria,
    )
    assert isinstance(e.monto, Decimal)


@pytest.mark.parametrize("text", ["0 cafe", "-5 cafe", "cafe 0,00"])
def test_not_positive_is_an_error_entry(text) -> None:
    e = parse(text)
    assert e is not None and e.error == NOT_POSITIVE


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hola",
        "cafe 5 y pan 3",
        "2 usd 3 cop",
        "mañana a las 4 dentista",
        "dentista 4pm 5",
        "16:00 cafe 5",
        "cafe 5 hoy",
        "lunes gym 5",
        "¿cuánto gasté?",
        "cuanto gaste en cafe 5",
        "el último era 3 dólares, no 5",
        "cambia el ultimo a 3",
        "recuérdame pagar 5",
        "/editar 1 3usd",
        "2abc cafe",
        "5/10 cafe",
        "1.2,3 cafe",
        "cafe 5 con juan en el centro de la ciudad",
    ],
)
def test_parse_leaves_everything_else_to_the_llm(text) -> None:
    assert parse(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3usd", (Decimal(3), "USD")),
        ("2000 cop", (Decimal(2000), "COP")),
        ("usd 2", (Decimal(2), "USD")),
        ("3", (Decimal(3), None)),
        ("x", None),
        ("3usd cop", None),
        ("", None),
    ],
)
def test_amount(text, expected) -> None:
    assert amount(text) == expected
