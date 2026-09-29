"""Weekly backup to ``gs://$BACKUP_BUCKET/YYYY-MM-DD/``.

Firestore collections and the ledger sheet values (read through the Sheets API)
become JSON objects. Never Drive ``files.copy``: service accounts have no Drive
quota.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import google.cloud.storage as storage
from google.cloud import firestore

from assistant.config import WorkerSettings
from assistant.services import sheets

log = logging.getLogger(__name__)

COLLECTIONS = ("users", "preferences", "invites", "pending")


def run(settings: WorkerSettings) -> None:
    if not settings.backup_bucket:
        log.warning("backup_skipped reason=no_bucket")
        return
    dia = datetime.now(ZoneInfo(settings.default_timezone)).date().isoformat()
    db = firestore.Client(project=settings.project_id)
    objetos: dict[str, Any] = {
        f"firestore/{c}.json": {d.id: d.to_dict() for d in db.collection(c).stream()}
        for c in COLLECTIONS
    }
    for hoja in (sheets.GASTOS, sheets.INGRESOS):
        objetos[f"sheets/{hoja}.json"] = sheets.valores(hoja)
    bucket = storage.Client(project=settings.project_id).bucket(settings.backup_bucket)
    for nombre, data in objetos.items():
        bucket.blob(f"{dia}/{nombre}").upload_from_string(
            json.dumps(data, default=str, ensure_ascii=False),
            content_type="application/json",
        )
    log.info("backup_done objects=%d", len(objetos))
