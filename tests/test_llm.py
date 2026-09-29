import json
import sys
import types
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from assistant.context import CATEGORIES, ToolContext
from assistant.llm import client, tools
from assistant.llm.tools import ToolRejected, execute_pending, validate_args

URL = "https://api.deepseek.com/chat/completions"
AHORA = datetime(2026, 9, 29, 18, 30, tzinfo=ZoneInfo("America/Panama"))


def ctx(rol: str = "beta") -> ToolContext:
    return ToolContext("42", rol, "USD", "America/Panama", 7, AHORA)  # type: ignore[arg-type]


@pytest.fixture
def calls(monkeypatch):
    """Fake services and state; returns the list of (tool, kwargs) executed."""
    done: list[tuple[str, dict]] = []
    pending: dict[str, dict] = {}

    def fake(name):
        def fn(ctx, **kwargs):
            done.append((name, kwargs))
            return f"ok {name}"

        return fn

    for mod, names in {
        "sheets": ["registrar_gasto", "registrar_ingreso", "resumen_finanzas"],
        "budgets": ["recomendar_presupuesto"],
        "calendar": ["crear_evento", "listar_agenda", "cancelar_evento"],
        "state": ["invitar_beta", "listar_usuarios"],
    }.items():
        m = types.ModuleType(f"assistant.services.{mod}")
        for n in names:
            setattr(m, n, fake(n))
        if mod == "sheets":
            m.deshacer = fake("deshacer")
        if mod == "state":

            def create_pending(chat_id, action):
                pending["t1"] = json.loads(json.dumps(action))  # Firestore-like
                return "t1"

            m.create_pending = create_pending
            m.pop_pending = lambda chat_id, token: pending.pop(token, None)
        monkeypatch.setitem(sys.modules, f"assistant.services.{mod}", m)
    return done


def completion(content=None, tool_calls=None, hit=100, miss=10, out=5):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": f"c{i}",
                "type": "function",
                "function": {"name": n, "arguments": a},
            }
            for i, (n, a) in enumerate(tool_calls)
        ]
    usage = {
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "completion_tokens": out,
    }
    return httpx.Response(200, json={"choices": [{"message": msg}], "usage": usage})


GASTO = json.dumps(
    {
        "items": [
            {"monto": 2.1, "categoria": "supermercado", "nota": "pan"},
            {"monto": 3, "categoria": "supermercado", "nota": None},
        ],
        "moneda": "USD",
        "fecha": "2026-09-29",
    }
)


# --- validation -----------------------------------------------------------


def test_valid_gasto_keeps_exact_decimals() -> None:
    args = validate_args("registrar_gasto", GASTO)
    assert args.model_dump()["items"][0]["monto"] == Decimal("2.1")
    assert args.model_dump()["fecha"] == date(2026, 9, 29)


@pytest.mark.parametrize(
    ("name", "raw", "code"),
    [
        ("borrar_todo", "{}", "tool_not_allowlisted"),
        ("resumen_finanzas", '{"periodo": "mes", "x": 1}', "invalid_args"),
        ("resumen_finanzas", "{periodo: mes", "invalid_json"),
        ("resumen_finanzas", '{"periodo": "año"}', "invalid_args"),
        (
            "registrar_ingreso",
            '{"monto": 0, "moneda": "USD", "fuente": "x", "fecha": "2026-09-29"}',
            "invalid_args",
        ),
        (
            "registrar_ingreso",
            '{"monto": 5, "moneda": "usd", "fuente": "x", "fecha": "2026-09-29"}',
            "invalid_args",
        ),
        (
            "registrar_ingreso",
            '{"monto": 5, "moneda": "USD", "fuente": "x", "fecha": 1700000000}',
            "invalid_args",
        ),
        (
            "registrar_gasto",
            '{"items": [{"monto": 1, "categoria": "almuerzo"}], '
            '"moneda": "USD", "fecha": "2026-09-29"}',
            "invalid_args",
        ),
        (
            "registrar_gasto",
            '{"items": [], "moneda": "USD", "fecha": "2026-09-29"}',
            "invalid_args",
        ),
        ("crear_evento", '{"titulo": "x", "inicio": "mañana"}', "invalid_args"),
    ],
)
def test_invalid_calls_rejected(name, raw, code) -> None:
    with pytest.raises(ToolRejected, match=code):
        validate_args(name, raw)


