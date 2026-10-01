from unittest.mock import MagicMock

from assistant import admin
from assistant.services import state


def test_add_owner(monkeypatch) -> None:
    upsert = MagicMock()
    monkeypatch.setattr(state, "upsert_user", upsert)
    admin.main(["add-owner", "42", "Ana"])
    upsert.assert_called_once_with("42", "Ana", rol="owner")


def test_migrate_gifs(monkeypatch, capsys) -> None:
    migrate = MagicMock(return_value=3)
    monkeypatch.setattr(state, "migrate_gifs", migrate)
    admin.main(["migrate-gifs", "42"])
    migrate.assert_called_once_with("42")
    assert capsys.readouterr().out == "3 gifs migrated\n"
