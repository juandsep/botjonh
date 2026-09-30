import copy
import dataclasses
import json
import operator
import sys
import types
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import firestore

from assistant.config import get_worker_settings
from assistant.context import ToolContext
from assistant.services import agenda

PANAMA = ZoneInfo("America/Panama")
OPS = {">=": operator.ge, "<": operator.lt}
BASE = "agenda/42/eventos"
SETTINGS = dataclasses.replace(
    get_worker_settings(), worker_url="https://w.run.app", worker_sa="sa@x"
)


def make_ctx(update_id: int = 100, ahora: datetime | None = None) -> ToolContext:
    ahora = ahora or datetime(2026, 9, 29, 12, tzinfo=PANAMA)  # a Tuesday
    return ToolContext("42", "owner", "USD", "America/Panama", update_id, ahora)


class Snap:
    def __init__(self, doc_id: str, data: dict | None) -> None:
        self.id, self._data, self.exists = doc_id, data, data is not None

    def to_dict(self) -> dict | None:
        return copy.deepcopy(self._data)


class Ref:
    def __init__(self, db: "FakeDB", path: str) -> None:
        self.db, self.path = db, path

    def collection(self, name: str) -> "Query":
        return Query(self.db, f"{self.path}/{name}")

    def create(self, data: dict) -> None:
        if self.path in self.db.store:
            raise AlreadyExists("exists")
        assert data["creado"] is firestore.SERVER_TIMESTAMP
        self.db.store[self.path] = {**copy.deepcopy(data), "creado": "ts"}

    def get(self) -> Snap:
        return Snap(self.path.rsplit("/", 1)[1], self.db.store.get(self.path))

    def update(self, data: dict) -> None:
        self.db.store[self.path].update(data)


class Query:
    def __init__(self, db: "FakeDB", path: str, filters: tuple = ()) -> None:
        self.db, self.path, self.filters = db, path, filters

    def document(self, doc_id: str) -> Ref:
        return Ref(self.db, f"{self.path}/{doc_id}")

    def where(self, *, filter: firestore.FieldFilter) -> "Query":
        return Query(self.db, self.path, (*self.filters, filter))

    def stream(self) -> list[Snap]:
        return [
            Snap(path.rsplit("/", 1)[1], data)
            for path, data in sorted(self.db.store.items())
            if path.rsplit("/", 1)[0] == self.path
            and all(OPS[f.op_string](data[f.field_path], f.value) for f in self.filters)
        ]


class FakeDB:
    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    def collection(self, name: str) -> Query:
        return Query(self, name)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr(agenda, "_db", lambda: fake)
    return fake


