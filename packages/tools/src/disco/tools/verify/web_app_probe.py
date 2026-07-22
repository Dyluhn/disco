"""Pure helpers for the ``verify_web_app`` structured verdict.

The tool drives the browser/sandbox. This module only classifies the structured
probe payload into deterministic diagnostics and a verdict, so server-side host
gates can reuse the same judgement without importing the tool implementation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Network failures that are NEVER load-bearing for "does the app work" — a missing
# favicon or a blocked analytics beacon must not fail an otherwise-good build.
_IGNORABLE_NETWORK = (
    "favicon",
    "/analytics",
    "google-analytics",
    "googletagmanager",
    "gtag/js",
    "/__vite_ping",
    "hot-update.json",
    "/sockjs-node",
)

# Advisory visual self-review checklist. Seeded into ``verdict["vision"]["notes"]``
# ONLY when a screenshot_b64 is present (vision active); the model applies it to the
# rendered screenshot. Advisory — it never gates the finish decision.
_VISION_REVIEW_CHECKLIST = (
    "Visual self-review (advisory — does not gate finish). Looking at the rendered\n"
    "screenshot, check ONLY for real problems and fix what is clearly off:\n"
    "- padding / alignment / spacing consistency (no cramped or colliding elements)\n"
    "- text contrast against its background (no low-contrast or invisible text)\n"
    "- visual hierarchy (headings, sections, and CTAs are distinguishable)\n"
    "- no overflow, overlap, or broken layout; footer and nav render correctly\n"
    "Only flag what is incorrect or off. Do not invent issues or restyle a page that\n"
    "already looks correct."
)


def _source_of(entry: dict[str, Any]) -> str:
    """Render a console entry's location dict into a ``url:line:col`` string."""
    loc = entry.get("location") or {}
    url = loc.get("url")
    if not url:
        return ""
    line = loc.get("lineNumber")
    if line is None:
        return str(url)
    col = loc.get("columnNumber")
    return f"{url}:{line}:{col}" if col is not None else f"{url}:{line}"


def _console_errors(console: list[dict[str, Any]]) -> list[dict[str, str]]:
    """error-level / uncaught console entries only (warnings handled separately)."""
    out: list[dict[str, str]] = []
    for c in console:
        if c.get("level") == "error":
            out.append(
                {
                    "text": str(c.get("text", "")),
                    "source": _source_of(c),
                    "stack": str(c.get("stack", "") or ""),
                }
            )
    return out


