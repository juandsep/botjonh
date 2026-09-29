import base64
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
from assistant.services import state

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