@pytest.fixture
def tasks(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(agenda, "_tasks", lambda: client)
    monkeypatch.setattr(agenda, "get_worker_settings", lambda: SETTINGS)
    return client


@pytest.fixture
def busy(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """No external calendar unless a test sets busy.bloques."""
    mod = types.ModuleType("assistant.services.busy")
    mod.bloques = []  # type: ignore[attr-defined]
    mod.ocupados = lambda chat_id, desde, hasta: mod.bloques  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "assistant.services.busy", mod)
    return mod


def at(day: int, hour: int, minute: int = 0) -> datetime:
    month = 9 if day >= 29 else 10
    return datetime(2026, month, day, hour, minute, tzinfo=PANAMA)


# --- writes ----------------------------------------------------------------------


def test_crear_evento_is_idempotent_on_retry(
    db: FakeDB, tasks: MagicMock, busy: types.ModuleType
) -> None:
    out = agenda.crear_evento(make_ctx(), "Dentista", datetime(2026, 9, 30, 9))
    assert out == "✓ 30/09 09:00 Dentista [100]"
    doc = db.store[f"{BASE}/100"]
    assert doc["inicio"] == "2026-09-30T09:00:00-05:00"
    assert doc["fin"] == "2026-09-30T10:00:00-05:00"
    assert doc["inicio_utc"] == datetime(2026, 9, 30, 14, tzinfo=UTC)
    assert (doc["tipo"], doc["estado"], doc["recordatorio_min"]) == (
        "evento",
        "activo",
        None,
    )
    retry = agenda.crear_evento(make_ctx(), "Dentista", datetime(2026, 9, 30, 9))
    assert retry == out and list(db.store) == [f"{BASE}/100"]
    tasks.create_task.assert_not_called()  # no reminder asked


def test_second_item_of_same_turn_gets_its_own_id(db: FakeDB, tasks: MagicMock) -> None:
    agenda.crear_evento(make_ctx(), "A", datetime(2026, 9, 30, 9))
    out = agenda.crear_evento(make_ctx(), "B", datetime(2026, 10, 1, 9))
    assert out.endswith("[100-1]")
    assert agenda.crear_evento(make_ctx(), "B", datetime(2026, 10, 1, 9)) == out
    assert sorted(db.store) == [f"{BASE}/100", f"{BASE}/100-1"]


def test_reminder_task_is_deterministic_and_already_exists_is_done(
    db: FakeDB, tasks: MagicMock
) -> None:
    inicio = datetime(2026, 9, 30, 14, tzinfo=UTC)
    agenda.crear_evento(
        make_ctx(), "Cita", inicio, ubicacion="Clínica", recordatorio_min=30
    )
    task = tasks.create_task.call_args.kwargs["task"]
    queue = "projects/test-project/locations/us-central1/queues/assistant-reminders"
    assert tasks.create_task.call_args.kwargs["parent"] == queue
    assert task.name == agenda.task_name(SETTINGS, "42", "100")
    assert (
        task.name.startswith(f"{queue}/tasks/r-") and len(task.name) == len(queue) + 41
    )
    assert task.name != agenda.task_name(SETTINGS, "7", "100")
    assert task.schedule_time == datetime(2026, 9, 30, 13, 30, tzinfo=UTC)
    http = task.http_request
    assert http.url == "https://w.run.app/tasks/reminder"
    assert json.loads(http.body) == {"chat_id": "42", "evento_id": "100"}
    assert http.oidc_token.service_account_email == "sa@x"
    assert http.oidc_token.audience == "https://w.run.app"
    # A retry: the task already exists, which counts as done.
    tasks.create_task.side_effect = AlreadyExists("dup")
    agenda.crear_evento(
        make_ctx(), "Cita", inicio, ubicacion="Clínica", recordatorio_min=30
    )
    assert tasks.create_task.call_count == 2


def test_enqueue_errors_and_missing_config_do_not_fail(
    db: FakeDB, tasks: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level("INFO")
    tasks.create_task.side_effect = RuntimeError("down")
    agenda.recordatorio(make_ctx(1), "Pagar luz", datetime(2026, 10, 1, 8))
    assert "reminder_enqueue_failed error=RuntimeError" in caplog.text
    unset = dataclasses.replace(SETTINGS, worker_url="")
    monkeypatch.setattr(agenda, "get_worker_settings", lambda: unset)
    tasks.reset_mock()
    agenda.recordatorio(make_ctx(2), "Pagar agua", datetime(2026, 10, 1, 8))
    assert agenda.cancelar_evento(make_ctx(), "2") == "✓ evento cancelado"
    tasks.create_task.assert_not_called()
    tasks.delete_task.assert_not_called()
    assert "reminder_skipped" in caplog.text and "Pagar" not in caplog.text


def test_recordatorio_is_15_min_with_task_at_time(db: FakeDB, tasks: MagicMock) -> None:
    agenda.recordatorio(make_ctx(), "Pagar luz", datetime(2026, 10, 1, 8))
    doc = db.store[f"{BASE}/100"]
    assert doc["fin_utc"] - doc["inicio_utc"] == timedelta(minutes=15)
    assert (doc["tipo"], doc["recordatorio_min"]) == ("recordatorio", 0)
    task = tasks.create_task.call_args.kwargs["task"]
    assert task.schedule_time == datetime(2026, 10, 1, 13, tzinfo=UTC)


def test_reminder_beyond_30_days_is_enqueued_later_by_digest(
    db: FakeDB, tasks: MagicMock
) -> None:
    agenda.recordatorio(make_ctx(), "Renovar pasaporte", datetime(2026, 11, 20, 9))
    agenda.recordatorio(make_ctx(101), "Ayer", datetime(2026, 9, 28, 9))  # past
    tasks.create_task.assert_not_called()
    agenda.encolar_recordatorios(make_ctx(0))  # still too far
    tasks.create_task.assert_not_called()
    later = make_ctx(0, datetime(2026, 10, 25, 7, 30, tzinfo=PANAMA))
    agenda.encolar_recordatorios(later)
    task = tasks.create_task.call_args.kwargs["task"]
    assert task.name == agenda.task_name(SETTINGS, "42", "100")
    tasks.create_task.side_effect = AlreadyExists("dup")
    agenda.encolar_recordatorios(later)  # next day's digest: idempotent
    assert tasks.create_task.call_count == 2


def test_cancel_keeps_doc_and_deletes_task(db: FakeDB, tasks: MagicMock) -> None:
    agenda.crear_evento(make_ctx(), "Dentista", at(30, 9), recordatorio_min=10)
    assert agenda.cancelar_evento(make_ctx(), "100") == "✓ evento cancelado"
    assert db.store[f"{BASE}/100"]["estado"] == "cancelado"
    tasks.delete_task.assert_called_once_with(
        name=agenda.task_name(SETTINGS, "42", "100")
    )
    assert agenda.cancelar_evento(make_ctx(), "100") == "Evento no encontrado."
    assert agenda.listar_agenda(make_ctx(), "manana") == "Sin eventos."


def test_cancel_task_not_found_or_failing_is_fine(
    db: FakeDB, tasks: MagicMock, caplog
) -> None:
    agenda.crear_evento(make_ctx(1), "A", at(30, 9))
    agenda.crear_evento(make_ctx(2), "B", at(30, 11))
    tasks.delete_task.side_effect = NotFound("gone")
    assert agenda.cancelar_evento(make_ctx(), "1") == "✓ evento cancelado"
    tasks.delete_task.side_effect = RuntimeError("down")
    assert agenda.cancelar_evento(make_ctx(), "2") == "✓ evento cancelado"
    assert "reminder_delete_failed" in caplog.text


def test_cancel_only_own_and_well_formed_ids(db: FakeDB, tasks: MagicMock) -> None:
    db.store["agenda/7/eventos/5"] = {"estado": "activo"}
    assert agenda.cancelar_evento(make_ctx(), "5") == "Evento no encontrado."
    assert agenda.cancelar_evento(make_ctx(), "../../7/eventos/5") == (
        "Evento no encontrado."
    )
    assert db.store["agenda/7/eventos/5"]["estado"] == "activo"
    tasks.delete_task.assert_not_called()


# --- reads -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rango", "lineas"),
    [
        ("hoy", ["29/09 18:00 Cena [1]"]),
        ("manana", ["30/09 09:00 Dentista [2]"]),
        ("semana", ["29/09 18:00 Cena [1]", "30/09 09:00 Dentista [2]"]),
    ],
)
def test_agenda_ranges(db: FakeDB, tasks: MagicMock, rango: str, lineas: list) -> None:
    agenda.crear_evento(make_ctx(2), "Dentista", at(30, 9))
    agenda.crear_evento(make_ctx(1), "Cena", at(29, 18))
    agenda.crear_evento(make_ctx(3), "Lejos", datetime(2026, 12, 1, 9))
    assert agenda.agenda(make_ctx(), rango) == lineas
    with pytest.raises(ValueError):
        agenda.listar_agenda(make_ctx(), "mes")


def test_conflicts_against_agenda_and_busy(
    db: FakeDB, tasks: MagicMock, busy: types.ModuleType
) -> None:
    agenda.crear_evento(make_ctx(1), "Dentista", at(30, 9))
    agenda.recordatorio(make_ctx(2), "Pastilla", at(30, 9, 30))
    agenda.crear_evento(make_ctx(3), "Viejo", at(30, 9, 15))
    agenda.cancelar_evento(make_ctx(), "3")
    ctx = make_ctx()
    assert agenda.conflictos(ctx, at(30, 9, 30), at(30, 10, 30)) == [
        "Dentista 09:00–10:00"
    ]
    assert agenda.conflictos(ctx, at(30, 10), at(30, 11)) == []  # [inicio, fin)
    busy.bloques = [  # type: ignore[attr-defined]
        (at(30, 10, 30).astimezone(UTC), at(30, 11).astimezone(UTC), "Ocupado")
    ]
    assert agenda.conflictos(ctx, datetime(2026, 9, 30, 10), at(30, 11)) == [
        "Ocupado 10:30–11:00"
    ]


def test_conflicts_ignore_missing_or_failing_busy(
    db: FakeDB, tasks: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    agenda.crear_evento(make_ctx(1), "Dentista", at(30, 9))
    monkeypatch.setitem(sys.modules, "assistant.services.busy", None)  # missing
    assert agenda.conflictos(make_ctx(), at(30, 9), at(30, 10)) == [
        "Dentista 09:00–10:00"
    ]
    boom = types.ModuleType("assistant.services.busy")

    def ocupados(*a: object) -> list:
        raise RuntimeError("ics down https://secret.example/x.ics")

    boom.ocupados = ocupados  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "assistant.services.busy", boom)
    assert agenda.conflictos(make_ctx(), at(30, 9), at(30, 10)) == [
        "Dentista 09:00–10:00"
    ]
    assert "busy_failed error=RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_libres_merges_agenda_and_busy(
    db: FakeDB, tasks: MagicMock, busy: types.ModuleType
) -> None:
    agenda.crear_evento(make_ctx(1), "Dentista", at(30, 9), at(30, 10))
    agenda.recordatorio(make_ctx(2), "Pastilla", at(30, 12))  # not busy time
    busy.bloques = [  # type: ignore[attr-defined]
        (at(30, 9, 30), at(30, 11), "Ocupado"),
        (at(30, 19), at(30, 21), "Ocupado"),
    ]
    assert agenda.libres(make_ctx(), date(2026, 9, 30)) == [
        "08:00–09:00",
        "11:00–19:00",
    ]
    busy.bloques = [(at(30, 7), at(30, 21), "Ocupado")]  # type: ignore[attr-defined]
    assert agenda.ver_libres(make_ctx(), date(2026, 9, 30)) == "Sin huecos libres."
    busy.bloques = []  # type: ignore[attr-defined]
    assert agenda.ver_libres(make_ctx(), date(2026, 10, 2)) == "08:00–20:00"


