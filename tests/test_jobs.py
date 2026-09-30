import dataclasses
import json
import sys
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from google.api_core.exceptions import PreconditionFailed

import assistant.jobs as jobs
from assistant.config import get_worker_settings
from assistant.jobs import backup
from assistant.services import budgets, calendar, ledger


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = MagicMock()
    state.list_chat_ids.return_value = ["42"]
    state.get_user.return_value = {"rol": "owner", "moneda": "USD"}
    state.get_preferences.return_value = {}
    monkeypatch.setitem(sys.modules, "assistant.services.state", state)
    telegram = MagicMock()
    monkeypatch.setattr(jobs, "Telegram", lambda token: telegram)
    monkeypatch.setattr(calendar, "agenda", lambda ctx, rango: [])
    monkeypatch.setattr(ledger, "gastos_por_categoria", lambda *a: {})
    monkeypatch.setattr(ledger, "total_ingresos", lambda *a: Decimal("0.00"))
    run_backup = MagicMock()
    monkeypatch.setattr(backup, "run", run_backup)
    export = MagicMock()
    monkeypatch.setattr(backup, "export_ledger", export)
    return SimpleNamespace(
        state=state, telegram=telegram, backup=run_backup, export=export
    )


def test_unknown_job() -> None:
    with pytest.raises(ValueError):
        jobs.run_job("nope")


def test_digest_sends_nothing_when_empty(env: SimpleNamespace) -> None:
    jobs.run_job("digest")
    env.telegram.send_message.assert_not_called()


