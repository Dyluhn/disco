"""Lightweight structured observability — the substrate for trace-based assertions
(the "monitor the harness" half of the testing rule) and clean prod logs.

No OpenTelemetry, no collector: just stdlib logging with a JSON formatter and a
`log_span()` context manager that emits start/end records carrying structured
fields (conversation_id / request_id / role / model / tokens / ms). Tests capture
these via `harness.logcap` and assert on the span sequence; in prod they're
greppable JSON.

Install the JSON formatter with `install_json_logging()` (the agent-server calls
it when PMX_LOG_JSON=1); otherwise spans still emit as ordinary log lines.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Iterator
from typing import Any

_LOG = logging.getLogger("perpleximanus.span")


class JsonFormatter(logging.Formatter):
    """One JSON object per log record; structured fields ride on `record._fields`."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "_fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        return json.dumps(out, default=str, ensure_ascii=False)


def install_json_logging(level: str = "INFO") -> None:
    """Replace the root handler with one that emits JSON. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def log_event(name: str, **fields: Any) -> None:
    """Emit a single structured event (a point-in-time span)."""
    _LOG.info(name, extra={"_fields": {"span": name, "event": "point", **fields}})


@contextlib.contextmanager
def log_span(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a span; emit `start` then `end` (with duration_ms) structured records.
    Yields a mutable dict so the body can attach measured fields (e.g. token counts)
    that land on the `end` record:

        with log_span("agent.step", role=role) as span:
            resp = await call()
            span["tokens"] = resp.usage.output_tokens
    """
    extra = dict(fields)
    _LOG.info(name, extra={"_fields": {"span": name, "event": "start", **extra}})
    start = time.monotonic()
    measured: dict[str, Any] = {}
    try:
        yield measured
    finally:
        ms = round((time.monotonic() - start) * 1000)
        _LOG.info(
            name,
            extra={"_fields": {"span": name, "event": "end", "ms": ms, **extra, **measured}},
        )