def test_semana_format(db: FakeDB, tasks: MagicMock, busy: types.ModuleType) -> None:
    assert agenda.semana(make_ctx()) == "Sin nada en 7 días."
    agenda.crear_evento(make_ctx(1), "Llamada banco", at(1, 16))
    agenda.crear_evento(make_ctx(2), "Dentista", at(1, 9))
    agenda.crear_evento(make_ctx(3), "Fuera", at(6, 9))  # day 8: outside
    busy.bloques = [(at(29, 15), at(29, 16), "Ocupado")]  # type: ignore[attr-defined]
    assert agenda.semana(make_ctx()) == (
        "Mar 29 · 15:00 Ocupado\nJue 1 · 09:00 Dentista · 16:00 Llamada banco"
    )


def test_aviso(db: FakeDB, tasks: MagicMock) -> None:
    agenda.crear_evento(make_ctx(), "Dentista", at(30, 9), recordatorio_min=15)
    assert agenda.aviso("42", "100") == "⏰ Dentista 09:00"
    assert agenda.aviso("42", "999") is None
    assert agenda.aviso("42", "a/b") is None
    agenda.cancelar_evento(make_ctx(), "100")
    assert agenda.aviso("42", "100") is None


# --- ICS -------------------------------------------------------------------------


def test_ics_escapes_folds_and_alarms(db: FakeDB, tasks: MagicMock) -> None:
    titulo = "Cena, vino; y \\ más\nnotas " + "ñ" * 60
    agenda.crear_evento(
        make_ctx(1),
        titulo,
        at(30, 20),
        ubicacion="Calle 5, Local 2",
        recordatorio_min=30,
    )
    agenda.crear_evento(make_ctx(2), "Sin aviso", at(30, 9))
    agenda.crear_evento(make_ctx(3), "Cancelado", at(30, 10))
    agenda.cancelar_evento(make_ctx(), "3")
    agenda.crear_evento(make_ctx(4), "Viejo", datetime(2026, 7, 1, 9))
    body = agenda.ics("42", datetime(2026, 9, 29, 17, tzinfo=UTC))
    assert body.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert body.endswith("END:VCALENDAR\r\n")
    assert "\n" not in body.replace("\r\n", "")
    lines = body.split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines)
    assert "X-WR-CALNAME:botjonh" in lines and "PRODID:-//botjonh//agenda//ES" in lines
    unfolded = body.replace("\r\n ", "")
    assert "SUMMARY:Cena\\, vino\\; y \\\\ más\\nnotas " + "ñ" * 60 in unfolded
    assert "LOCATION:Calle 5\\, Local 2" in lines
    assert "UID:1@botjonh" in lines and "UID:2@botjonh" in lines
    assert "Cancelado" not in body and "Viejo" not in body
    assert "DTSTART:20260930T140000Z" in lines and "DTEND:20260930T150000Z" in lines
    assert "DTSTART:20261001T010000Z" in lines  # 20:00 Panama
    assert unfolded.count("BEGIN:VALARM") == 1
    assert "TRIGGER:-PT30M" in lines and "ACTION:DISPLAY" in lines


def test_tasks_client_is_lazy_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    agenda._tasks.cache_clear()
    ctor = MagicMock()
    monkeypatch.setattr(agenda.tasks_v2, "CloudTasksClient", ctor)
    assert agenda._tasks() is agenda._tasks()
    ctor.assert_called_once()
    agenda._tasks.cache_clear()
