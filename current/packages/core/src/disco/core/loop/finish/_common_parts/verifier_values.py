"""Bounded verifier-context value shaping — pure, stdlib + imported types only.

Extracted verbatim from `finish/common.py` (module logical LOC reduction).
The verifier may inspect deterministic check evidence, but it never needs an
unbounded page dump or raw tool transcript; these helpers keep the seed small
and explicit at the core boundary.
"""

from __future__ import annotations

from typing import Any

from ...boundaries import VerifierScreenshot

_VERIFIER_CHECK_KEYS = frozenset(
    {
        "passed",
        "verdict",
        "url",
        "http_status",
        "title",
        "document_content_type",
        "meaningful_content",
        "visible_text_chars",
        "elements_count",
        "canvas_count",
        "console_errors",
        "console_warnings",
        "network_failures",
        "checks",
        "browser_unavailable",
        "medium",
        "game_interaction",
        "vision",
        "summary",
        "detail",
        "next_action",
        "failure_fingerprint",
        "screenshot_path",
    }
)
_VERIFIER_MAX_STRING_CHARS = 2000
_VERIFIER_MAX_LIST_ITEMS = 20
_VERIFIER_MAX_DICT_ITEMS = 40


def _bounded_verifier_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-ish, size-bounded value for the verifier context.

    The verifier may inspect deterministic check evidence, but it never needs an
    unbounded page dump or raw tool transcript. This helper keeps the seed small
    and explicit at the core boundary.
    """

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _VERIFIER_MAX_STRING_CHARS:
            return value
        return value[:_VERIFIER_MAX_STRING_CHARS] + "...[truncated]"
    if depth >= 4:
        return str(value)[:_VERIFIER_MAX_STRING_CHARS]
    if isinstance(value, list):
        return [
            _bounded_verifier_value(item, depth=depth + 1)
            for item in value[:_VERIFIER_MAX_LIST_ITEMS]
        ]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _VERIFIER_MAX_DICT_ITEMS:
                break
            if not isinstance(k, str):
                continue
            out[k] = _bounded_verifier_value(v, depth=depth + 1)
        return out
    return str(value)[:_VERIFIER_MAX_STRING_CHARS]


def _bounded_verifier_check_results(verdict: dict[str, Any]) -> dict[str, Any]:
    """The only check-result fields allowed into the verifier model seed."""

    return {
        key: _bounded_verifier_value(value)
        for key, value in verdict.items()
        if key in _VERIFIER_CHECK_KEYS
    }


def _screenshot_from_verdict(verdict: dict[str, Any]) -> VerifierScreenshot:
    image_data_url = str(
        verdict.get("screenshot")
        or verdict.get("screenshot_data_url")
        or verdict.get("screenshot_image")
        or ""
    )
    b64 = verdict.get("screenshot_b64")
    if not image_data_url and isinstance(b64, str) and b64.strip():
        image_data_url = f"data:image/png;base64,{b64.strip()}"
    return VerifierScreenshot(
        path=str(verdict.get("screenshot_path") or ""),
        image_data_url=image_data_url,
    )
