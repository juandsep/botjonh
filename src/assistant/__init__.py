"""Personal assistant bot package."""

import logging
import sys

__version__ = "0.1.0"

# Cloud Run ships stdout to Cloud Logging. Without a handler Python drops every
# INFO record, including the per-turn trace line. Only this package logs at
# INFO: httpx logs request URLs, which carry the bot token and the secret iCal
# address, so it stays at WARNING.
_log = logging.getLogger("assistant")
if not _log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
