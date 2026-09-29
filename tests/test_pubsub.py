from unittest.mock import MagicMock

from assistant.services import pubsub


def test_publish_json(monkeypatch) -> None:
    client = MagicMock()
    client.topic_path.return_value = "projects/test-project/topics/t"
    monkeypatch.setattr(pubsub, "PublisherClient", lambda: client)
    pubsub._publisher.cache_clear()
    pubsub.publish("t", {"update_id": 1})
    pubsub._publisher.cache_clear()
    client.topic_path.assert_called_once_with("test-project", "t")
    client.publish.assert_called_once_with(
        "projects/test-project/topics/t", b'{"update_id": 1}'
    )
    client.publish.return_value.result.assert_called_once()
