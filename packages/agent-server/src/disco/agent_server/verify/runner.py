"""disco-verify API-first runner (W15).

Drives the app through its HTTP/WS API exactly like a frontend would — no
browser, no internal calls — and writes a redacted evidence dossier per run.

Key invariant (from codex): ``disco-verify`` is API-FIRST and has NO
Playwright/browser dependency. Every signal comes from the real HTTP/WS
boundary the frontend uses.

Usage::

    from disco.agent_server.verify.runner import run_scenario
    from disco.agent_server.verify.scenarios import slides_from_research_report

    result = await run_scenario(slides_from_research_report, agent_base="http://127.0.0.1:8000")
    print(result.passed, result.dossier_path)

Injectable transport (``_client`` param) keeps IO decoupled from logic so unit
tests can feed canned events without a live server.

---

PKG-08 extraction: the implementation now lives in cohesive sub-modules under
``runner_parts/`` (transport, discovery, policy_checks, orchestration). This
module is a state-free compatibility/export facade that preserves the exact
public import surface so existing callers and tests are unchanged.
"""

from __future__ import annotations

from .runner_parts.discovery import (
    _DELIVERABLE_TYPE_EXTS,
    _VALIDATABLE_EXTS,
    _deliverable_type_satisfied,
    _locate_deliverables,
)
from .runner_parts.orchestration import (
    _app_body_problem,
    _run_file_validators,
    _run_validators,
    _validate_app_deliverables,
    _validate_report_export,
    _write_dossier,
    run_scenario,
)
from .runner_parts.policy_checks import (
    _appkit_verdicts,
    _run_appkit_verify_check,
    _run_forbid_checks,
    _run_tool_checks,
    _tool_names_used,
    _tools_with_success,
)
from .runner_parts.transport import (
    _MAX_AUTO_ANSWERS,
    _POLL_INTERVAL,
    _TERMINAL,
    AbstractVerifyClient,
    HttpVerifyClient,
    _status_from_frame,
)

__all__ = [
    # transport
    "AbstractVerifyClient",
    "HttpVerifyClient",
    "_MAX_AUTO_ANSWERS",
    "_POLL_INTERVAL",
    "_TERMINAL",
    "_status_from_frame",
    # discovery
    "_DELIVERABLE_TYPE_EXTS",
    "_VALIDATABLE_EXTS",
    "_deliverable_type_satisfied",
    "_locate_deliverables",
    # policy_checks
    "_appkit_verdicts",
    "_run_appkit_verify_check",
    "_run_forbid_checks",
    "_run_tool_checks",
    "_tool_names_used",
    "_tools_with_success",
    # orchestration
    "_app_body_problem",
    "_run_file_validators",
    "_run_validators",
    "_validate_app_deliverables",
    "_validate_report_export",
    "_write_dossier",
    "run_scenario",
]
