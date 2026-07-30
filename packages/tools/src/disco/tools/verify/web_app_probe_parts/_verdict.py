"""Pure verdict builder for the web-app probe evidence.

Deterministic checks decide pass/fail/degraded from the collected probe
diagnostics. This is an EVIDENCE-TO-VERDICT classifier only: it returns a plain
``dict`` the finish gate consumes. It never produces or upgrades a typed
``HostVerificationResult`` — that typed receipt is host authority, owned
elsewhere (``disco.core.verification``), and a target probe must not manufacture
one.
"""

from __future__ import annotations

from typing import Any

from ._classification import _VISION_REVIEW_CHECKLIST
from ._probe import collect_web_app_probe


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
    document_content_type = str(probe["document_content_type"])
    visible_text_chars = int(probe["visible_text_chars"])
    elements_count = int(probe["elements_count"])
    canvas_count = int(probe["canvas_count"])
    console_errors = probe["console_errors"]
    console_warnings = probe["console_warnings"]
    network_failures = probe["network_failures"]
    screenshot_path = str(probe["screenshot_path"])
    screenshot_b64 = str(probe["screenshot_b64"])
    rendered_text = str(probe["rendered_text"])
    visible_dom_text = str(probe["visible_dom_text"])
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
        # Every FAILING branch above tells the agent what to do next. The passing
        # branch used to say nothing at all, which is the one case where silence
        # is most expensive: the agent has just proven the thing it was asked to
        # prove and has no signal that the proof is durable.
        #
        # Counted-promotion evidence 2026-07-27 (p4_ff_react_continue seeds
        # 400006 / 400023): verification PASSED at action #16 with
        # `next_action: ""`, and 33-35 further actions followed — five rebuilds,
        # three browser checks, two more verifications of the SAME url, and a
        # replan that reset plan progress from 5/5 to 0/5 before re-walking it.
        #
        # This states what the verifier actually knows and nothing more. It does
        # NOT say "finish": this tool cannot see the plan, and only the finish
        # gate knows whether the remaining steps are satisfied. Re-verifying is
        # still correct after a MATERIAL change — the claim is durable against
        # re-proof, not against real mutation.
        next_action = (
            "Verified — this claim is now proven and recorded for this URL. "
            "Re-running the same verification without changing the app proves "
            "nothing new. Move to your remaining plan steps, and if none are "
            "outstanding, finish; re-verify only after a material change."
        )

    result: dict[str, Any] = {
        "passed": passed,
        "verdict": verdict,
        "url": url,
        "http_status": http_status,
        "title": title,
        "document_content_type": document_content_type,
        "meaningful_content": meaningful,
        "visible_text_chars": visible_text_chars,
        "elements_count": elements_count,
        "canvas_count": canvas_count,
        "console_errors": console_errors,
        "console_warnings": console_warnings,
        "network_failures": network_failures,
        "screenshot_path": screenshot_path,
        "rendered_text": rendered_text,
        "visible_dom_text": visible_dom_text,
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