import json
import logging
import re
import sys
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from google.cloud import firestore

from assistant.context import ToolContext
from assistant.services import gcal

CAL = "yo.secreto@gmail.com"
EVENTS = f"{gcal.API}/calendars/yo.secreto%40gmail.com/events"
PANAMA = ZoneInfo("America/Panama")
DESDE = datetime(2026, 9, 30, tzinfo=UTC)
HASTA = datetime(2026, 10, 1, tzinfo=UTC)
EVENTO = {
    "titulo": "Dentista",
    "inicio": "2026-09-30T09:00:00-05:00",
    "fin": "2026-09-30T10:00:00-05:00",
    "ubicacion": "Clínica",
    "recordatorio_min": 15,
}


def ctx(chat_id: str = "42") -> ToolContext:
    ahora = datetime(2026, 9, 29, 12, tzinfo=PANAMA)
    return ToolContext(chat_id, "owner", "USD", "America/Panama", 1, ahora)


@pytest.fixture(autouse=True)
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(valid=True, token="tok")  # noqa: S106
    monkeypatch.setattr(gcal, "_creds", lambda: fake)


@pytest.fixture
def prefs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """In-memory preferences/users behind the state module."""
    store: dict = {"gcal_id": CAL}
    mod = types.SimpleNamespace(
        get_preferences=lambda chat_id: store if chat_id == "42" else {},
        get_user=lambda chat_id: {"zona_horaria": "America/Panama"},
    )
    monkeypatch.setitem(sys.modules, "assistant.services.state", mod)
    return store


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr(gcal, "_db", lambda: fake)
    return fake


def test_event_id_is_deterministic_and_valid_for_google() -> None:
    gid = gcal.evento_gid("42", "100")
    assert gid == gcal.evento_gid("42", "100") != gcal.evento_gid("7", "100")
    assert re.fullmatch(r"[a-v0-9]{5,1024}", gid) and gcal._MIO.fullmatch(gid)


# --- vincular --------------------------------------------------------------------


@respx.mock
def test_vincular_probes_write_access_and_stores(db: MagicMock) -> None:
    insert = respx.post(EVENTS).respond(200, json={"id": "probe1"})
    delete = respx.delete(f"{EVENTS}/probe1").respond(204)
    assert (
        gcal.vincular(ctx(), f" {CAL} ")
        == "✓ Google Calendar vinculado (0 eventos copiados)."
    )
    assert insert.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert delete.called
    db.collection.assert_any_call("preferences")
    db.collection().document.assert_any_call("42")
    db.collection().document().set.assert_called_once_with({"gcal_id": CAL}, merge=True)


