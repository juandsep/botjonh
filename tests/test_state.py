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


def test_ics_token_created_reused_and_rotated(db) -> None:
    assert state.chat_for_ics_token("x" * 32) is None
    token = state.ics_token("1")
    assert len(token) == 32 and state.ics_token("1") == token
    assert state.chat_for_ics_token(token) == "1"
    nuevo = state.ics_token("1", rotate=True)
    assert nuevo != token
    assert state.chat_for_ics_token(token) is None  # old link revoked
    assert state.chat_for_ics_token(nuevo) == "1"
    assert state.get_user("1")["ics_token"] == nuevo


def test_ics_token_format_checked_before_lookup(monkeypatch) -> None:
    def boom():
        raise AssertionError("no Firestore lookup")

    monkeypatch.setattr(state, "_db", boom)
    for bad in ("", "short", "a" * 31 + "/", "../users/1" + "a" * 22, "a" * 33):
        assert state.chat_for_ics_token(bad) is None


def test_gif_catalog_dedupe_cap_and_fallback(db) -> None:
    assert state.random_gif("gasto", "restaurantes") is None
    for i in range(25):
        state.add_gif("gasto", "restaurantes", f"f{i}")
    state.add_gif("gasto", "restaurantes", "f10")  # repeated: moves to the end
    state.add_gif("gasto", "general", "g0")
    state.add_gif("ingreso", "general", "i0")
    ids = state.gif_catalog("gasto")["restaurantes"]
    assert len(ids) == 20 and ids[-1] == "f10" and ids.count("f10") == 1
    assert state.gif_catalog("gasto")["general"] == ["g0"]
    assert state.random_gif("gasto", "restaurantes") in ids
    assert state.random_gif("gasto", "salud") == "g0"  # no GIFs: general
    assert state.random_gif("gasto", "") == "g0"
    assert state.random_gif("ingreso", "salario") == "i0"


def test_remove_gif_everywhere(db) -> None:
    state.add_gif("gasto", "general", "x")
    state.add_gif("gasto", "salud", "x")
    state.add_gif("gasto", "salud", "y")
    state.add_gif("ingreso", "salario", "x")
    assert state.remove_gif("x") == 3
    assert state.gif_catalog("gasto") == {"salud": ["y"]}
    assert state.gif_catalog("ingreso") == {}
    assert state.remove_gif("x") == 0


def test_migrate_gifs_into_general(db) -> None:
    db.store[("gifs", "1")] = {"gasto": ["a", "b"], "ingreso": ["c"]}
    state.add_gif("gasto", "general", "a")
    assert state.migrate_gifs("1") == 3
    assert state.gif_catalog("gasto") == {"general": ["a", "b"]}
    assert state.gif_catalog("ingreso") == {"general": ["c"]}
    assert state.migrate_gifs("2") == 0


def test_valid_clave() -> None:
    assert state.valid_clave("ñandú") and state.valid_clave("x_1")
    assert not state.valid_clave("Comida") and not state.valid_clave("a b")


def test_revocar_only_betas(db) -> None:
    db.store[("users", "1")] = {"nombre": "Yo", "rol": "owner"}
    db.store[("users", "2")] = {"nombre": "Ana", "rol": "beta"}
    assert state.revocar("1") is False
    assert state.revocar("3") is False
    assert state.revocar("2") is True
    assert state.get_user("2") is None and state.get_user("1") is not None


def test_dash_token(db) -> None:
    token = state.dash_token("1")
    assert len(token) == 32
    doc = db.store[("dash", token)]
    assert doc["chat_id"] == "1"
    assert doc["expire_at"] - datetime.now(UTC) > timedelta(minutes=59)
    assert state.chat_for_dash_token(token) == "1"
    assert state.dash_token("1") != token  # every link is new
    long = state.dash_token("1", ttl=timedelta(hours=24))
    assert db.store[("dash", long)]["expire_at"] - datetime.now(UTC) > timedelta(
        hours=23
    )


def test_dash_token_expired_or_bogus(db) -> None:
    token = state.dash_token("1")
    db.store[("dash", token)]["expire_at"] = datetime.now(UTC)
    assert state.chat_for_dash_token(token) is None
    assert state.chat_for_dash_token("u" * 32) is None
    assert state.chat_for_dash_token("../users/1") is None
