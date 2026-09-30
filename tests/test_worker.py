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


def message(text="gasté 5 en pan") -> dict:
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
    for name in (
        "get_user",
        "check_rate",
        "llm_spend_today",
        "get_history",
        "add_llm_spend",
        "append_history",
        "pop_pending",
    ):
        monkeypatch.setattr(state, name, getattr(m, name))
    return m


@pytest.fixture
def llm(monkeypatch):
    result = types.SimpleNamespace(
        reply="Anotado.",
        keyboard=[[("Sí", "ok:t")]],
        messages=[{"role": "user", "content": "x"}],
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
        "gasté 5 en pan",
    )
    assert ctx.ahora.tzinfo is not None
    body = json.loads(tg.calls.last.request.read())
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ok:t"
    st.add_llm_spend.assert_called_once_with("42", Decimal("0.001"))
    st.append_history.assert_called_once_with("42", llm.result.messages)
    record_args = llm.record.call_args.args
    assert record_args[0] is llm.result and record_args[2] == "gasté 5 en pan"


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
        "Uso: /conectar <url del calendario .ics>",
        "✓ calendario conectado",
    ]
    assert conectar.call_args.args[1] == "https://x/a.ics"
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