@pytest.mark.parametrize("code", [403, 404])
def test_vincular_without_access(
    db: MagicMock, caplog: pytest.LogCaptureFixture, code: int
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock:
        respx.post(EVENTS).respond(code)
        assert gcal.vincular(ctx(), CAL) == gcal.NO_ACCESS
    assert "assistant-worker@jd-botjonh" in gcal.NO_ACCESS
    db.collection().document().set.assert_not_called()
    assert "secreto" not in caplog.text


@respx.mock
def test_vincular_network_error(db: MagicMock) -> None:
    respx.post(EVENTS).mock(side_effect=httpx.ConnectTimeout("t"))
    assert gcal.vincular(ctx(), CAL).startswith("No pude verificar")
    db.collection().document().set.assert_not_called()


@pytest.mark.parametrize(
    "bad", ["no-arroba", "a@b", "x" * 191 + "@gmail.com", "a b@gmail.com", "../x@y.co"]
)
def test_vincular_rejects_bad_ids_without_calls(db: MagicMock, bad: str) -> None:
    with respx.mock:  # any request would fail as unmatched
        assert gcal.vincular(ctx(), bad) == "Id de calendario no válido."
    db.collection().document().set.assert_not_called()


def test_vincular_accepts_group_calendars(db: MagicMock) -> None:
    group = "abc123@group.calendar.google.com"
    with respx.mock:
        respx.post(f"{gcal.API}/calendars/{group.replace('@', '%40')}/events").respond(
            200, json={"id": "p"}
        )
        respx.delete(url__regex=r".*/events/p").respond(204)
        assert (
            gcal.vincular(ctx(), group)
            == "✓ Google Calendar vinculado (0 eventos copiados)."
        )


def test_vincular_off_unlinks(db: MagicMock) -> None:
    assert gcal.vincular(ctx(), "OFF") == "Google Calendar desvinculado."
    db.collection().document().set.assert_called_once_with(
        {"gcal_id": firestore.DELETE_FIELD}, merge=True
    )


# --- mirror ----------------------------------------------------------------------


@respx.mock
def test_espejo_crear_inserts_with_deterministic_id(prefs: dict) -> None:
    route = respx.post(EVENTS).respond(200, json={})
    gcal.espejo_crear(ctx(), "100", EVENTO)
    body = route.calls[0].request.read().decode()
    assert gcal.evento_gid("42", "100") in body
    sent = json.loads(body)
    assert sent["start"] == {"dateTime": EVENTO["inicio"]}
    assert sent["location"] == "Clínica"
    assert sent["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 15}],
    }


@respx.mock
def test_espejo_crear_409_is_done(
    prefs: dict, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    respx.post(EVENTS).respond(409)
    gcal.espejo_crear(ctx(), "100", {**EVENTO, "recordatorio_min": None})
    assert "gcal_mirror" in caplog.text and "failed" not in caplog.text


@pytest.mark.parametrize("code", [204, 404, 410])
def test_espejo_cancelar_tolerates_gone(
    prefs: dict, caplog: pytest.LogCaptureFixture, code: int
) -> None:
    with respx.mock:
        route = respx.delete(f"{EVENTS}/{gcal.evento_gid('42', '100')}")
        route.respond(code)
        gcal.espejo_cancelar(ctx(), "100")
        assert route.called
    assert "failed" not in caplog.text


@pytest.mark.parametrize(
    "response", [httpx.Response(500), httpx.ConnectTimeout("t"), RuntimeError("x")]
)
def test_mirror_failures_never_raise_and_log_no_pii(
    prefs: dict, caplog: pytest.LogCaptureFixture, response: object
) -> None:
    with respx.mock:
        kw = (
            {"side_effect": response}
            if isinstance(response, Exception)
            else {"return_value": response}
        )
        respx.post(EVENTS).mock(**kw)
        respx.delete(url__regex=r".*/events/.*").mock(**kw)
        gcal.espejo_crear(ctx(), "100", EVENTO)
        gcal.espejo_cancelar(ctx(), "100")
    assert "gcal_mirror_failed" in caplog.text and "gcal_unmirror_failed" in caplog.text
    for pii in ("secreto", "Dentista", "42"):
        assert pii not in caplog.text


def test_mirror_is_noop_when_unlinked(prefs: dict) -> None:
    with respx.mock:  # any request would fail as unmatched
        gcal.espejo_crear(ctx("7"), "100", EVENTO)
        gcal.espejo_cancelar(ctx("7"), "100")


# --- ocupados --------------------------------------------------------------------


@respx.mock
def test_ocupados_excludes_our_mirrors_and_free_events(prefs: dict) -> None:
    mine = gcal.evento_gid("42", "100")
    route = respx.get(EVENTS).respond(
        200,
        json={
            "items": [
                {
                    "id": mine,
                    "start": {"dateTime": "2026-09-30T09:00:00-05:00"},
                    "end": {"dateTime": "2026-09-30T10:00:00-05:00"},
                },
                {
                    "id": "abc",
                    "start": {"dateTime": "2026-09-30T11:00:00-05:00"},
                    "end": {"dateTime": "2026-09-30T12:00:00-05:00"},
                },
                {
                    "id": "free",
                    "transparency": "transparent",
                    "start": {"dateTime": "2026-09-30T13:00:00-05:00"},
                    "end": {"dateTime": "2026-09-30T14:00:00-05:00"},
                },
                {"id": "gone", "status": "cancelled"},
                {
                    "id": "allday",
                    "start": {"date": "2026-09-30"},
                    "end": {"date": "2026-10-01"},
                },
            ]
        },
    )
    assert gcal.ocupados("42", DESDE, HASTA) == [
        (
            datetime(2026, 9, 30, 5, tzinfo=UTC),
            datetime(2026, 10, 1, 5, tzinfo=UTC),
            "Ocupado",
        ),
        (
            datetime(2026, 9, 30, 16, tzinfo=UTC),
            datetime(2026, 9, 30, 17, tzinfo=UTC),
            "Ocupado",
        ),
    ]
    params = route.calls[0].request.url.params
    assert params["singleEvents"] == "true"
    assert params["timeMin"] == "2026-09-30T00:00:00+00:00"


@pytest.mark.parametrize(
    "response", [httpx.Response(403), httpx.ConnectTimeout("t"), httpx.Response(200)]
)
def test_ocupados_failure_is_empty(
    prefs: dict, caplog: pytest.LogCaptureFixture, response: object
) -> None:
    with respx.mock:
        if isinstance(response, Exception):
            respx.get(EVENTS).mock(side_effect=response)
        else:
            respx.get(EVENTS).mock(return_value=response)
        assert gcal.ocupados("42", DESDE, HASTA) == []
    assert "gcal_busy_failed" in caplog.text and "secreto" not in caplog.text


def test_ocupados_unlinked_is_empty(prefs: dict) -> None:
    with respx.mock:
        assert gcal.ocupados("7", DESDE, HASTA) == []


def test_credentials_are_lazy_cached_and_refreshed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # drop the fake creds
    gcal._creds.cache_clear()
    creds = MagicMock(valid=False, token="new")  # noqa: S106
    default = MagicMock(return_value=(creds, "p"))
    monkeypatch.setattr("google.auth.default", default)
    with respx.mock:
        respx.get("https://x.example/").respond(200)
        monkeypatch.setattr(gcal, "API", "https://x.example")
        gcal._call("GET", "/")
        gcal._call("GET", "/")
    default.assert_called_once_with(scopes=[gcal.SCOPE])
    assert creds.refresh.call_count == 2
    gcal._creds.cache_clear()


def test_backfill_mirrors_only_active_items(
    db: MagicMock, prefs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    snaps = [
        types.SimpleNamespace(id="1", to_dict=lambda: {"estado": "activo"}),
        types.SimpleNamespace(id="2", to_dict=lambda: {"estado": "cancelado"}),
    ]
    db.collection().document().collection().where().stream.return_value = snaps
    mirrored: list[str] = []
    monkeypatch.setattr(gcal, "espejo_crear", lambda c, i, e: mirrored.append(i))
    assert gcal._backfill(ctx()) == 1
    assert mirrored == ["1"]
