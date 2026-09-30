import base64
import dataclasses
import importlib
import json
import sys
import types
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from assistant import worker
from assistant.channels.telegram import API_BASE
from assistant.config import get_worker_settings
from assistant.services import agenda, state

TG = f"{API_BASE}/bot123:test"
client = TestClient(worker.app)


def envelope(payload) -> dict:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "s"}


def message(text="¿cuánto gasté hoy?") -> dict:
    return {"update_id": 9, "message": {"chat": {"id": 42}, "text": text}}


def callback(data: str) -> dict:
    cq = {"id": "q1", "data": data, "message": {"chat": {"id": 42}}}
    return {"update_id": 10, "callback_query": cq}


def fake_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, mod)
    parent, _, child = name.rpartition(".")
    monkeypatch.setattr(importlib.import_module(parent), child, mod, raising=False)
    return mod


class LLMUnavailable(Exception):
    pass


@pytest.fixture
def st(monkeypatch):
    m = MagicMock()
    m.get_user.return_value = {
        "rol": "owner",
        "moneda": "USD",
        "zona_horaria": "America/Panama",
    }
    m.check_rate.return_value = True
    m.llm_spend_today.return_value = Decimal("0")
    m.get_history.return_value = []
    m.random_gif.return_value = None
    m.gifs.return_value = {"gasto": ["a", "b"], "ingreso": []}
    m.create_pending.return_value = "t" * 22
    for name in (
        "get_user",
        "check_rate",
        "llm_spend_today",
        "get_history",
        "add_llm_spend",
        "append_history",
        "pop_pending",
        "random_gif",
        "add_gif",
        "gifs",
        "create_pending",
    ):
        monkeypatch.setattr(state, name, getattr(m, name))
    return m


@pytest.fixture
def llm(monkeypatch):
    result = types.SimpleNamespace(
        reply="Anotado.",
        keyboard=[[("Sí", "ok:t")]],
        messages=[{"role": "user", "content": "x"}],
        tools=[],
        cost_usd=Decimal("0.001"),
    )
    run_turn = MagicMock(return_value=result)
    fake_module(
        monkeypatch,
        "assistant.llm.client",
        run_turn=run_turn,
        LLMUnavailable=LLMUnavailable,
    )
    record = MagicMock()
    fake_module(monkeypatch, "assistant.observability.trace", record_turn=record)
    return types.SimpleNamespace(run_turn=run_turn, record=record, result=result)


