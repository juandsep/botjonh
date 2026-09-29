# Minimal smoke test: the two apps import and expose a health route.

from fastapi.testclient import TestClient

from assistant import api, worker


def test_api_health() -> None:
    client = TestClient(api.app)
    assert client.get("/health").status_code == 200


def test_worker_health() -> None:
    client = TestClient(worker.app)
    assert client.get("/health").status_code == 200


def test_webhook_rejects_missing_secret() -> None:
    client = TestClient(api.app)
    assert client.post("/tg/whatever").status_code == 403
