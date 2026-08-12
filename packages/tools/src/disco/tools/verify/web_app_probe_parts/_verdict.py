"""Pure verdict builder for the web-app probe evidence.

Deterministic checks decide pass/fail/degraded from the collected probe
diagnostics. This is an EVIDENCE-TO-VERDICT classifier only: it returns a plain
``dict`` the finish gate consumes. It never produces or upgrades a typed
``HostVerificationResult`` — that typed receipt is host authority, owned
elsewhere (``disco.core.verification``), and a target probe must not manufacture
one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._classification import _VISION_REVIEW_CHECKLIST
from ._probe import collect_web_app_probe


@dataclass(frozen=True)
class _VerdictDecision:
    passed: bool
    verdict: str
    summary: str
    next_action: str


def _decide(
    *,
    url: str,
    reachable: bool,
    http_status: int,
    meaningful: bool,
    probe: dict[str, Any],
) -> _VerdictDecision:
    http_ok = isinstance(http_status, int) and 200 <= http_status < 400
    if not reachable or not http_ok:
        shown = http_status if reachable else "no response"
        return _VerdictDecision(
            passed=False,
            verdict="fail",
            summary=f"App not serving: {url} returned {shown}.",
            next_action=(
                "Serve through the PLATFORM preview, not your own server: call "
                "`preview_start` (serve_dir for static files, or framework/command). "
                "The platform picks the port, serves, supervises, and health-verifies "
                "it — a 'running' status MEANS it is serving (HTTP 200, probed "
                "in-sandbox). Do NOT install or run your own server (`python -m "
                "http.server`, `http-server`, a framework dev command on a port you "
                "pick, etc.) — a model-run server collides with the platform preview "
                "on a different port and wedges the 'preview' session. If you already "
                "called preview_start, just re-check `preview_status` (give it a "
                "moment to come up); never re-serve manually."
            ),
        )
    console_errors = probe["console_errors"]
    network_failures = probe["network_failures"]
    if console_errors:
        first = console_errors[0]
        where = f" @ {first['source']}" if first["source"] else ""
        return _VerdictDecision(
            passed=False,
            verdict="fail",
            summary=(
                f"App loaded (HTTP {http_status}) but threw a console error: "
                f"{first['text']}{where}"
            ),
            next_action=f"Fix the console error: {first['text']}",
        )
    if network_failures:
        failure = network_failures[0]
        marker = failure.get("status") or failure.get("failure") or "failed"
        return _VerdictDecision(
            passed=False,
            verdict="fail",
            summary=(
                f"App loaded (HTTP {http_status}) but a request failed: "
                f"{failure['method']} {failure['url']} -> {marker}"
            ),
            next_action=(
                f"Fix the failing request {failure['method']} {failure['url']} "
                f"({marker}) — the endpoint/asset is missing or erroring."
            ),
        )
    if not meaningful:
        return _VerdictDecision(
            passed=False,
            verdict="degraded",
            summary=(
                f"App served HTTP {http_status} with a clean console but rendered no "
                "visible content (blank page — "
                f"{probe['visible_text_chars']} text chars, "
                f"{probe['elements_count']} elements)."
            ),
            next_action=(
                "The page mounts nothing a user can see — check that the app renders "
                "into the DOM (an SPA may be failing to hydrate, or the root element "
                "is empty)."
            ),
        )
    return _VerdictDecision(
        passed=True,
        verdict="pass",
        summary=(
            f"{url} served HTTP {http_status} with {probe['visible_text_chars']} chars "
            f"of visible content, {probe['elements_count']} interactive elements, and "
            "no console/network errors."
        ),
        next_action=(
            "Surface/runtime health passed and is recorded for this URL; semantic "
            "completeness is outside this probe's scope. Move to the remaining plan "
            "steps, or finish if none remain. Re-run only after a material change."
        ),
    )


def _result(
    *,
    url: str,
    http_status: int,
    meaningful: bool,
    probe: dict[str, Any],
    decision: _VerdictDecision,
) -> dict[str, Any]:
    return {
        "passed": decision.passed,
        "verdict": decision.verdict,
        "url": url,
        "http_status": http_status,
        "title": str(probe["title"]),
        "document_content_type": str(probe["document_content_type"]),
        "meaningful_content": meaningful,
        "visible_text_chars": int(probe["visible_text_chars"]),
        "elements_count": int(probe["elements_count"]),
        "canvas_count": int(probe["canvas_count"]),
        "console_errors": probe["console_errors"],
        "console_warnings": probe["console_warnings"],
        "network_failures": probe["network_failures"],
        "screenshot_path": str(probe["screenshot_path"]),
        "rendered_text": str(probe["rendered_text"]),
        "visible_dom_text": str(probe["visible_dom_text"]),
        "freshness": dict(probe["freshness"]),
        "vision": {"used": False, "passed": None, "notes": []},
        "failure_fingerprint": str(probe["failure_fingerprint"]),
        "summary": decision.summary,
        "next_action": decision.next_action,
    }


def _attach_vision(result: dict[str, Any], screenshot_b64: str) -> None:
    if not screenshot_b64:
        return
    result["screenshot_b64"] = screenshot_b64
    result["vision"] = {
        "used": True,
        "passed": None,
        "notes": [_VISION_REVIEW_CHECKLIST],
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
    screenshot_b64 = str(probe["screenshot_b64"])
    decision = _decide(
        url=url,
        reachable=reachable,
        http_status=http_status,
        meaningful=meaningful,
        probe=probe,
    )
    result = _result(
        url=url,
        http_status=http_status,
        meaningful=meaningful,
        probe=probe,
        decision=decision,
    )
    _attach_vision(result, screenshot_b64)
    return result
