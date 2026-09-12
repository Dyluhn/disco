"""Both servers configure root logging from the same two variables."""

from __future__ import annotations

import logging

from disco.core.obs import JsonFormatter, configure_logging


def test_plain_logging_sets_level_and_format() -> None:
    configure_logging("debug", False)
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert root.handlers and not isinstance(root.handlers[0].formatter, JsonFormatter)


def test_json_logging_installs_the_json_formatter() -> None:
    configure_logging("warning", True)
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    configure_logging("INFO", False)  # leave the process as the other tests expect


def test_missing_level_defaults_to_info() -> None:
    configure_logging(None, False)
    assert logging.getLogger().level == logging.INFO
