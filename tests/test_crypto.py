from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from assistant.services import crypto


def test_round_trip_binds_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.encrypt.return_value = SimpleNamespace(ciphertext=b"\x01\x02")
    client.decrypt.return_value = SimpleNamespace(plaintext=b"https://x")
    monkeypatch.setattr(crypto, "_client", lambda: client)
    enc = crypto.encrypt("key", "https://x", "42")
    assert enc == "AQI="
    assert crypto.decrypt("key", enc, "42") == "https://x"
    sent = client.encrypt.call_args.kwargs["request"]
    assert sent["additional_authenticated_data"] == b"42"
    got = client.decrypt.call_args.kwargs["request"]
    assert got["ciphertext"] == b"\x01\x02"
    assert got["additional_authenticated_data"] == b"42"
