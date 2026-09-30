import logging

import assistant


def test_package_logs_info_and_httpx_stays_quiet() -> None:
    log = logging.getLogger("assistant.worker")
    assert log.getEffectiveLevel() == logging.INFO
    assert logging.getLogger("assistant").handlers
    # httpx logs full request URLs (bot token, secret iCal address).
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert assistant.__version__
