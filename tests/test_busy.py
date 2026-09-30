import logging
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from google.cloud import firestore

from assistant.context import ToolContext
from assistant.services import busy

MARKER = "zzmarkerzz"
URL = f"https://calendar.google.com/calendar/ical/x/{MARKER}/basic.ics"
PANAMA = ZoneInfo("America/Panama")
DESDE = datetime(2026, 9, 28, tzinfo=UTC)
HASTA = datetime(2026, 10, 5, tzinfo=UTC)

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:single
DTSTART:20260929T150000Z
DTEND:20260929T160000Z
SUMMARY:Dentist appointment
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTART:20260901T130000Z
DTEND:20260901T133000Z
RRULE:FREQ=WEEKLY;BYDAY=TU,TH
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:allday
DTSTART;VALUE=DATE:20261001
DTEND;VALUE=DATE:20261002
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
UID:free
DTSTART:20260930T150000Z
DTEND:20260930T160000Z
TRANSP:TRANSPARENT
SUMMARY:Reminder
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART:20260930T170000Z
DTEND:20260930T180000Z
STATUS:CANCELLED
SUMMARY:Called off
END:VEVENT
BEGIN:VEVENT
UID:outside
DTSTART:20261020T150000Z
DTEND:20261020T160000Z
SUMMARY:Later
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")


def ctx(chat_id: str = "42") -> ToolContext:
    ahora = datetime(2026, 9, 29, 12, tzinfo=PANAMA)
    return ToolContext(chat_id, "beta", "USD", "America/Panama", 1, ahora)


@pytest.fixture(autouse=True)
def clean_cache() -> None:
    busy._cache.clear()


class FakeKms:
    """Reversible stand-in for crypto; binds the ciphertext to the chat id."""

    @staticmethod
    def encrypt(key: str, plaintext: str, chat_id: str) -> str:
        assert key == "k"
        return f"enc:{chat_id}:{plaintext[::-1]}"

    @staticmethod
    def decrypt(key: str, ciphertext: str, chat_id: str) -> str:
        prefix = f"enc:{chat_id}:"
        if not ciphertext.startswith(prefix):
            raise ValueError("aad mismatch")
        return ciphertext[len(prefix) :][::-1]


ENC = FakeKms.encrypt("k", URL, "42")


@pytest.fixture(autouse=True)
def kms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(busy, "crypto", FakeKms)
    monkeypatch.setattr(busy, "get_worker_settings", lambda: MagicMock(kms_key="k"))


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = MagicMock()
    mod.get_preferences.return_value = {"ics_url_enc": ENC}
    mod.get_user.return_value = {"zona_horaria": "America/Panama"}
    monkeypatch.setitem(sys.modules, "assistant.services.state", mod)
    return mod


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr(busy, "_db", lambda: fake)
    return fake


def ok(text: str = ICS) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"Content-Type": "text/calendar"})


# --- URL validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        URL,
        "https://outlook.office365.com/owa/calendar/x/reachcalendar.ics",
        "https://outlook.live.com/owa/calendar/x/calendar.ics",
        "https://calendar.google.com:443/calendar/ical/x.ics",
        "webcal://p42-caldav.icloud.com/published/2/abc",
        "https://p123-caldav.icloud.com/published/2/abc",
    ],
)
def test_valid_urls(raw: str) -> None:
    url = busy.validar(raw)
    assert url.scheme == "https"


@pytest.mark.parametrize(
    "raw",
    [
        "http://calendar.google.com/calendar/ical/x.ics",
        "https://evil.example.com/x.ics",
        "https://calendar.google.com.evil.com/x.ics",
        "https://caldav.icloud.com/x",
        "https://px-caldav.icloud.com/x",
        "https://127.0.0.1/x.ics",
        "https://[::1]/x.ics",
        "https://169.254.169.254/latest",
        "https://calendar.google.com:8443/x.ics",
        "https://user:pw@calendar.google.com/x.ics",  # pragma: allowlist secret
        "https://calendar.google.com@evil.com/x.ics",
        "ftp://calendar.google.com/x.ics",
        "not a url",
        "",
    ],
)
def test_invalid_urls(raw: str) -> None:
    with pytest.raises(busy.BusyError, match="invalid_url"):
        busy.validar(raw)


def test_webcal_is_fetched_over_https(db: MagicMock) -> None:
    with respx.mock:
        route = respx.get("https://p42-caldav.icloud.com/published/2/abc")
        route.return_value = ok()
        out = busy.conectar(ctx(), "webcal://p42-caldav.icloud.com/published/2/abc")
    assert out == "✓ Calendario conectado."
    assert route.calls[0].request.headers["Accept"] == "text/calendar"


# --- conectar ----------------------------------------------------------------