def test_digest_agenda_and_yesterday(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(calendar, "agenda", lambda ctx, rango: ["29/09 09:00 X [e1]"])
    monkeypatch.setattr(
        ledger, "gastos_por_categoria", lambda *a: {"otros": Decimal("4.50")}
    )
    jobs.run_job("digest")
    env.telegram.send_message.assert_called_once_with(
        "42", "29/09 09:00 X [e1]\nAyer: 4.50 USD."
    )


def test_checkin_only_when_nothing_logged(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.run_job("checkin")
    assert env.telegram.send_message.call_count == 1
    monkeypatch.setattr(ledger, "total_ingresos", lambda *a: Decimal("5.00"))
    jobs.run_job("checkin")
    monkeypatch.setattr(ledger, "gastos_por_categoria", lambda *a: {"otros": 1})
    jobs.run_job("checkin")
    assert env.telegram.send_message.call_count == 1


def test_weekly_backs_up_and_summarizes(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.run_job("weekly")
    env.backup.assert_called_once()
    env.telegram.send_message.assert_not_called()  # no spend, nothing to say
    monkeypatch.setattr(
        ledger, "gastos_por_categoria", lambda *a: {"restaurantes": Decimal("80.00")}
    )
    jobs.run_job("weekly")
    env.telegram.send_message.assert_called_with("42", "Semana: 80.00 USD.")
    monkeypatch.setattr(ledger, "total_ingresos", lambda *a: Decimal("1000.00"))
    jobs.run_job("weekly")
    text = env.telegram.send_message.call_args.args[1]
    assert text.splitlines()[1].startswith("Exceso en ocio (50/30/20): 80.00 de 70.00")
    assert len(text.splitlines()) == 2


def test_one_failing_chat_does_not_stop_others(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.state.list_chat_ids.return_value = ["1", "2", "3"]
    env.state.get_user.side_effect = [RuntimeError("boom"), None, {"moneda": "USD"}]
    jobs.run_job("checkin")
    env.telegram.send_message.assert_called_once()
    assert env.telegram.send_message.call_args.args[0] == "3"


class FakeBucket:
    """storage bucket whose blobs only support create (if_generation_match=0)."""

    def __init__(self) -> None:
        self.uploads: dict[str, str] = {}

    def blob(self, name: str) -> SimpleNamespace:
        def upload(data: str, content_type: str, **kw: object) -> None:
            if kw.get("if_generation_match") == 0 and name in self.uploads:
                raise PreconditionFailed("exists")
            self.uploads[name] = data

        return SimpleNamespace(upload_from_string=upload)


@pytest.fixture
def gcs(monkeypatch: pytest.MonkeyPatch) -> FakeBucket:
    bucket = FakeBucket()
    client = MagicMock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(backup.storage, "Client", lambda project: client)
    return bucket


SETTINGS = dataclasses.replace(get_worker_settings(), backup_bucket="b")


def test_digest_exports_before_messages_and_survives_failure(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    env.export.side_effect = lambda s: order.append("export")
    env.telegram.send_message.side_effect = lambda *a: order.append("send")
    monkeypatch.setattr(calendar, "agenda", lambda ctx, rango: ["x"])
    jobs.run_job("digest")
    assert order == ["export", "send"]
    env.export.side_effect = RuntimeError("gcs down")
    jobs.run_job("digest")
    assert env.telegram.send_message.call_count == 2
    jobs.run_job("checkin")
    assert env.export.call_count == 2  # only the digest exports


def test_backup_writes_json_objects(
    monkeypatch: pytest.MonkeyPatch, gcs: FakeBucket
) -> None:
    doc = SimpleNamespace(id="42", to_dict=lambda: {"nombre": "J", "n": Decimal(1)})
    mov = SimpleNamespace(
        reference=SimpleNamespace(path="ledger/42/movimientos/100-0"),
        to_dict=lambda: {"monto": "2.00", "tipo_mov": "gasto"},
    )
    db = MagicMock()
    db.collection.return_value.stream.return_value = [doc]
    db.collection_group.return_value.stream.return_value = [mov]
    monkeypatch.setattr(backup.firestore, "Client", lambda project: db)
    backup.run(SETTINGS)
    db.collection_group.assert_called_once_with("movimientos")
    root, dia, _ = next(iter(gcs.uploads)).split("/", 2)
    assert root == "backup" and len(dia) == 10 and dia[4] == "-"  # YYYY-MM-DD
    assert sorted(n.split("/", 2)[2] for n in gcs.uploads) == [
        "firestore/invites.json",
        "firestore/ledger.json",
        "firestore/pending.json",
        "firestore/preferences.json",
        "firestore/users.json",
    ]
    users = json.loads(gcs.uploads[f"backup/{dia}/firestore/users.json"])
    assert users == {"42": {"nombre": "J", "n": "1"}}
    movs = json.loads(gcs.uploads[f"backup/{dia}/firestore/ledger.json"])
    assert movs == {
        "ledger/42/movimientos/100-0": {"monto": "2.00", "tipo_mov": "gasto"}
    }


def test_backup_skipped_without_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(backup.firestore, "Client", client)
    backup.run(dataclasses.replace(get_worker_settings(), backup_bucket=""))
    backup.export_ledger(dataclasses.replace(get_worker_settings(), backup_bucket=""))
    client.assert_not_called()


class FixedNow(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "FixedNow":  # type: ignore[override]
        # 03:00 UTC on the 30th is still the 29th in Panama (UTC-5).
        return cls(2026, 9, 30, 3, tzinfo=UTC).astimezone(tz)  # type: ignore[return-value]


@pytest.fixture
def export_env(
    monkeypatch: pytest.MonkeyPatch, gcs: FakeBucket
) -> list[tuple[object, ...]]:
    state = MagicMock()
    state.list_chat_ids.return_value = ["42", "7"]
    monkeypatch.setitem(sys.modules, "assistant.services.state", state)
    monkeypatch.setattr(backup, "datetime", FixedNow)
    queries: list[tuple[object, ...]] = []
    docs = {
        "42": [
            {
                "fecha": "2026-09-28", "monto": "-2.00", "moneda": "USD",
                "categoria": "supermercado", "tipo_mov": "gasto", "nota": "pan, leche",
                "batch_id": "g100", "update_id": 101, "tipo": "reverso",
            },
            {
                "fecha": "2026-09-28", "monto": "900.00", "moneda": "USD",
                "fuente": "salario", "tipo_mov": "ingreso", "nota": "",
                "batch_id": "i5", "update_id": 5, "tipo": "registro",
            },
        ],
        "7": [],
    }  # fmt: skip

    def movimientos(chat_id: str, *args: object) -> list[dict]:
        queries.append((chat_id, *args))
        return docs[chat_id]

    monkeypatch.setattr(ledger, "movimientos", movimientos)
    return queries


def test_export_writes_yesterday_csv_in_panama(
    export_env: list[tuple[object, ...]], gcs: FakeBucket
) -> None:
    backup.export_ledger(SETTINGS)
    panama = ZoneInfo("America/Panama")
    desde = datetime(2026, 9, 28, tzinfo=panama)
    assert export_env == [
        ("42", "creado", desde, desde + timedelta(days=1)),
        ("7", "creado", desde, desde + timedelta(days=1)),
    ]
    assert list(gcs.uploads) == ["ledger/mes=2026-09/2026-09-28.csv"]
    lines = gcs.uploads["ledger/mes=2026-09/2026-09-28.csv"].splitlines()
    assert lines == [
        "fecha,chat_id,tipo_mov,categoria,monto,moneda,nota,batch_id,tipo",
        '2026-09-28,42,gasto,supermercado,-2.00,USD,"pan, leche",g100,reverso',
        "2026-09-28,42,ingreso,salario,900.00,USD,,i5,registro",
    ]


def test_export_already_done_is_ok(
    export_env: list[tuple[object, ...]], gcs: FakeBucket
) -> None:
    gcs.uploads["ledger/mes=2026-09/2026-09-28.csv"] = "old"
    backup.export_ledger(SETTINGS)  # PreconditionFailed: treated as done
    assert gcs.uploads["ledger/mes=2026-09/2026-09-28.csv"] == "old"


def test_export_skipped_without_rows(
    monkeypatch: pytest.MonkeyPatch, gcs: FakeBucket
) -> None:
    state = MagicMock()
    state.list_chat_ids.return_value = ["42"]
    monkeypatch.setitem(sys.modules, "assistant.services.state", state)
    monkeypatch.setattr(ledger, "movimientos", lambda *a: [])
    backup.export_ledger(SETTINGS)
    assert not gcs.uploads


def test_budgets_used_by_weekly_is_pure() -> None:
    assert budgets.mayor_exceso({}, None, Decimal(0)) is None
