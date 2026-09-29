from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from assistant import api
from assistant.services import pubsub, state

URL = "/tg/test-path"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}  # pragma: allowlist secret


def update(text="hola", update_id=1, chat_id=42):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


@pytest.fixture
def fake(monkeypatch):
    users = {"42": {"rol": "owner"}}
    seen: set[int] = set()

    def mark(update_id):
        new = update_id not in seen
        seen.add(update_id)
        return new

    def redeem(code, chat_id):
        if code != "good":
            return False
        users[chat_id] = {"rol": "beta"}
        return True

    m = MagicMock()
    monkeypatch.setattr(state, "get_user", users.get)
    monkeypatch.setattr(state, "mark_processed", mark)
    monkeypatch.setattr(state, "unmark_processed", lambda uid: seen.discard(uid))
    monkeypatch.setattr(state, "redeem_invite", MagicMock(side_effect=redeem))
    monkeypatch.setattr(pubsub, "publish", m.publish)
    return m


client = TestClient(api.app)


def test_missing_header_403(fake) -> None:
    assert client.post(URL, json=update()).status_code == 403
    fake.publish.assert_not_called()


def test_wrong_path_403(fake) -> None:
    assert client.post("/tg/wrong", json=update(), headers=HEADERS).status_code == 403


def test_known_chat_published(fake) -> None:
    body = update()
    assert client.post(URL, json=body, headers=HEADERS).status_code == 200
    fake.publish.assert_called_once_with("assistant-updates", body)


def test_callback_query_published(fake) -> None:
    body = {
        "update_id": 5,
        "callback_query": {"id": "q", "data": "ok:t", "message": {"chat": {"id": 42}}},
    }
    assert client.post(URL, json=body, headers=HEADERS).status_code == 200
    fake.publish.assert_called_once()


def test_unknown_chat_dropped(fake) -> None:
    body = update(chat_id=99)
    assert client.post(URL, json=body, headers=HEADERS).status_code == 200
    fake.publish.assert_not_called()
    state.redeem_invite.assert_not_called()


def test_repeated_update_not_republished(fake) -> None:
    for _ in range(2):
        assert client.post(URL, json=update(), headers=HEADERS).status_code == 200
    assert fake.publish.call_count == 1


def test_start_with_valid_invite(fake) -> None:
    body = update("/start good", chat_id=99)
    assert client.post(URL, json=body, headers=HEADERS).status_code == 200
    fake.publish.assert_called_once()


def test_start_with_invalid_invite_is_noop(fake) -> None:
    body = update("/start bad", chat_id=99)
    assert client.post(URL, json=body, headers=HEADERS).status_code == 200
    fake.publish.assert_not_called()


def test_garbage_and_unsupported_updates_acked(fake) -> None:
    assert client.post(URL, content=b"{", headers=HEADERS).status_code == 200
    body = {"update_id": 3, "edited_message": {}}
    assert client.post(URL, json=body, headers=HEADERS).status_code == 200
    fake.publish.assert_not_called()


def test_publish_failure_lets_telegram_retry(fake) -> None:
    fake.publish.side_effect = RuntimeError("down")
    assert client.post(URL, json=update(), headers=HEADERS).status_code == 500
    fake.publish.side_effect = None
    assert client.post(URL, json=update(), headers=HEADERS).status_code == 200
    assert fake.publish.call_count == 2