@pytest.fixture
def tg():
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{TG}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        router.post(f"{TG}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        router.post(f"{TG}/sendAnimation").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        router.post(f"{TG}/deleteMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        yield router


def sent_texts(tg) -> list[str]:
    return [
        json.loads(c.request.read())["text"]
        for c in tg.calls
        if c.request.url.path.endswith("sendMessage")
    ]


def test_malformed_envelope_acked() -> None:
    assert client.post("/push", content=b"nope").status_code == 204
    assert client.post("/push", json={"message": {}}).status_code == 204
    bad = {"message": {"data": "!!!"}}
    assert client.post("/push", json=bad).status_code == 204
    assert client.post("/push", json=envelope({"x": 1})).status_code == 204


def test_job_routed(monkeypatch) -> None:
    run_job = MagicMock()
    monkeypatch.setitem(
        sys.modules, "assistant.jobs", types.SimpleNamespace(run_job=run_job)
    )
    assert client.post("/push", json=envelope({"job": "digest"})).status_code == 204
    run_job.assert_called_once_with("digest")


def test_turn_in_order(st, llm, tg) -> None:
    assert client.post("/push", json=envelope(message())).status_code == 204
    ctx, text, history = llm.run_turn.call_args.args
    assert (ctx.chat_id, ctx.rol, ctx.update_id, text) == (
        "42",
        "owner",
        9,
        "¿cuánto gasté hoy?",
    )
    assert ctx.ahora.tzinfo is not None
    body = json.loads(tg.calls.last.request.read())
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ok:t"
    st.add_llm_spend.assert_called_once_with("42", Decimal("0.001"))
    st.append_history.assert_called_once_with("42", llm.result.messages)
    record_args = llm.record.call_args.args
    assert record_args[0] is llm.result and record_args[2] == "¿cuánto gasté hoy?"


def test_rate_limit_skips_llm(st, llm, tg) -> None:
    st.check_rate.return_value = False
    assert client.post("/push", json=envelope(message())).status_code == 204
    llm.run_turn.assert_not_called()
    assert sent_texts(tg) == [worker.LIMIT_REPLY]


def test_daily_cap_skips_llm(st, llm, tg) -> None:
    st.llm_spend_today.return_value = Decimal("0.10")
    assert client.post("/push", json=envelope(message())).status_code == 204
    llm.run_turn.assert_not_called()
    assert sent_texts(tg) == [worker.LIMIT_REPLY]


def test_llm_unavailable_503(st, llm, tg) -> None:
    llm.run_turn.side_effect = LLMUnavailable()
    assert client.post("/push", json=envelope(message())).status_code == 503
    st.add_llm_spend.assert_not_called()
    assert not tg.calls


def test_telegram_failure_still_accounts(st, llm, tg, caplog) -> None:
    tg.post(f"{TG}/sendMessage").mock(return_value=httpx.Response(403))
    assert client.post("/push", json=envelope(message())).status_code == 204
    st.add_llm_spend.assert_called_once()
    assert "send_failed" in caplog.text


def test_start_and_non_text_skip_llm(st, llm, tg) -> None:
    client.post("/push", json=envelope(message("/start abc")))
    client.post("/push", json=envelope(message("")))
    llm.run_turn.assert_not_called()
    assert sent_texts(tg) == [worker.WELCOME, worker.TEXT_ONLY]


def test_unknown_user_ignored(st, llm, tg) -> None:
    st.get_user.return_value = None
    assert client.post("/push", json=envelope(message())).status_code == 204
    assert not tg.calls


def test_callback_ok(monkeypatch, st, tg) -> None:
    execute = MagicMock(return_value="Hecho.")
    fake_module(monkeypatch, "assistant.llm.tools", execute_pending=execute)
    assert client.post("/push", json=envelope(callback("ok:tok"))).status_code == 204
    assert execute.call_args.args[1] == "tok"
    assert tg.routes[1].called
    assert sent_texts(tg) == ["Hecho."]


def test_callback_no(st, tg, caplog) -> None:
    tg.post(f"{TG}/answerCallbackQuery").mock(return_value=httpx.Response(400))
    assert client.post("/push", json=envelope(callback("no:tok"))).status_code == 204
    st.pop_pending.assert_called_once_with("42", "tok")
    assert sent_texts(tg) == ["Cancelado."]
    assert "answer_callback_failed" in caplog.text


def test_callback_unknown_action(st, tg) -> None:
    assert client.post("/push", json=envelope(callback("zz"))).status_code == 204
    assert sent_texts(tg) == []


def test_unexpected_turn_error_is_acknowledged(st, llm, tg) -> None:
    # A retry would pay for the turn again and could repeat a write.
    llm.run_turn.side_effect = RuntimeError("sheets down")
    assert client.post("/push", json=envelope(message())).status_code == 204
    assert sent_texts(tg) == [worker.FAILED_REPLY]
    st.add_llm_spend.assert_not_called()


# --- commands without the LLM ----------------------------------------------------


@pytest.fixture
def settings(monkeypatch):
    s = dataclasses.replace(get_worker_settings(), api_url="https://api.example")
    monkeypatch.setattr(worker, "get_worker_settings", lambda: s)
    return s


def test_calendario_lists_week_without_llm(monkeypatch, st, llm, tg) -> None:
    semana = MagicMock(return_value="Jue 1 · 09:00 Dentista")
    monkeypatch.setattr(agenda, "semana", semana)
    assert (
        client.post("/push", json=envelope(message("/calendario"))).status_code == 204
    )
    semana.return_value = "Sin nada en 7 días."
    client.post("/push", json=envelope(message("/calendario@botjonh_bot")))
    assert sent_texts(tg) == ["Jue 1 · 09:00 Dentista", "Sin nada en 7 días."]
    assert semana.call_args.args[0].chat_id == "42"
    llm.run_turn.assert_not_called()
    st.check_rate.assert_not_called()


def test_calendario_enlace_and_rotation(monkeypatch, st, llm, tg, settings) -> None:
    tokens = iter(["a" * 32, "b" * 32])
    current: list[str] = []

    def ics_token(chat_id, rotate=False):
        if rotate or not current:
            current[:] = [next(tokens)]
        return current[0]

    monkeypatch.setattr(state, "ics_token", ics_token)
    for text in ("/calendario enlace", "/calendario enlace", "/calendario nuevo"):
        client.post("/push", json=envelope(message(text)))
    links = [t.splitlines()[0] for t in sent_texts(tg)]
    assert links == [
        f"https://api.example/ics/{'a' * 32}.ics",
        f"https://api.example/ics/{'a' * 32}.ics",
        f"https://api.example/ics/{'b' * 32}.ics",
    ]
    assert sent_texts(tg)[0].splitlines()[1] == worker.GOOGLE_HINT
    llm.run_turn.assert_not_called()


def test_calendario_enlace_unconfigured_and_errors(monkeypatch, st, llm, tg) -> None:
    unset = dataclasses.replace(get_worker_settings(), api_url="")
    monkeypatch.setattr(worker, "get_worker_settings", lambda: unset)
    client.post("/push", json=envelope(message("/calendario enlace")))
    monkeypatch.setattr(agenda, "semana", MagicMock(side_effect=RuntimeError("x")))
    client.post("/push", json=envelope(message("/calendario")))
    assert sent_texts(tg) == ["Enlace no configurado.", worker.FAILED_REPLY]


def test_conectar(monkeypatch, st, llm, tg) -> None:
    monkeypatch.setitem(sys.modules, "assistant.services.busy", None)  # not shipped
    client.post("/push", json=envelope(message("/conectar https://x/a.ics")))
    client.post("/push", json=envelope(message("/conectar")))
    conectar = MagicMock(return_value="✓ calendario conectado")
    monkeypatch.setitem(
        sys.modules,
        "assistant.services.busy",
        types.SimpleNamespace(conectar=conectar),
    )
    client.post("/push", json=envelope(message("/conectar https://x/a.ics")))
    assert sent_texts(tg) == [
        "Aún no disponible.",
        worker.CONECTAR_HINT,
        "✓ calendario conectado",
    ]
    assert conectar.call_args.args[1] == "https://x/a.ics"
    llm.run_turn.assert_not_called()


def test_conectar_deletes_the_message_with_the_url(monkeypatch, st, llm, tg) -> None:
    monkeypatch.setitem(
        sys.modules,
        "assistant.services.busy",
        types.SimpleNamespace(
            conectar=MagicMock(return_value="✓ Calendario conectado.")
        ),
    )
    update = message("/conectar https://x/a.ics")
    update["message"]["message_id"] = 77
    client.post("/push", json=envelope(update))
    deleted = [c for c in tg.calls if c.request.url.path.endswith("deleteMessage")]
    assert json.loads(deleted[0].request.read()) == {"chat_id": "42", "message_id": 77}
    assert sent_texts(tg) == [
        "✓ Calendario conectado.\nBorré tu mensaje con el enlace."
    ]


# --- quick entry, ledger commands and GIFs (no LLM) ------------------------------


@pytest.fixture
def ledger(monkeypatch):
    return fake_module(
        monkeypatch,
        "assistant.services.ledger",
        registrar_gasto=MagicMock(return_value="−2.00 USD · cafe"),
        registrar_ingreso=MagicMock(return_value="+1000.00 USD · salario"),
        ultimos_texto=MagicMock(return_value="1. cafe 2.00 USD"),
        editar=MagicMock(return_value="✓ editado"),
        anular=MagicMock(return_value="✓ anulado"),
    )


def animations(tg) -> list[dict]:
    return [
        json.loads(c.request.read())
        for c in tg.calls
        if c.request.url.path.endswith("sendAnimation")
    ]


def test_quick_gasto_skips_llm_and_sends_gif(st, llm, tg, ledger) -> None:
    st.random_gif.return_value = "gif1"
    assert (
        client.post("/push", json=envelope(message("2000 cop cafe"))).status_code == 204
    )
    ctx = ledger.registrar_gasto.call_args.args[0]
    assert ledger.registrar_gasto.call_args.kwargs == {
        "items": [
            {"monto": Decimal(2000), "categoria": "restaurantes", "nota": "cafe"}
        ],
        "moneda": "COP",
        "fecha": ctx.ahora.date(),
    }
    assert sent_texts(tg) == []  # the GIF is the whole answer
    assert animations(tg) == [{"chat_id": "42", "animation": "gif1"}]
    st.random_gif.assert_called_once_with("42", "gasto")
    llm.run_turn.assert_not_called()
    st.check_rate.assert_not_called()
    st.add_llm_spend.assert_not_called()
    st.append_history.assert_not_called()


def test_quick_ingreso_without_gif_stored(st, llm, tg, ledger) -> None:
    client.post("/push", json=envelope(message("ingreso 1000 salario")))
    kwargs = ledger.registrar_ingreso.call_args.kwargs
    assert (kwargs["monto"], kwargs["moneda"], kwargs["fuente"]) == (
        Decimal(1000),
        "USD",
        "salario",
    )
    assert sent_texts(tg) == ["+1000.00 USD · salario"] and animations(tg) == []
    llm.run_turn.assert_not_called()


def test_quick_errors_never_5xx(st, llm, tg, ledger, caplog) -> None:
    client.post("/push", json=envelope(message("0 cafe")))
    ledger.registrar_gasto.assert_not_called()
    ledger.registrar_gasto.side_effect = RuntimeError("down")
    assert client.post("/push", json=envelope(message("cafe 5"))).status_code == 204
    ledger.registrar_gasto.side_effect = None
    st.random_gif.return_value = "gif1"
    tg.post(f"{TG}/sendAnimation").mock(return_value=httpx.Response(400))
    assert client.post("/push", json=envelope(message("cafe 7"))).status_code == 204
    assert sent_texts(tg) == [
        "El monto debe ser mayor que 0.",
        worker.FAILED_REPLY,
        "−2.00 USD · cafe",  # GIF failed: the text is the fallback
    ]
    assert "gif_failed" in caplog.text and "gif1" not in caplog.text
    assert "cafe" not in caplog.text
    llm.run_turn.assert_not_called()


def test_llm_registration_sends_gif(st, llm, tg) -> None:
    st.random_gif.return_value = "gif1"
    llm.result.keyboard = None
    llm.result.tools = ["registrar_ingreso"]
    client.post("/push", json=envelope(message("me pagaron el freelance")))
    st.random_gif.assert_called_once_with("42", "ingreso")
    assert animations(tg) == [{"chat_id": "42", "animation": "gif1"}]
    llm.result.keyboard = [[("Sí", "ok:t")]]  # pending confirmation: no GIF yet
    llm.result.tools = ["registrar_gasto"]
    client.post("/push", json=envelope(message("vuelo de 900")))
    assert len(animations(tg)) == 1


def test_ultimos_and_editar(st, llm, tg, ledger) -> None:
    client.post("/push", json=envelope(message("/ultimos")))
    client.post("/push", json=envelope(message("/editar 1 3usd")))
    client.post("/push", json=envelope(message("/editar 2 2000 cop")))
    for bad in ("/editar", "/editar x 3", "/editar 1 0", "/editar 1 tres"):
        client.post("/push", json=envelope(message(bad)))
    assert sent_texts(tg) == [
        "1. cafe 2.00 USD",
        "✓ editado",
        "✓ editado",
        *[worker.EDIT_USAGE] * 4,
    ]
    ledger.ultimos_texto.assert_called_once()
    assert ledger.ultimos_texto.call_args.kwargs == {"n": 5}
    assert [c.kwargs for c in ledger.editar.call_args_list] == [
        {
            "indice": 1,
            "monto": Decimal(3),
            "moneda": "USD",
            "categoria": None,
            "nota": None,
        },
        {
            "indice": 2,
            "monto": Decimal(2000),
            "moneda": "COP",
            "categoria": None,
            "nota": None,
        },
    ]
    llm.run_turn.assert_not_called()


def test_anular_asks_then_runs_on_ok(monkeypatch, st, llm, tg, ledger) -> None:
    token = "t" * 22
    client.post("/push", json=envelope(message("/anular 1")))
    client.post("/push", json=envelope(message("/anular")))
    ledger.anular.assert_not_called()
    body = json.loads(tg.calls[0].request.read())
    assert body["text"] == "¿Anulo el movimiento 1?"
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        f"ok:{token}"
    )
    assert st.create_pending.call_args.args[1] == {
        "tool": "anular_movimiento",
        "args": {"indice": 1},
    }
    st.pop_pending.return_value = st.create_pending.call_args.args[1]
    client.post("/push", json=envelope(callback(f"ok:{token}")))
    ledger.anular.assert_called_once()
    assert ledger.anular.call_args.kwargs == {"indice": 1}
    assert sent_texts(tg)[1:] == [worker.ANULAR_USAGE, "✓ anulado"]


