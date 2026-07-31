"""Forbid/tool/AppKit policy checks over the event log (PKG-08 extraction).

These checks are pure (no IO): they judge the event log + deliverable list for
forbidden patterns, the EPIC M strict AppKit tool path, and the EPIC G
structural verifier verdict. Extracted from ``runner.py`` so the forbid-check
McCabe score is scoped to this module.

The public symbols are re-exported by ``runner.py`` so existing imports are
unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from ..schema import Scenario

log = logging.getLogger(__name__)


def _run_forbid_checks(
    scenario: Scenario,
    deliverables: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[str]:
    """Forbid checks over the event log + deliverable list — no file IO."""
    problems: list[str] = []
    for item in scenario.forbid:
        if item == "raw_html_default":
            problems.extend(_check_raw_html_default(deliverables))
        elif item == "procedural_image_provider":
            problems.extend(_check_procedural_image_provider(events))
        else:
            log.debug("Unknown forbid item %r — skipped", item)
    return problems


def _check_raw_html_default(deliverables: list[dict[str, Any]]) -> list[str]:
    """codex round-4: judge only the DECK deliverables (.html/.htm/.pptx/.pdf), NOT
    companion outputs (an editable source .json, a .png, a datasource). Otherwise a
    companion file masks a raw-HTML deck — "not all deliverables are html" → missed."""
    deck_exts = (".html", ".htm", ".pptx", ".pdf")

    def _p(d: dict[str, Any]) -> str:
        return str(d.get("path", "")).lower()

    decks = [d for d in deliverables if _p(d).endswith(deck_exts)]
    html_decks = [d["path"] for d in decks if _p(d).endswith((".html", ".htm"))]
    presentable = [d for d in decks if _p(d).endswith((".pptx", ".pdf"))]
    if html_decks and not presentable:
        return [f"forbid.raw_html_default: deck is raw HTML, no pptx/pdf: {html_decks}"]
    return []


def _check_procedural_image_provider(events: list[dict[str, Any]]) -> list[str]:
    """codex round-2: the real image_generate tool emits `placeholder` /
    `backend` / `backend_connected` — NOT a `provider` field (the old check
    never fired). A procedural placeholder is `placeholder is True` /
    backend "pil-procedural" / not backend_connected."""
    for evt in events:
        if evt.get("kind") != "observation":
            continue
        tr = evt.get("tool_result") or {}
        if tr.get("tool_name") != "image_generate":
            continue
        if not tr.get("success"):
            continue
        structured: dict[str, Any] = tr.get("structured") or {}
        if (
            structured.get("placeholder") is True
            or structured.get("backend") == "pil-procedural"
            or structured.get("backend_connected") is False
        ):
            return [
                "forbid.procedural_image_provider: image_generate produced a "
                "procedural placeholder image"
            ]
    return []


def _tool_names_used(events: list[dict[str, Any]]) -> set[str]:
    """All tool names the run touched — from ActionEvents (the tool the agent CHOSE,
    ``tool_call.tool_name``) and ObservationEvents (the tool that RAN,
    ``tool_result.tool_name``). EPIC M expect_tools/forbid_tools are judged against this:
    an attempted-but-failed call still counts as "used" so a forbidden escape can't hide
    behind a non-success result."""
    names: set[str] = set()
    for evt in events:
        kind = evt.get("kind")
        if kind == "action":
            tc = evt.get("tool_call") or {}
            name = tc.get("tool_name")
            if isinstance(name, str) and name:
                names.add(name)
        elif kind == "observation":
            tr = evt.get("tool_result") or {}
            name = tr.get("tool_name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _tools_with_success(events: list[dict[str, Any]]) -> set[str]:
    """Tool names that produced a SUCCESSFUL OBSERVATION (a real tool RESULT with
    ``success`` truthy) — NOT merely an emitted action. EPIC M ``expect_tools`` is judged
    against this: a golden-path tool that was ATTEMPTED but FAILED (an ActionEvent with no
    successful observation — e.g. an ``app_create`` that errored) must NOT satisfy
    ``expect_tools`` (P1 — codex). Otherwise a degenerate run where app_create was emitted
    but never succeeded would be falsely green on the golden-path check."""
    names: set[str] = set()
    for evt in events:
        if evt.get("kind") != "observation":
            continue
        tr = evt.get("tool_result") or {}
        if not tr.get("success"):
            continue
        name = tr.get("tool_name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _run_tool_checks(scenario: Scenario, events: list[dict[str, Any]]) -> list[str]:
    """EPIC M: prove the run took the strict AppKit golden path. Every ``expect_tools``
    entry must have a SUCCESSFUL OBSERVATION in the event log (not just an emitted action —
    P1); no ``forbid_tools`` entry may appear at all (action OR observation, so a forbidden
    escape can't hide behind a non-success result). Proves ``app_create``/``verify_appkit_app``
    actually SUCCEEDED while the escape hatch (``request_custom_build``) and raw build tools
    (shell/file_write/code_exec) did NOT run."""
    problems: list[str] = []
    if not scenario.expect_tools and not scenario.forbid_tools:
        return problems
    # forbid: ANY use (attempted action OR observation) counts — a forbidden escape that
    # erred still escaped. expect: only a SUCCESSFUL OBSERVATION counts — an attempted-but-
    # failed golden-path tool must NOT satisfy the requirement.
    used = _tool_names_used(events)
    succeeded = _tools_with_success(events)
    missing = [t for t in scenario.expect_tools if t not in succeeded]
    if missing:
        problems.append(
            f"expect_tools: required tool(s) had no successful observation "
            f"(attempted-but-failed or never run): {missing} "
            f"(succeeded: {sorted(succeeded)}, any-use: {sorted(used)})"
        )
    present = [t for t in scenario.forbid_tools if t in used]
    if present:
        problems.append(
            f"forbid_tools: forbidden tool(s) were used: {present} — the strict "
            f"AppKit scope did not hold"
        )
    return problems


def _appkit_verdicts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every SUCCESSFUL ``verify_appkit_app`` structured verdict in the log, in order."""
    verdicts: list[dict[str, Any]] = []
    for evt in events:
        if evt.get("kind") != "observation":
            continue
        tr = evt.get("tool_result") or {}
        if tr.get("tool_name") != "verify_appkit_app" or not tr.get("success"):
            continue
        structured = tr.get("structured")
        if isinstance(structured, dict):
            verdicts.append(structured)
    return verdicts


def _run_appkit_verify_check(events: list[dict[str, Any]]) -> list[str]:
    """EPIC M: require the EPIC G structural verifier (``verify_appkit_app``) to have run
    AND passed. The LAST verdict is authoritative (the agent may iterate to green). A
    missing verdict, a failing verdict, or any failing individual check is a problem —
    each named so the dashboard/dossier can surface the first failing AppKit check."""
    verdicts = _appkit_verdicts(events)
    if not verdicts:
        return ["expect_appkit_verify: no successful verify_appkit_app observation found"]
    final = verdicts[-1]
    if final.get("passed") is True:
        return []
    checks = final.get("checks") or []
    failed = [
        str(c.get("name", "?")) for c in checks if isinstance(c, dict) and not c.get("passed")
    ]
    fp = final.get("failure_fingerprint") or ""
    detail = f" — failing checks: {failed}" if failed else ""
    fp_detail = f" (fingerprint: {fp})" if fp else ""
    return [f"expect_appkit_verify: verify_appkit_app did NOT pass{detail}{fp_detail}"]


__all__ = [
    "_appkit_verdicts",
    "_run_appkit_verify_check",
    "_run_forbid_checks",
    "_run_tool_checks",
    "_tool_names_used",
    "_tools_with_success",
]
