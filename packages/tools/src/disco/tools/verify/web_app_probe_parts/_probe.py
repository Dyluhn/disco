"""Probe collection — deterministic diagnostics from a browser structured payload.

This is the reusable "load diagnostics" shape for host-side verification:
console errors/warnings, critical network failures, visible-content counts,
screenshot path, and stable failure fingerprint. The output is immutable
EVIDENCE bound for later host verification; it never manufactures or upgrades a
typed ``HostVerificationResult``.
"""

from __future__ import annotations

from typing import Any

from ._classification import (
    _console_errors,
    _console_warnings,
    _critical_network_failures,
    _dict_list,
)
from ._fingerprint import _failure_fingerprint


def collect_web_app_probe(structured: dict[str, Any] | None) -> dict[str, Any]:
    """Collect deterministic diagnostics from a browser structured payload.

    This is the reusable "load diagnostics" shape for host-side verification:
    console errors/warnings, critical network failures, visible-content counts,
    screenshot path, and stable failure fingerprint.
    """
    s = structured or {}
    console = _dict_list(s.get("console"))
    network = _dict_list(s.get("network"))
    console_errors = _console_errors(console)
    console_warnings = _console_warnings(console)
    network_failures = _critical_network_failures(network)
    title = str(s.get("title", "") or "")
    text = str(s.get("text", "") or "")
    raw_content_type = s.get("document_content_type")
    document_content_type = raw_content_type if isinstance(raw_content_type, str) else ""
    elements = s.get("elements", []) or []
    visible_text_chars = len(text.strip())
    elements_count = len(elements) if isinstance(elements, list) else 0
    canvas_count = s.get("canvas_count")
    canvas_count = canvas_count if isinstance(canvas_count, int) else 0
    screenshot_path = str(s.get("screenshot_path", "") or "")
    # Present ONLY when the browser layer captured it under vision (_vision_mode()).
    # Absent for a no-vision model — keep it absent so the verdict payload stays lean.
    screenshot_b64 = str(s.get("screenshot_b64", "") or "")
    # Host verification consumes the rendered DOM/accessibility text directly.
    # Keep it bounded: this is evidence for exact required-text claims, not a
    # second unbounded page transcript.
    rendered_text = text[:32_768]
    raw_visible_dom_text = s.get("visible_dom_text")
    visible_dom_text = (
        raw_visible_dom_text[:32_768] if isinstance(raw_visible_dom_text, str) else ""
    )
    raw_freshness = s.get("freshness")
    freshness: dict[str, Any] = dict(raw_freshness) if isinstance(raw_freshness, dict) else {}

    return {
        "title": title,
        "text": text,
        "document_content_type": document_content_type,
        "visible_text_chars": visible_text_chars,
        "elements_count": elements_count,
        "canvas_count": canvas_count,
        "console_errors": console_errors,
        "console_warnings": console_warnings,
        "network_failures": network_failures,
        "screenshot_path": screenshot_path,
        "screenshot_b64": screenshot_b64,
        "rendered_text": rendered_text,
        "visible_dom_text": visible_dom_text,
        "freshness": freshness,
        "failure_fingerprint": _failure_fingerprint(console_errors, network_failures),
    }