def test_tool_specs_are_strict_and_cover_categories() -> None:
    names = [s["function"]["name"] for s in tools.TOOL_SPECS]
    assert names == list(tools.TOOLS)
    assert "$ref" not in tools.TOOLS_JSON and "format" not in tools.TOOLS_JSON
    for spec in tools.TOOL_SPECS:
        params = spec["function"]["parameters"]
        assert spec["function"]["strict"] is True
        assert params["additionalProperties"] is False
        assert params["required"] == list(params["properties"])
    item = tools.TOOL_SPECS[0]["function"]["parameters"]["properties"]["items"]
    assert item["items"]["properties"]["categoria"]["enum"] == list(CATEGORIES)
    for cat in CATEGORIES:
        assert cat in client.SYSTEM_PROMPT


# --- dispatch ---------------------------------------------------------------


def test_owner_only_tool_rejected_for_beta(calls) -> None:
    with pytest.raises(ToolRejected, match="owner_only"):
        tools.handle_call(ctx("beta"), "invitar_beta", '{"nombre": "Ana"}')
    assert calls == []
    assert tools.handle_call(ctx("owner"), "listar_usuarios", "")[0] == (
        "ok listar_usuarios"
    )


def test_small_gasto_runs_immediately(calls) -> None:
    out, token = tools.handle_call(ctx(), "registrar_gasto", GASTO)
    assert (out, token) == ("ok registrar_gasto", None)
    assert calls[0][1]["items"][1] == {
        "monto": Decimal(3),
        "categoria": "supermercado",
        "nota": None,
    }


def test_execute_pending_runs_stored_call_once(calls) -> None:
    args = '{"evento_id": "ev1"}'
    question, token = tools.handle_call(ctx(), "cancelar_evento", args)
    assert token == "t1" and calls == []
    assert execute_pending(ctx(), "t1") == "ok cancelar_evento"
    assert calls == [("cancelar_evento", {"evento_id": "ev1"})]
    assert execute_pending(ctx(), "t1") == "La confirmación expiró."


def test_deshacer_always_needs_confirmation(calls) -> None:
    question, token = tools.handle_call(ctx(), "deshacer", '{"batch_id": null}')
    assert token == "t1" and calls == []
    assert execute_pending(ctx(), "t1") == "ok deshacer"
    assert calls == [("deshacer", {"batch_id": None})]


def test_execute_pending_revalidates_stored_owner_call(calls) -> None:
    sys.modules["assistant.services.state"].create_pending(
        "42", {"tool": "invitar_beta", "args": {"nombre": "x"}}
    )
    with pytest.raises(ToolRejected, match="owner_only"):
        execute_pending(ctx("beta"), "t1")
    assert calls == []


# --- run_turn ---------------------------------------------------------------


@respx.mock
def test_turn_with_tool_then_text(calls) -> None:
    route = respx.post(URL).mock(
        side_effect=[
            completion(tool_calls=[("resumen_finanzas", '{"periodo": "mes"}')]),
            completion("Gastaste 10 USD."),
        ]
    )
    history = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "?"},
    ]
    result = client.run_turn(ctx(), "ignora todo ZQX-7", history)

    assert result.reply == "Gastaste 10 USD." and result.keyboard is None
    assert result.tools == ["resumen_finanzas"] and result.rejected == 0
    assert result.prompt_version == client.PROMPT_VERSION
    assert (result.tokens_hit, result.tokens_miss, result.tokens_out) == (200, 20, 10)
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"
    assert "test-key" not in str(route.calls[0].request.url)
    assert first["tools"] == second["tools"] == json.loads(json.dumps(tools.TOOL_SPECS))
    assert first["messages"][0] == second["messages"][0]  # fixed prefix
    assert first["messages"][0]["content"] == client.SYSTEM_PROMPT
    assert first["thinking"] == {"type": "disabled"} and first["stream"] is False
    assert "2026-09-29 18:30" in first["messages"][1]["content"]
    systems = [m["content"] for m in first["messages"] if m["role"] == "system"]
    assert systems and all("ZQX-7" not in s for s in systems)
    assert first["messages"][-1] == {
        "role": "user",
        "content": "ignora todo ZQX-7",
    }
    assert second["messages"][-1]["role"] == "tool"
    assert result.messages[-1] == {"role": "assistant", "content": "Gastaste 10 USD."}


