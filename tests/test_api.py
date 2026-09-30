from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from assistant import api
from assistant.services import agenda, pubsub, state

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
    resp = client.post(URL, json=body, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {
        "method": "sendChatAction",
        "chat_id": "42",
        "action": "typing",
    }
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


# --- ICS feed --------------------------------------------------------------------


@pytest.fixture
def ics_db(monkeypatch):
    """state._db with one feed token; agenda items for chat 42."""
    db = MagicMock()
    tokens = {"t" * 32: {"chat_id": "42"}}

    def document(token):
        data = tokens.get(token)
        snap = MagicMock(exists=data is not None)
        snap.to_dict.return_value = data
        return MagicMock(get=MagicMock(return_value=snap))

    db.collection.return_value.document.side_effect = document
    monkeypatch.setattr(state, "_db", lambda: db)
    items = MagicMock(
        return_value=[
            {
                "id": "100",
                "titulo": "Cena, vino; y más",
                "ubicacion": "",
                "inicio_utc": datetime(2026, 10, 1, 1, tzinfo=UTC),
                "fin_utc": datetime(2026, 10, 1, 2, tzinfo=UTC),
                "recordatorio_min": 15,
            }
        ]
    )
    monkeypatch.setattr(agenda, "_items", items)
    return db


@pytest.mark.parametrize(
    "path", ["/ics/short.ics", f"/ics/{'a' * 31}%2F.ics", f"/ics/{'a' * 33}.ics"]
)
def test_ics_bad_token_404_without_lookup(ics_db, path) -> None:
    assert client.get(path).status_code == 404
    ics_db.collection.assert_not_called()


def test_ics_unknown_token_404(ics_db) -> None:
    assert client.get(f"/ics/{'u' * 32}.ics").status_code == 404
    ics_db.collection.assert_called_with("ics_tokens")


def test_ics_feed(ics_db, caplog) -> None:
    caplog.set_level("INFO")
    resp = client.get(f"/ics/{'t' * 32}.ics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/calendar; charset=utf-8"
    assert resp.headers["cache-control"] == "private, max-age=300"
    body = resp.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "SUMMARY:Cena\\, vino\\; y más\r\n" in body
    assert "UID:100@botjonh\r\n" in body and "DTSTART:20261001T010000Z\r\n" in body
    assert "BEGIN:VALARM\r\n" in body and "TRIGGER:-PT15M\r\n" in body
    assert agenda._items.call_args.args[0] == "42"
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("assistant")]
    assert ours == ["ics status=200"]  # the token never reaches our logs


def test_caption_only_gif_from_known_chat_published(fake) -> None:
    body = {
        "update_id": 5,
        "message": {
            "chat": {"id": 42},
            "caption": "gasto",
            "animation": {"file_id": "g"},
        },
    }
    assert client.post(URL, json=body, headers=HEADERS).status_code == 200
    fake.publish.assert_called_once_with("assistant-updates", body)