def test_gif_saved_from_caption_and_reply(st, llm, tg) -> None:
    gif = {"update_id": 11, "message": {"chat": {"id": 42}, "caption": "Gasto"}}
    gif["message"]["animation"] = {"file_id": "g1"}
    client.post("/push", json=envelope(gif))
    reply = message("/gif ingreso")
    reply["message"]["reply_to_message"] = {"animation": {"file_id": "g2"}}
    client.post("/push", json=envelope(reply))
    client.post("/push", json=envelope(message("/gif")))
    no_caption = {"update_id": 12, "message": {"chat": {"id": 42}}}
    no_caption["message"]["animation"] = {"file_id": "g3"}
    client.post("/push", json=envelope(no_caption))
    assert [c.args for c in st.add_gif.call_args_list] == [
        ("42", "gasto", "g1"),
        ("42", "ingreso", "g2"),
    ]
    usage = worker.GIF_USAGE.format(gasto=2, ingreso=0)
    assert sent_texts(tg) == [
        "✓ GIF guardado para gasto.",
        "✓ GIF guardado para ingreso.",
        usage,
        usage,
    ]
    llm.run_turn.assert_not_called()


# --- reminders from Cloud Tasks --------------------------------------------------


def test_reminder_sends_for_active(monkeypatch, tg) -> None:
    aviso = MagicMock(return_value="⏰ Dentista 09:00")
    monkeypatch.setattr(agenda, "aviso", aviso)
    body = {"chat_id": "42", "evento_id": "100"}
    assert client.post("/tasks/reminder", json=body).status_code == 204
    aviso.assert_called_once_with("42", "100")
    assert sent_texts(tg) == ["⏰ Dentista 09:00"]
    assert json.loads(tg.calls.last.request.read())["chat_id"] == "42"


