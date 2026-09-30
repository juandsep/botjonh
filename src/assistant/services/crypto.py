"""Application-level encryption with Cloud KMS for per-user secrets.

The chat id is the associated data, so a ciphertext copied to another user's
document does not decrypt. Firestore, its console and the GCS backups only
ever hold ciphertext.
"""

from __future__ import annotations

import base64
from functools import cache

from google.cloud import kms


@cache
def _client() -> kms.KeyManagementServiceClient:
    return kms.KeyManagementServiceClient()


def encrypt(key: str, plaintext: str, chat_id: str) -> str:
    resp = _client().encrypt(
        request={
            "name": key,
            "plaintext": plaintext.encode(),
            "additional_authenticated_data": chat_id.encode(),
        }
    )
    return base64.b64encode(resp.ciphertext).decode()


def decrypt(key: str, ciphertext: str, chat_id: str) -> str:
    resp = _client().decrypt(
        request={
            "name": key,
            "ciphertext": base64.b64decode(ciphertext),
            "additional_authenticated_data": chat_id.encode(),
        }
    )
    return bytes(resp.plaintext).decode()
