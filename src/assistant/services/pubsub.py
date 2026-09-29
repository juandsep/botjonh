"""Pub/Sub publisher. The client is created lazily, never at import."""

from __future__ import annotations

import json
import os
from functools import cache

from google.cloud.pubsub_v1 import PublisherClient


@cache
def _publisher() -> PublisherClient:
    return PublisherClient()


def publish(topic: str, data: dict) -> None:
    """Publish ``data`` as JSON and wait for the ack, so a failure is visible."""
    client = _publisher()
    path = client.topic_path(os.environ["GCP_PROJECT_ID"], topic)
    client.publish(path, json.dumps(data).encode()).result(timeout=10)
