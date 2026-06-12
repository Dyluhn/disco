"""Capture structured spans (core.obs) during a test so the harness can assert on
the TRACE — the "monitor the harness while you enact the feature" half of the
testing rule, made programmatic.

    with capture_spans() as spans:
        await run_something()
    assert ends(spans, "agent.step")          # the span fired
    assert tokens_of(spans, "agent.step") > 0 # and carried real fields
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "_fields", None)
        if isinstance(fields, dict):
            self.records.append(dict(fields))


@contextlib.contextmanager
def capture_spans(logger: str = "disco.span") -> Iterator[list[dict[str, Any]]]:
    h = _CaptureHandler()
    lg = logging.getLogger(logger)
    lg.addHandler(h)
    prev = lg.level
    lg.setLevel(logging.INFO)
    try:
        yield h.records
    finally:
        lg.removeHandler(h)
        lg.setLevel(prev)


def ends(spans: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The `end` records for a span name (each carries `ms` + any measured fields)."""
    return [s for s in spans if s.get("span") == name and s.get("event") == "end"]


def sequence(spans: list[dict[str, Any]]) -> list[str]:
    """The ordered list of span names that ended — for asserting the pipeline order."""
    return [s["span"] for s in spans if s.get("event") == "end"]
