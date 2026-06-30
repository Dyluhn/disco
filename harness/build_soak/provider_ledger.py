"""HARN-1a — the provider-call ledger evidence.

A provider-call-ledger is the per-run record of every LLM provider request the build
made: the destination host, the model, and whether the call fired after the
conversation went terminal. It is the evidence the ProviderLedgerOracle judges to
enforce the soak constraint (MiniMax-only / no-OpenRouter / zero-calls-after-terminal).

The source of truth for a HEADLESS run is the MiniMax relay log (the relay sits between
disco and the provider and logs each upstream request). This module normalizes relay-log
lines into ledger records. Records may also be supplied directly as JSONL.

A ledger record (dict):
    {"ts": str|None, "host": str, "model": str, "after_terminal": bool}
Only ``host`` is strictly required for enforcement; ``model``/``ts``/``after_terminal``
default to "" / "" / False when the source line does not carry them.
"""

from __future__ import annotations

import json
import re
from typing import Any

# A URL host: grab the authority of the first http(s):// URL on the line.
_URL_HOST_RE = re.compile(r"https?://([^/\s\"']+)", re.IGNORECASE)
# A model id: "model":"X" (JSON), model=X, or model: X.
_MODEL_RE = re.compile(r"""["']?model["']?\s*[:=]\s*["']?([A-Za-z0-9._\-]+)""", re.IGNORECASE)


def _record(
    host: str,
    *,
    model: str = "",
    ts: str | None = None,
    after_terminal: bool = False,
    has_tools: bool = True,
) -> dict[str, Any]:
    # [REL-5b] has_tools defaults True (fail-closed): an UNMARKED record (older relay, or a loose
    # log line) is treated as a build-driver call so the after-terminal runaway check never silently
    # under-counts. A tool-less SUMMARIZER/title call is excluded only when the relay explicitly
    # marks has_tools=False.
    return {
        "ts": ts,
        "host": host,
        "model": model,
        "after_terminal": bool(after_terminal),
        "has_tools": bool(has_tools),
    }


def parse_relay_log_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Normalize relay-log lines into ledger records (best-effort, tolerant).

    Each line that is valid JSON carrying a ``host``/``url`` (and optional ``model``)
    is used directly; otherwise a host is regex-extracted from the first URL on the
    line and a model from a ``model``-like token. Lines with no recoverable host are
    skipped (not every relay line is a provider request)."""
    out: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        # 1) structured JSONL relay record
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            host = str(obj.get("host") or "")
            if not host:
                url = str(obj.get("url") or "")
                m = _URL_HOST_RE.search(url)
                host = m.group(1) if m else ""
            if host:
                out.append(
                    _record(
                        host,
                        model=str(obj.get("model") or ""),
                        ts=obj.get("ts") or obj.get("timestamp"),
                        after_terminal=bool(obj.get("after_terminal", False)),
                        has_tools=bool(obj.get("has_tools", True)),  # [REL-5b] default True=fail-closed
                    )
                )
            continue
        # 2) loose log line — extract host + model by regex
        mh = _URL_HOST_RE.search(line)
        if not mh:
            continue
        mm = _MODEL_RE.search(line)
        out.append(_record(mh.group(1), model=mm.group(1) if mm else ""))
    return out


def parse_relay_log(text: str) -> list[dict[str, Any]]:
    """Convenience: parse a whole relay-log blob into ledger records."""
    return parse_relay_log_lines(text.splitlines())