def test_reminder_cancelled_or_malformed_is_204(monkeypatch, tg) -> None:
    monkeypatch.setattr(agenda, "aviso", MagicMock(return_value=None))
    body = {"chat_id": "42", "evento_id": "100"}
    assert client.post("/tasks/reminder", json=body).status_code == 204
    assert client.post("/tasks/reminder", content=b"{").status_code == 204
    assert client.post("/tasks/reminder", json={"x": 1}).status_code == 204
    assert not tg.calls


def test_reminder_telegram_error_is_not_5xx(monkeypatch, tg, caplog) -> None:
    monkeypatch.setattr(agenda, "aviso", MagicMock(return_value="⏰ X 09:00"))
    tg.post(f"{TG}/sendMessage").mock(return_value=httpx.Response(500))
    body = {"chat_id": "42", "evento_id": "100"}
    assert client.post("/tasks/reminder", json=body).status_code == 204
    assert "reminder_send_failed" in caplog.text and "X 09:00" not in caplog.text


def test_bare_ical_url_connects_and_is_deleted(monkeypatch, st, llm, tg) -> None:
    conectar = MagicMock(return_value="✓ Conectado.")
    busy = types.SimpleNamespace(conectar=conectar, validar=lambda raw: raw)
    monkeypatch.setitem(sys.modules, "assistant.services.busy", busy)
    url = "https://calendar.google.com/calendar/ical/x/private-y/basic.ics"
    update = message(url)
    update["message"]["message_id"] = 5
    client.post("/push", json=envelope(update))
    assert conectar.call_args.args[1] == url
    assert any(c.request.url.path.endswith("deleteMessage") for c in tg.calls)
    llm.run_turn.assert_not_called()