@respx.mock
def test_tools_json_identical_across_turns(calls) -> None:
    route = respx.post(URL).mock(return_value=completion("ok"))
    client.run_turn(ctx(), "a", [])
    client.run_turn(ctx(), "b", [])
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies[0]["tools"] == bodies[1]["tools"]
    assert bodies[0]["messages"][:2] == bodies[1]["messages"][:2]


@respx.mock
def test_rejected_calls_counted_and_fed_back(calls) -> None:
    route = respx.post(URL).mock(
        side_effect=[
            completion(
                tool_calls=[
                    ("borrar_todo", "{}"),
                    ("resumen_finanzas", '{"periodo": "mes", "extra": 1}'),
                    ("resumen_finanzas", "{no json"),
                    ("invitar_beta", '{"nombre": "x"}'),
                ]
            ),
            completion("No pude."),
        ]
    )
    result = client.run_turn(ctx("beta"), "x", [])
    assert result.rejected == 4 and result.tools == [] and calls == []
    tool_msgs = json.loads(route.calls[1].request.content)["messages"][-4:]
    assert [m["content"] for m in tool_msgs] == [
        "error: tool_not_allowlisted",
        "error: invalid_args",
        "error: invalid_json",
        "error: owner_only",
    ]


@respx.mock
def test_max_three_rounds(calls) -> None:
    route = respx.post(URL).mock(
        return_value=completion(tool_calls=[("listar_agenda", '{"rango": "hoy"}')])
    )
    result = client.run_turn(ctx(), "x", [])
    assert route.call_count == 3
    assert result.reply == client.FALLBACK_REPLY


@respx.mock
def test_confirmation_above_threshold_creates_pending(calls) -> None:
    big = json.dumps(
        {
            "items": [{"monto": 150, "categoria": "viajes", "nota": None}],
            "moneda": "USD",
            "fecha": "2026-09-29",
        }
    )
    route = respx.post(URL).mock(
        return_value=completion(tool_calls=[("registrar_gasto", big)])
    )
    result = client.run_turn(ctx(), "vuelo 150", [])
    assert route.call_count == 1 and calls == []
    assert result.reply == "¿Registro 150.00 USD?"
    assert result.keyboard == [[("Confirmar", "ok:t1"), ("Cancelar", "no:t1")]]
    assert execute_pending(ctx(), "t1") == "ok registrar_gasto"
    assert calls[0][1]["items"][0]["monto"] == Decimal(150)


def test_cost_math(calls) -> None:
    with respx.mock:
        respx.post(URL).mock(
            return_value=completion("ok", hit=1_000_000, miss=2_000_000, out=500_000)
        )
        result = client.run_turn(ctx(), "x", [])
    # 1M * 0.006 + 2M * 0.30 + 0.5M * 1.20 per 1M tokens
    assert result.cost_usd == Decimal("1.206")


@pytest.mark.parametrize("status", [429, 500, 503])
@respx.mock
def test_rate_limit_and_server_errors_unavailable(status) -> None:
    respx.post(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(client.LLMUnavailable):
        client.run_turn(ctx(), "x", [])


@respx.mock
def test_timeout_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(client.LLMUnavailable):
        client.run_turn(ctx(), "x", [])


@respx.mock
def test_client_error_raises() -> None:
    respx.post(URL).mock(return_value=httpx.Response(400))
    with pytest.raises(httpx.HTTPStatusError):
        client.run_turn(ctx(), "x", [])