@respx.mock
def test_conectar_stores_url(db: MagicMock) -> None:
    respx.get(URL).return_value = ok()
    assert busy.conectar(ctx(), URL) == "✓ Calendario conectado."
    db.collection.assert_called_with("preferences")
    db.collection().document.assert_called_with("42")
    db.collection().document().set.assert_called_once_with(
        {"ics_url_enc": ENC, "ics_url": firestore.DELETE_FIELD}, merge=True
    )
    stored = str(db.collection().document().set.call_args)
    assert MARKER not in stored  # never in clear


def test_conectar_refuses_without_kms_key(
    db: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(busy, "get_worker_settings", lambda: MagicMock(kms_key=""))
    with respx.mock:  # no fetch either
        assert busy.conectar(ctx(), URL) == "Aún no disponible."
    db.collection().document().set.assert_not_called()


def test_ciphertext_of_another_chat_does_not_decrypt(state: MagicMock) -> None:
    state.get_preferences.return_value = {"ics_url_enc": ENC}
    with respx.mock:  # decrypt fails before any fetch
        assert busy.ocupados("99", DESDE, HASTA) == []


def test_conectar_rejects_invalid_without_fetch(db: MagicMock) -> None:
    with respx.mock:  # any request would fail as unmatched
        assert busy.conectar(ctx(), "http://10.0.0.1/x") == "Enlace no válido."
    db.collection().document().set.assert_not_called()


@respx.mock
def test_redirect_is_rejected(db: MagicMock) -> None:
    respx.get(URL).return_value = httpx.Response(
        302, headers={"Location": "https://169.254.169.254/"}
    )
    assert busy.conectar(ctx(), URL) == "No pude leer ese calendario."
    db.collection().document().set.assert_not_called()


@respx.mock
def test_oversize_body_is_rejected(db: MagicMock) -> None:
    respx.get(URL).return_value = ok("X" * (busy.MAX_BYTES + 1))
    assert busy.conectar(ctx(), URL) == "No pude leer ese calendario."
    db.collection().document().set.assert_not_called()


@respx.mock
def test_unparseable_body_is_rejected(db: MagicMock) -> None:
    respx.get(URL).return_value = ok("<html>login</html>")
    assert busy.conectar(ctx(), URL) == "No pude leer ese calendario."


def test_off_disconnects(db: MagicMock) -> None:
    busy._cache["42"] = (0.0, ENC, None)
    assert busy.conectar(ctx(), "off") == "Calendario desconectado."
    db.collection().document().set.assert_called_once_with(
        {"ics_url": firestore.DELETE_FIELD, "ics_url_enc": firestore.DELETE_FIELD},
        merge=True,
    )
    assert "42" not in busy._cache


# --- ocupados ----------------------------------------------------------------


def utc(d: int, h: int, m: int = 0) -> datetime:
    month = 9 if d > 20 else 10
    return datetime(2026, month, d, h, m, tzinfo=UTC)


@respx.mock
def test_ocupados_expands_and_filters(state: MagicMock) -> None:
    respx.get(URL).return_value = ok()
    blocks = busy.ocupados("42", DESDE, HASTA)
    assert blocks == [
        (utc(29, 13), utc(29, 13, 30), "Ocupado"),  # weekly Tue
        (utc(29, 15), utc(29, 16), "Ocupado"),  # single
        # all-day Oct 1 in Panama (UTC-5)
        (utc(1, 5), datetime(2026, 10, 2, 5, tzinfo=UTC), "Ocupado"),
        (utc(1, 13), utc(1, 13, 30), "Ocupado"),  # weekly Thu
    ]
    assert all(b[0].tzinfo is not None for b in blocks)
    text = repr(blocks)
    for title in ("Dentist", "Standup", "Holiday", "Reminder", "Called off"):
        assert title not in text


@respx.mock
def test_ocupados_caches_per_chat(state: MagicMock) -> None:
    route = respx.get(URL)
    route.return_value = ok()
    busy.ocupados("42", DESDE, HASTA)
    busy.ocupados("42", DESDE, utc(30, 0))
    assert route.call_count == 1


def test_ocupados_without_url(state: MagicMock) -> None:
    state.get_preferences.return_value = {}
    with respx.mock:
        assert busy.ocupados("42", DESDE, HASTA) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(301, headers={"Location": "https://evil.example.com"}),
        httpx.ConnectTimeout("timed out"),
        ok("garbage"),
    ],
)
def test_ocupados_failure_logs_code_only(
    state: MagicMock, caplog: pytest.LogCaptureFixture, response: object
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock:
        respx.get(URL).mock(
            side_effect=response if isinstance(response, Exception) else None,
            return_value=None if isinstance(response, Exception) else response,
        )
        assert busy.ocupados("42", DESDE, HASTA) == []
    assert "ics_busy_failed" in caplog.text
    assert MARKER not in caplog.text and "calendar.google.com" not in caplog.text
