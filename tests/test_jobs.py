import dataclasses
import json
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import assistant.jobs as jobs
from assistant.config import get_worker_settings
from assistant.jobs import backup
from assistant.services import budgets, calendar, sheets


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
    monkeypatch.setattr(sheets, "gastos_por_categoria", lambda *a: {})
    monkeypatch.setattr(sheets, "total_ingresos", lambda *a: Decimal("0.00"))
    run_backup = MagicMock()
    monkeypatch.setattr(backup, "run", run_backup)
    return SimpleNamespace(state=state, telegram=telegram, backup=run_backup)


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
        sheets, "gastos_por_categoria", lambda *a: {"otros": Decimal("4.50")}
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
    monkeypatch.setattr(sheets, "total_ingresos", lambda *a: Decimal("5.00"))
    jobs.run_job("checkin")
    monkeypatch.setattr(sheets, "gastos_por_categoria", lambda *a: {"otros": 1})
    jobs.run_job("checkin")
    assert env.telegram.send_message.call_count == 1


def test_weekly_backs_up_and_summarizes(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.run_job("weekly")
    env.backup.assert_called_once()
    env.telegram.send_message.assert_not_called()  # no spend, nothing to say
    monkeypatch.setattr(
        sheets, "gastos_por_categoria", lambda *a: {"restaurantes": Decimal("80.00")}
    )
    jobs.run_job("weekly")
    env.telegram.send_message.assert_called_with("42", "Semana: 80.00 USD.")
    monkeypatch.setattr(sheets, "total_ingresos", lambda *a: Decimal("1000.00"))
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


def test_backup_writes_json_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = SimpleNamespace(id="42", to_dict=lambda: {"nombre": "J", "n": Decimal(1)})
    db = MagicMock()
    db.collection.return_value.stream.return_value = [doc]
    monkeypatch.setattr(backup.firestore, "Client", lambda project: db)
    uploads: dict[str, str] = {}
    bucket = MagicMock()
    bucket.blob.side_effect = lambda name: SimpleNamespace(
        upload_from_string=lambda data, content_type: uploads.__setitem__(name, data)
    )
    gcs = MagicMock()
    gcs.bucket.return_value = bucket
    monkeypatch.setattr(backup.storage, "Client", lambda project: gcs)
    monkeypatch.setattr(sheets, "valores", lambda hoja: [["fecha"], ["2026-09-29"]])
    settings = dataclasses.replace(get_worker_settings(), backup_bucket="b")
    backup.run(settings)
    gcs.bucket.assert_called_once_with("b")
    names = sorted(n.split("/", 1)[1] for n in uploads)
    assert names == [
        "firestore/invites.json",
        "firestore/pending.json",
        "firestore/preferences.json",
        "firestore/users.json",
        "sheets/Gastos.json",
        "sheets/Ingresos.json",
    ]
    prefix = next(iter(uploads)).split("/")[0]
    assert len(prefix) == 10 and prefix[4] == "-"  # YYYY-MM-DD
    users = json.loads(next(v for k, v in uploads.items() if k.endswith("users.json")))
    assert users == {"42": {"nombre": "J", "n": "1"}}
    assert json.loads(uploads[f"{prefix}/sheets/Gastos.json"])[1] == ["2026-09-29"]


def test_backup_skipped_without_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(backup.firestore, "Client", client)
    backup.run(dataclasses.replace(get_worker_settings(), backup_bucket=""))
    client.assert_not_called()


def test_budgets_used_by_weekly_is_pure() -> None:
    assert budgets.mayor_exceso({}, None, Decimal(0)) is None
