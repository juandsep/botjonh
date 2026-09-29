import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from assistant.context import ToolContext
from assistant.services import state


class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data, self.exists = doc_id, data, data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class Ref:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def get(self, transaction=None):
        return Snap(self.key[1], self.store.get(self.key))

    def set(self, data, merge=False):
        base = self.store.get(self.key, {}) if merge else {}
        self.store[self.key] = {**base, **copy.deepcopy(data)}

    def update(self, data):
        self.store[self.key].update(data)

    def delete(self):
        self.store.pop(self.key, None)


class Tx:
    """Just enough of firestore.Transaction for @firestore.transactional."""

    _read_only, _max_attempts, _id = False, 1, None

    def _clean_up(self): ...
    def _begin(self, retry_id=None): ...
    def _commit(self): ...
    def _rollback(self): ...

    def set(self, ref, data, merge=False):
        ref.set(data, merge)

    def update(self, ref, data):
        ref.update(data)

    def delete(self, ref):
        ref.delete()


class Collection:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def document(self, doc_id):
        return Ref(self.store, (self.name, doc_id))

    def stream(self):
        return [Snap(k[1], v) for k, v in self.store.items() if k[0] == self.name]


class FakeDB:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return Collection(self.store, name)

    def transaction(self):
        return Tx()


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(state, "_db", lambda: fake)
    return fake


def ctx(rol="owner"):
    return ToolContext("1", rol, "USD", "America/Panama", 1, datetime.now(UTC))


def test_users(db) -> None:
    assert state.get_user("1") is None
    state.upsert_user("1", "Ana", rol="owner")
    state.set_last_batch("1", "b1")
    state.upsert_user("1", "Ana", rol="owner", moneda="PAB")
    assert state.get_user("1")["moneda"] == "PAB"
    assert state.last_batch("1") == "b1"
    assert state.last_batch("2") is None
    assert state.list_chat_ids() == ["1"]


def test_mark_processed_once(db) -> None:
    assert state.mark_processed(7) is True
    assert state.mark_processed(7) is False
    assert db.store[("processed", "7")]["expire_at"] > datetime.now(UTC)
    state.unmark_processed(7)
    assert state.mark_processed(7) is True


def test_invite_single_use(db) -> None:
    out = state.invitar_beta(ctx(), "Beto")
    code = out.split("/start ")[1].split(" ")[0]
    assert state.redeem_invite(code, "2") is True
    assert state.get_user("2")["rol"] == "beta"
    assert state.get_user("2")["nombre"] == "Beto"
    assert state.redeem_invite(code, "3") is False
    assert state.get_user("3") is None


def test_invite_expired_or_bogus(db) -> None:
    code = "a" * 22
    db.store[("invites", code)] = {
        "nombre": "x",
        "used": False,
        "expire_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    assert state.redeem_invite(code, "2") is False
    assert state.redeem_invite("../users/1", "2") is False
    assert state.redeem_invite("b" * 22, "2") is False


def test_owner_only_tools(db) -> None:
    assert state.invitar_beta(ctx("beta"), "x") == state.OWNER_ONLY
    assert state.listar_usuarios(ctx("beta")) == state.OWNER_ONLY
    assert not db.store
    state.upsert_user("1", "Ana", rol="owner")
    assert state.listar_usuarios(ctx()) == "Ana (owner)"


def test_rate_limit(db) -> None:
    assert [state.check_rate("1", 2) for _ in range(3)] == [True, True, False]
    assert state.check_rate("2", 2) is True


def test_spend_as_string(db) -> None:
    assert state.llm_spend_today("1") == Decimal("0")
    state.add_llm_spend("1", Decimal("0.01"))
    state.add_llm_spend("1", Decimal("0.02"))
    assert state.llm_spend_today("1") == Decimal("0.03")
    (doc,) = [v for k, v in db.store.items() if k[0] == "spend"]
    assert doc["usd"] == "0.03"


def test_preferences(db) -> None:
    assert state.get_preferences("1") == {}
    db.store[("preferences", "1")] = {"presupuesto": {"salud": "10"}}
    assert state.get_preferences("1")["presupuesto"] == {"salud": "10"}


def test_pending(db) -> None:
    token = state.create_pending("1", {"tool": "deshacer"})
    assert state.pop_pending("2", token) is None  # other chat
    assert state.pop_pending("1", token) == {"tool": "deshacer"}
    assert state.pop_pending("1", token) is None  # single use
    assert state.pop_pending("1", "bad/token") is None


def test_pending_expired(db) -> None:
    token = state.create_pending("1", {"tool": "x"})
    db.store[("pending", token)]["expire_at"] = datetime.now(UTC)
    assert state.pop_pending("1", token) is None
    assert ("pending", token) not in db.store


def test_history_keeps_last_turns(db) -> None:
    assert state.get_history("1") == []
    for i in range(8):
        state.append_history(
            "1",
            [{"role": "user", "content": str(i)}, {"role": "assistant", "content": ""}],
        )
    history = state.get_history("1")
    assert len(history) == 12
    assert history[0]["content"] == "2"
