from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from assistant.context import ToolContext
from assistant.services import calendar

PANAMA = ZoneInfo("America/Panama")


def make_ctx(ahora: datetime, chat_id: str = "42") -> ToolContext:
    return ToolContext(chat_id, "owner", "USD", "America/Panama", 1, ahora)


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(calendar, "_service", lambda: mock)
    return mock


NOON = make_ctx(datetime(2026, 9, 29, 12, tzinfo=PANAMA))


def test_crear_evento_defaults(svc: MagicMock) -> None:
    svc.events().insert().execute.return_value = {"id": "e1"}
    out = calendar.crear_evento(NOON, "Dentista", datetime(2026, 9, 30, 9))
    assert out == "✓ 30/09 09:00 Dentista [e1]"
    body = svc.events().insert.call_args.kwargs["body"]
    assert body["start"]["dateTime"] == "2026-09-30T09:00:00-05:00"
    assert body["end"]["dateTime"] == "2026-09-30T10:00:00-05:00"
    assert body["reminders"] == {"useDefault": True}
    assert body["extendedProperties"]["private"]["chat_id"] == "42"
    assert "location" not in body


def test_crear_evento_with_reminder_and_location(svc: MagicMock) -> None:
    svc.events().insert().execute.return_value = {"id": "e2"}
    inicio = datetime(2026, 9, 30, 14, tzinfo=ZoneInfo("UTC"))
    calendar.crear_evento(
        NOON, "Cita", inicio, ubicacion="Clínica", recordatorio_min=30
    )
    body = svc.events().insert.call_args.kwargs["body"]
    assert body["start"]["dateTime"] == "2026-09-30T09:00:00-05:00"
    assert body["location"] == "Clínica"
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


def test_recordatorio_is_15_min_popup_at_time(svc: MagicMock) -> None:
    svc.events().insert().execute.return_value = {"id": "r1"}
    calendar.recordatorio(NOON, "Pagar luz", datetime(2026, 10, 1, 8))
    body = svc.events().insert.call_args.kwargs["body"]
    start = datetime.fromisoformat(body["start"]["dateTime"])
    end = datetime.fromisoformat(body["end"]["dateTime"])
    assert end - start == timedelta(minutes=15)
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 0}]


@pytest.mark.parametrize(
    ("rango", "desde", "hasta"),
    [
        ("hoy", "2026-09-29T00:00:00-05:00", "2026-09-30T00:00:00-05:00"),
        ("manana", "2026-09-30T00:00:00-05:00", "2026-10-01T00:00:00-05:00"),
        ("semana", "2026-09-29T00:00:00-05:00", "2026-10-06T00:00:00-05:00"),
    ],
)
def test_listar_agenda_range_in_panama(
    svc: MagicMock, rango: str, desde: str, hasta: str
) -> None:
    # 04:30 UTC on the 30th is still the 29th in Panama.
    late = make_ctx(datetime(2026, 9, 30, 4, 30, tzinfo=ZoneInfo("UTC")))
    svc.events().list().execute.return_value = {"items": []}
    assert calendar.listar_agenda(late, rango) == "Sin eventos."
    kw = svc.events().list.call_args.kwargs
    assert (kw["timeMin"], kw["timeMax"]) == (desde, hasta)
    assert kw["privateExtendedProperty"] == "chat_id=42"


def test_listar_agenda_lines(svc: MagicMock) -> None:
    svc.events().list().execute.return_value = {
        "items": [
            {
                "id": "e1",
                "summary": "Dentista",
                "start": {"dateTime": "2026-09-29T09:00:00-05:00"},
            },
            {"id": "e2", "summary": "Feriado", "start": {"date": "2026-09-29"}},
        ]
    }
    assert calendar.listar_agenda(NOON, "hoy") == (
        "29/09 09:00 Dentista [e1]\n29/09 todo el día Feriado [e2]"
    )
    with pytest.raises(ValueError):
        calendar.listar_agenda(NOON, "mes")


def test_cancelar_evento(svc: MagicMock) -> None:
    events = svc.events()
    events.get().execute.return_value = {
        "extendedProperties": {"private": {"chat_id": "42"}}
    }
    assert calendar.cancelar_evento(NOON, "e1") == "✓ evento cancelado"
    events.delete.assert_called_with(calendarId="", eventId="e1")


def test_cancelar_evento_of_other_chat_is_refused(svc: MagicMock) -> None:
    events = svc.events()
    events.get().execute.return_value = {
        "extendedProperties": {"private": {"chat_id": "7"}}
    }
    assert calendar.cancelar_evento(NOON, "e1") == "Evento no encontrado."
    events.delete.assert_not_called()


def test_cancelar_evento_not_found_and_errors(svc: MagicMock) -> None:
    events = svc.events()
    events.get().execute.side_effect = HttpError(MagicMock(status=404), b"")
    assert calendar.cancelar_evento(NOON, "x") == "Evento no encontrado."
    events.get().execute.side_effect = HttpError(MagicMock(status=500), b"")
    with pytest.raises(HttpError):
        calendar.cancelar_evento(NOON, "x")


def test_service_is_lazy_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calendar._service.cache_clear()
    monkeypatch.setattr(calendar.google.auth, "default", MagicMock(return_value=(1, 2)))
    build = MagicMock()
    monkeypatch.setattr(calendar, "build", build)
    assert calendar._service() is calendar._service()
    build.assert_called_once()
    calendar._service.cache_clear()
