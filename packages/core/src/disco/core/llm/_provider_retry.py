"""Provider protocol facts used by retry and meaningful-progress policy."""

from __future__ import annotations

import datetime
import json
import math
from collections.abc import Mapping
from email.utils import parsedate_to_datetime

from .errors import LLMTransientError


def attach_retry_after(error: LLMTransientError, headers: Mapping[str, str]) -> None:
    """Retain a bounded Retry-After delay, accepting seconds and HTTP dates."""
    raw = headers.get("retry-after", "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
            seconds = (when - datetime.datetime.now(datetime.UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return
    if math.isfinite(seconds):
        error.retry_after_s = min(300.0, max(0.0, seconds))


def meaningful_sse_line(line: str) -> bool:
    """Keepalives, role headers, empty deltas and usage are not generation."""
    if not line.startswith("data:"):
        return False
    data = line[5:].strip()
    if data == "[DONE]":
        return True
    try:
        frame = json.loads(data)
    except (ValueError, TypeError):
        return False
    if not isinstance(frame, dict):
        return False
    for choice in frame.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason"):
            return True
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        if delta.get("content") or delta.get("reasoning_content"):
            return True
        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict) and (call.get("function") or {}).get("arguments"):
                return True
    return False