def test_other_links_are_not_treated_as_calendars(monkeypatch, st, llm, tg) -> None:
    def validar(raw: str) -> str:
        raise ValueError("invalid_url")

    busy = types.SimpleNamespace(conectar=MagicMock(), validar=validar)
    monkeypatch.setitem(sys.modules, "assistant.services.busy", busy)
    client.post("/push", json=envelope(message("https://example.com/x")))
    busy.conectar.assert_not_called()


def test_bare_amount_asks_and_registers_the_chosen_type(
    monkeypatch, st, llm, tg, ledger
) -> None:
    pending: dict[str, dict] = {}

    def create(chat_id, action):
        pending["tok" + str(len(pending))] = action
        return "tok" + str(len(pending) - 1)

    st.create_pending.side_effect = create
    st.pop_pending.side_effect = lambda chat_id, t: pending.pop(t, None)
    st.random_gif.return_value = None
    client.post("/push", json=envelope(message("5")))
    asked = [
        json.loads(c.request.read())
        for c in tg.calls
        if c.request.url.path.endswith("sendMessage")
    ][-1]
    assert asked["text"] == "¿5.00 USD: gasto o ingreso?"
    buttons = asked["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["g:tok0", "i:tok0"]
    ledger.registrar_ingreso.assert_not_called()
    client.post("/push", json=envelope(callback("i:tok0")))
    kwargs = ledger.registrar_ingreso.call_args.kwargs
    assert (kwargs["monto"], kwargs["moneda"], kwargs["fuente"]) == (
        Decimal("5.00"),
        "USD",
        "",
    )
    assert sent_texts(tg)[-1] == "+1000.00 USD · salario"  # no GIF: text fallback
    client.post("/push", json=envelope(callback("g:tok0")))  # single use
    assert sent_texts(tg)[-1] == "La confirmación expiró."
    ledger.registrar_gasto.assert_not_called()
    llm.run_turn.assert_not_called()