def _console_warnings(console: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for c in console:
        if c.get("level") == "warning":
            out.append({"text": str(c.get("text", "")), "source": _source_of(c)})
    return out


def _is_ignorable_network(entry: dict[str, Any]) -> bool:
    url = str(entry.get("url", "")).lower()
    return any(token in url for token in _IGNORABLE_NETWORK)


def _critical_network_failures(network: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """4xx/5xx responses + hard request failures, minus the favicon/analytics
    allowlist. These are the network failures that mean the app is broken."""
    out: list[dict[str, Any]] = []
    for n in network:
        if _is_ignorable_network(n):
            continue
        status = n.get("status")
        failure = n.get("failure")
        is_http_error = isinstance(status, int) and not isinstance(status, bool) and status >= 400
        if failure or is_http_error:
            out.append(
                {
                    "method": str(n.get("method", "GET")),
                    "url": str(n.get("url", "")),
                    "status": status if isinstance(status, int) else None,
                    "failure": str(failure) if failure else None,
                }
            )
    return out


def _normalize_error_text(text: str) -> str:
    """Stabilize an error signature so the SAME bug fingerprints identically across
    reloads: lowercase, collapse whitespace, and strip volatile numerics (line/col
    offsets, hex addresses, hashed asset names) that differ run-to-run."""
    t = text.strip().lower()
    t = re.sub(r"0x[0-9a-f]+", "0x", t)  # hex addresses
    t = re.sub(r"[0-9a-f]{8,}", "", t)  # content hashes (asset fingerprints)
    t = re.sub(r"\d+", "", t)  # line/col numbers, counts
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _path_of(url: str) -> str:
    """Path component of a URL (drop scheme/host/query) for a stable network sig."""
    u = re.sub(r"^[a-z]+://[^/]+", "", str(url))
    return u.split("?", 1)[0].split("#", 1)[0]


def _failure_fingerprint(
    console_errors: list[dict[str, str]], network_failures: list[dict[str, Any]]
) -> str:
    """Stable 16-hex hash of the top console-error signatures + critical network
    failures. Identical failures → identical fingerprint (the loop-breaker key);
    a different bug → different fingerprint. Empty inputs → a fixed 'clean' hash
    (never collides with a real failure set)."""
    sigs: list[str] = []
    for e in console_errors[:5]:
        sigs.append("E:" + _normalize_error_text(e["text"]))
    for n in network_failures[:5]:
        marker = str(n.get("status") or n.get("failure") or "fail")
        sigs.append(f"N:{marker}:{_path_of(n.get('url', ''))}")
    raw = "|".join(sorted(sigs)) if sigs else "CLEAN"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


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
    raw_freshness = s.get("freshness")
    freshness: dict[str, Any] = dict(raw_freshness) if isinstance(raw_freshness, dict) else {}

    return {
        "title": title,
        "text": text,
        "visible_text_chars": visible_text_chars,
        "elements_count": elements_count,
        "canvas_count": canvas_count,
        "console_errors": console_errors,
        "console_warnings": console_warnings,
        "network_failures": network_failures,
        "screenshot_path": screenshot_path,
        "screenshot_b64": screenshot_b64,
        "rendered_text": rendered_text,
        "freshness": freshness,
        "failure_fingerprint": _failure_fingerprint(console_errors, network_failures),
    }


def compute_verdict(
    *,
    url: str,
    reachable: bool,
    http_status: int,
    structured: dict[str, Any] | None,
    meaningful: bool,
) -> dict[str, Any]:
    """Pure verdict builder — deterministic checks decide pass/fail/degraded.

    Order of judgement (first failing gate wins the verdict):
      1. server reachable + HTTP 2xx/3xx           → else FAIL (not serving)
      2. no console errors / critical network fails → else FAIL (runtime broken)
      3. meaningful (non-blank) render              → else DEGRADED (mounted nothing)
      4. all pass                                   → PASS

    Vision is ADVISORY only and never runs here in v1 (it is escalation for
    layout/blank-canvas judgement); the field is reported as unused so a normal
    pass never depends on it.
    """
    probe = collect_web_app_probe(structured)
    title = str(probe["title"])
    visible_text_chars = int(probe["visible_text_chars"])
    elements_count = int(probe["elements_count"])
    canvas_count = int(probe["canvas_count"])
    console_errors = probe["console_errors"]
    console_warnings = probe["console_warnings"]
    network_failures = probe["network_failures"]
    screenshot_path = str(probe["screenshot_path"])
    screenshot_b64 = str(probe["screenshot_b64"])
    rendered_text = str(probe["rendered_text"])
    freshness = dict(probe["freshness"])
    fingerprint = str(probe["failure_fingerprint"])

    http_ok = isinstance(http_status, int) and 200 <= http_status < 400

    if not reachable or not http_ok:
        passed, verdict = False, "fail"
        shown = http_status if reachable else "no response"
        summary = f"App not serving: {url} returned {shown}."
        next_action = (
            "Serve through the PLATFORM preview, not your own server: call `preview_start` "
            "(serve_dir for static files, or framework/command). The platform picks the "
            "port, serves, supervises, and health-verifies it — a 'running' status MEANS it "
            "is serving (HTTP 200, probed in-sandbox). Do NOT install or run your own server "
            "(`python -m http.server`, `http-server`, a framework dev command on a port you "
            "pick, etc.) — a model-run server collides with the platform preview on a "
            "different port and wedges the 'preview' session. If you already called "
            "preview_start, just re-check `preview_status` (give it a moment to come up); "
            "never re-serve manually."
        )
    elif console_errors or network_failures:
        passed, verdict = False, "fail"
        if console_errors:
            first = console_errors[0]
            where = f" @ {first['source']}" if first["source"] else ""
            summary = (
                f"App loaded (HTTP {http_status}) but threw a console error: {first['text']}{where}"
            )
            next_action = f"Fix the console error: {first['text']}"
        else:
            nf = network_failures[0]
            marker = nf.get("status") or nf.get("failure") or "failed"
            summary = (
                f"App loaded (HTTP {http_status}) but a request failed: "
                f"{nf['method']} {nf['url']} -> {marker}"
            )
            next_action = (
                f"Fix the failing request {nf['method']} {nf['url']} ({marker}) — "
                "the endpoint/asset is missing or erroring."
            )
    elif not meaningful:
        passed, verdict = False, "degraded"
        summary = (
            f"App served HTTP {http_status} with a clean console but rendered no visible "
            f"content (blank page — {visible_text_chars} text chars, {elements_count} elements)."
        )
        next_action = (
            "The page mounts nothing a user can see — check that the app renders into the "
            "DOM (an SPA may be failing to hydrate, or the root element is empty)."
        )
    else:
        passed, verdict = True, "pass"
        summary = (
            f"{url} served HTTP {http_status} with {visible_text_chars} chars of visible "
            f"content, {elements_count} interactive elements, and no console/network errors."
        )
        next_action = ""

    result: dict[str, Any] = {
        "passed": passed,
        "verdict": verdict,
        "url": url,
        "http_status": http_status,
        "title": title,
        "meaningful_content": meaningful,
        "visible_text_chars": visible_text_chars,
        "elements_count": elements_count,
        "canvas_count": canvas_count,
        "console_errors": console_errors,
        "console_warnings": console_warnings,
        "network_failures": network_failures,
        "screenshot_path": screenshot_path,
        "rendered_text": rendered_text,
        "freshness": freshness,
        "vision": {"used": False, "passed": None, "notes": []},
        "failure_fingerprint": fingerprint,
        "summary": summary,
        "next_action": next_action,
    }
    # ADVISORY visual self-review — active ONLY when the browser layer captured a
    # screenshot under vision (_vision_mode()). `passed`/`verdict` above are already
    # decided; this block never touches them, so it can never gate the finish
    # decision. When vision is off, screenshot_b64 is "" → nothing is added and the
    # verdict is byte-identical to today's (no base64 bloat, no vision.used flip).
    if screenshot_b64:
        result["screenshot_b64"] = screenshot_b64
        result["vision"] = {
            "used": True,
            "passed": None,  # advisory only — not a pass/fail signal
            "notes": [_VISION_REVIEW_CHECKLIST],
        }
    return result


__all__ = [
    "collect_web_app_probe",
    "compute_verdict",
]
