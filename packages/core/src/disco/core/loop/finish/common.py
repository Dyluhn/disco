"""Shared implementation for the finish path package.

Extracted from engine.py as a stateful collaborator: `FinishGate` holds a
back-ref to its `FinishLoopFacet` and runs the affirmative-finish pipeline. The
module-level verify-command builders, the web-deliverable / browser-verify
helpers, the finish-cap constants, and the `_DoDWorkspaceUnavailable` exception
moved here too (re-exported from engine for back-compat). Method bodies are
byte-identical with `self.` rewritten to `self._loop.` (sibling calls stay
in-collaborator).
"""

# Shared implementation helpers are imported explicitly by finish services.
# ruff: noqa: F401

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import re
from typing import TYPE_CHECKING, Any, Protocol, cast

from ...context.artifact_projection import (
    artifact_manifest_reader_enabled,
    artifact_paths_from_events,
    artifact_paths_from_manifest_records,
    manifest_path_divergence,
)
from ...contract.export_render import (
    EXPORT_GATE_MAX_REFUSALS,
    ExportRenderFacts,
    count_export_gate_refusals,
    export_gate_refusal_reminder,
    export_gate_release_warning,
    export_render_facts_for_path,
    latest_export_render_facts,
    latest_export_render_index,
)
from ...dod import DoDSpec, FileExistsPredicate, predicate_fingerprint, predicate_fingerprints
from ...dod_evaluator import DoDEvaluator, HttpProbeResult
from ...effects import EffectCapability
from ...env import disco_env
from ...events import (
    ActionEvent,
    AgentErrorEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    DeliverableEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    PlanVerifierFailure,
    PlanVerifierPass,
    StatusEvent,
    ToolCall,
    VerifierShadowEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
    current_build_platform_admission,
    current_workspace_agent_view_id,
    event_matches_current_workspace_intent,
    latest_workspace_run_intent,
)
from ...llm import OperatingMode
from ...state import ConversationState
from ...verification import (
    HostVerificationClaim,
    HostVerificationResult,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerifierReferenceImage,
    apply_semantic_verifier_result,
    default_structured_web_claims,
)
from ...view import effective_plan_progress
from .. import signals
from ..boundaries import (
    AgentStep,
    HostVerificationDeliverable,
    TypedVerifierVerdict,
    VerifierContextSeed,
    VerifierScreenshot,
)
from ..control import Disp
from ..plan_conditions import (
    DictatedContentCondition,
    dictated_content_conditions_from_events,
)
from ..preview_target import (
    PREVIEW_PORTS,
    active_managed_preview_ports,
    backend_shares_host_network,
    parse_port_ownership,
    port_ownership_probe_command,
    resolve_preview_port,
)
from ..signals import _NON_PRODUCTIVE_TOOLS
from ._common_parts.browser_signals import _browser_content_meaningful
from ._common_parts.command_signals import (
    _CONTENT_VERB_RE,
    _FILE_WRITE_TOOLS,
    _GREP_EXECUTABLES,
    _NONVALIDATION_EXECUTABLES,
    _PYTHON_EXECUTABLES,
    _PYTHON_PARSER_RE,
    _SHELL_TOOLS,
    _VALIDATOR_EXECUTABLES,
    _VERIFY_ACTION_RE,
    _is_real_validation_command,
    _shell_command_text,
)
from ._common_parts.event_authority import (
    _browser_observation_failed,
    _browser_verified_verdict,
    _index_html_or_port_owned,
    _mutation_authority_update,
    _preview_authority_update,
    _preview_start_pair_observed,
    _qualifying_browser_observations,
    _verify_web_app_verdict_failed,
)
from ._common_parts.preview_urls import (
    _PREVIEW_PORTS,
    _preview_key,
    _safe_deliverable_file_path,
    _url_targets_preview,
)
from ._common_parts.verifier_values import (
    _VERIFIER_CHECK_KEYS,
    _VERIFIER_MAX_DICT_ITEMS,
    _VERIFIER_MAX_LIST_ITEMS,
    _VERIFIER_MAX_STRING_CHARS,
    _bounded_verifier_check_results,
    _bounded_verifier_value,
    _screenshot_from_verdict,
)

if TYPE_CHECKING:
    from ..ports import (
        ConversationModePort,
        FinishVerificationPort,
        GateCounterPort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        TurnControlPort,
    )

    class FinishLoopFacet(

        ConversationModePort,

        FinishVerificationPort,

        GateCounterPort,

        LoopEventPort,

        PlanLifecyclePort,

        ToolExecutionPort,

        TurnControlPort,

        Protocol,

    ):

        """Shared loop capability for the FinishGate composition.

        Inherited by every mixin in the composition, so it carries the
        union of what they reach: conversation mode, finish verification, gate counters, the event
        log, the plan lifecycle, tool execution, turn control.
        """

_LOG = logging.getLogger("disco.loop")

# finish-verify cap (issue B). The model-authored `verify` gate was the ONLY uncapped
# gate in the loop — a broken/always-failing verify command could refuse `finish`
# forever. Mirror the existing _browser_verify_refusals release valve: after N REAL
# failures, finish anyway with a LOUD warning (the failure stays visible — important
# for weak local models — rather than grinding to max_iterations). A MALFORMED verify
# (SyntaxError / command-not-found) is a broken check, not a failed task, so it is
# auto-stripped (bounded by _finish_verify_strips so it can't be gamed as a free finish).
_FINISH_VERIFY_CAP = 3

# C1c external DoD retry cap. External authority never auto-releases: once
# the bounded repair budget is exhausted, the run pauses fail-closed for user
# review without weakening the acceptance boundary or looping forever.
_DOD_REFUSAL_CAP = 3

# REL-27 — finish-time sealability refusal cap. The probe refusal is trivially
# actionable (it names the exact blocking entries and the remedy), so a capable
# model clears it in one step; a model that cannot must still reach a terminal.
# After N refusals the finish RELEASES LOUDLY (unsealed_release marker + visible
# warning) and the commit-time strict seal + post-terminal disclosure carry the
# honest unsealed truth — the cap breaks the loop, never the honesty.
_FINISH_SEAL_CAP = 3

# REL-RC-O — quoted user literals are hard content floors at finish, but the
# refusal must be bounded so a model that cannot repair does not deadlock.
_DICTATED_CONTENT_REFUSAL_CAP = 3

_HOST_VERIFY_AUTHORITATIVE_FLAG = "HOST_VERIFY_AUTHORITATIVE"
_FALSY = frozenset({"0", "false", "no", "off"})


def _appkit_scope_active(loop: FinishLoopFacet) -> bool:
    """True when the loop is running under the strict AppKit tool surface.

    The build-phase tool-surface signal is ``verify_appkit_app``: it is present in
    the AppKit-only allowlist and absent from normal build surfaces.
    """

    executor = getattr(loop, "executor", None)
    if executor is None:
        return False
    # The phase object persists even while the strict verifier is temporarily
    # hidden from an earlier AppKit phase's tool allowlist.  Generic web PASS
    # must never delegate around the strict AppKit floor in that window.
    if getattr(executor, "appkit_phase", None) is not None:
        return True
    try:
        return any(
            getattr(tool, "name", None) == "verify_appkit_app"
            for tool in executor.available_tools()
        )
    except Exception:  # noqa: BLE001 — introspection failure is not an AppKit signal
        return False


def host_verify_authoritative_enabled() -> bool:
    """Default ON unless DISCO_HOST_VERIFY_AUTHORITATIVE is explicitly falsy.

    Explicit off restores the REL-1c shadow posture. The authoritative default
    is live-proven as of 2026-07-03: no false-block on good builds, broken apps
    are caught and driven to repair, and an ``unavailable`` host verdict
    (browser infrastructure absent) degrades to the inline browser gate rather
    than hard-refusing.
    """
    return str(disco_env(_HOST_VERIFY_AUTHORITATIVE_FLAG) or "").strip().lower() not in _FALSY


# W5 — execution-nudge cap. The execution gate was the ONE uncapped gate in the
# finish path (the comment "No cap" in the old code). After _EXECUTION_NUDGE_CAP
# consecutive nudges the gate RELEASES with a LOUD warning so an agent that cannot
# act (e.g. every tool is withheld) doesn't grind forever. Mirror the cap-3
# pattern of _FINISH_VERIFY_CAP / _DOD_REFUSAL_CAP / _browser_verify_refusals.
_EXECUTION_NUDGE_CAP = 3

# Symmetric to _PLAN_NUDGE on the execution side: a hard gate that refuses FINISHED
# until the agent has done productive work since the most recent plan approval.
# Small open models sometimes echo the plan as prose and declare "done" without
# touching anything — the gate catches that and re-enters the loop. Phrased as a
# `<system-reminder>` (ambient, implicit) rather than a user-tone scolding: the
# model sees an automated environment notification, not a confrontation.
_EXECUTION_NUDGE = (
    "<system-reminder>\n"
    "The approved plan has not been executed yet — no workspace files have been "
    "written, edited, or run since approval. Continue by calling a tool "
    "(file_write, file_edit, shell, code_exec, …) to carry out the plan's steps "
    "in order. The plan is in your context above.\n"
    "</system-reminder>"
)

# E4 — static-site verify. A static deliverable shouldn't have to curl a running
# server to prove it's good; the honest post-condition is "the file exists and is
# parseable HTML." The agent signals this with verify="static" (default index.html)
# or verify="static:<path>". We translate it to a server-free python3 check that
# runs through the SAME safe gate as any verify command (it assesses LOW — no
# confirm). exit 0 ⇔ the page exists, is non-trivial, and parses.
_STATIC_VERIFY_PREFIX = "static"


def _static_verify_command(path: str) -> str:
    p = (path or "index.html").strip().strip("'\"") or "index.html"
    # single-quote the path safely for the shell, then hand to python3 -c
    safe = p.replace("'", "'\\''")
    script = (
        "import sys,os.path,html.parser as H;"
        f"p='{safe}';"
        "(os.path.isfile(p) or sys.exit('missing '+p));"
        "d=open(p,encoding='utf-8',errors='replace').read();"
        "(len(d.strip())>=20 or sys.exit('empty '+p));"
        "t=[];pr=H.HTMLParser();pr.handle_starttag=lambda n,a:t.append(n);pr.feed(d);"
        "print('OK '+p+' tags='+str(len(t)));"
        "sys.exit(0 if t else 'no html tags in '+p)"
    )
    return f'python3 -c "{script}"'


# verify="app" / "app:<url>" — SERVER-AWARE self-verification (the verify_app
# gap). Where `static` only proves a file exists + parses, `app` proves the
# RUNNING deliverable actually serves: it GETs the URL inside the sandbox and
# requires HTTP 200 + a non-trivial body. Portable (python3/urllib — no curl/
# chromium dependency) so it runs on every backend; routes through the same safe
# verify gate. A full pixel screenshot needs a browser in the sandbox image (not
# present on the process backend) — this is the honest server-up post-condition.
_APP_VERIFY_PREFIX = "app"


def _app_verify_command(url: str) -> str:
    # NO hardcoded :8000 default. The platform assigns the preview port (it is NOT a fixed
    # port — see PreviewManager), so the ACTUAL served URL must be supplied by the caller
    # (`resolve_verify_command` resolves it from the live preview, and degrades to the
    # server-free static check when no preview can be located). A bare `verify="app"` with
    # no resolvable preview never reaches here with an empty url — guarding against an empty
    # url keeps this from fabricating a wrong port.
    u = (url or "").strip().strip("'\"")
    safe = u.replace("'", "'\\''")
    # No try/except (a `-c` one-liner can't carry the block): a connection failure
    # raises URLError → nonzero exit + a traceback the agent reads as "not serving".
    script = (
        "import sys,urllib.request as U;"
        f"u='{safe}';"
        "r=U.urlopen(u,timeout=10);"
        "code=getattr(r,'status',None) or r.getcode();"
        "(code==200 or sys.exit('HTTP '+str(code)+' from '+u));"
        "b=r.read().decode('utf-8','replace');"
        "(b.strip() or sys.exit('empty body from '+u));"
        "print('OK '+u+' '+str(code)+' bytes='+str(len(b)))"
    )
    return f'python3 -c "{script}"'


def _last_productive_seq(events: list[Event]) -> int:
    """Seq of the last successful state-changing action (not in _NON_PRODUCTIVE_TOOLS).
    If no successful productive action found, returns 0."""
    action_succeeded: dict[str, bool] = {}
    for ev in events:
        if isinstance(ev, ObservationEvent):
            action_id = ev.action_id
            if isinstance(action_id, str):
                action_succeeded.setdefault(action_id, bool(ev.tool_result.success))
        elif isinstance(ev, AgentErrorEvent) and ev.action_id is not None:
            action_succeeded.setdefault(ev.action_id, False)
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            if (
                ev.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS
                and action_succeeded.get(ev.id) is True
            ):
                return ev.seq or 0
    return 0


def _last_verification_authority_seq(events: list[Event]) -> int:
    """Last event that can change which exact verification receipt is current.

    Preview controls remain nonproductive for execution accounting, while a
    causally paired successful start/stop still changes the browser authority.
    Handoffs and user/admission requirements likewise change receipt identity
    without pretending that they edited workspace bytes.
    """

    authority_seq = _last_productive_seq(events)

    actions: dict[str, ActionEvent] = {}
    active_preview_sessions: set[str] = set()
    for event in events:
        if isinstance(event, ActionEvent):
            actions[event.id] = event
        elif isinstance(event, ObservationEvent):
            authority_seq = _mutation_authority_update(event, authority_seq)
            authority_seq = _preview_authority_update(
                event, actions, active_preview_sessions, authority_seq
            )
        elif isinstance(event, AgentErrorEvent):
            authority_seq = _mutation_authority_update(event, authority_seq)
        elif isinstance(event, DeliverableEvent) and type(event.seq) is int:
            authority_seq = max(authority_seq, event.seq)
        elif isinstance(event, BuildPlatformAdmissionEvent) and type(event.seq) is int:
            authority_seq = max(authority_seq, event.seq)
        elif (
            isinstance(event, MessageEvent)
            and event.source is EventSource.USER
            and type(event.seq) is int
        ):
            authority_seq = max(authority_seq, event.seq)
    return authority_seq


def _tc_components_installed(events: list[Event]) -> bool:
    """True when a trusted component was SUCCESSFULLY installed in this
    conversation (WO-TC3): such a build must pass through the verify_web_app
    gate even when nothing else marks it as a web deliverable — the component
    checks (integrity / requires-graph / probe) ride that verdict, and a
    finish that skipped them would silently drop every verified-component
    claim. Events-only, pure."""
    for ev in events:
        if isinstance(ev, ObservationEvent):
            tr = ev.tool_result
            if tr.success and tr.tool_name == "add_trusted_component":
                return True
    return False


def _is_web_deliverable(events: list[Event]) -> bool:
    """Web deliverable if index.html was written/edited OR port 8000 owned by non-preview
    OR a successful preview_start action/observation pair exists.
    Derived from events to keep the check pure (event-list in, verdict out)."""

    if _preview_start_pair_observed(events):
        return True
    return _index_html_or_port_owned(events)


def _latest_plan_revision(events: list[Event]) -> int | None:
    rev: int | None = None
    for ev in events:
        if isinstance(ev, PlanEvent):
            rev = ev.revision if rev is None else max(rev, ev.revision)
    return rev


def _plan_file_exists_paths(events: list[Event]) -> list[str]:
    out: list[str] = []
    plan = signals.latest_approved_plan(events)
    if plan is None:
        return out
    for step in plan.steps:
        dc = step.done_condition
        if isinstance(dc, FileExistsPredicate):
            p = _safe_deliverable_file_path(dc.path)
            if p is not None:
                out.append(p)
    return out


def _deliverable_event_paths(events: list[Event]) -> list[str]:
    out: list[str] = []
    for ev in events:
        if not isinstance(ev, DeliverableEvent):
            continue
        # Public `serve` records a validated entry file for apps. Do not rewrite
        # non-index entries such as server.py into the impossible
        # server.py/index.html path.
        p = _safe_deliverable_file_path(ev.path)
        if p is not None:
            out.append(p)
    return out


def _latest_app_deliverable_event(events: list[Event]) -> DeliverableEvent | None:
    for ev in reversed(events):
        if (
            isinstance(ev, DeliverableEvent)
            and ev.artifact_kind == "app"
            and event_matches_current_workspace_intent(events, ev)
        ):
            return ev
    return None


def _latest_deliverable_event(events: list[Event]) -> DeliverableEvent | None:
    for ev in reversed(events):
        if isinstance(ev, DeliverableEvent) and event_matches_current_workspace_intent(events, ev):
            return ev
    return None


def _artifact_record_kind(record: Any) -> str:
    kind = getattr(record, "kind", "files")
    return kind if isinstance(kind, str) and kind else "files"


# ---- Bug 6: actionless honest-unverifiable-static finish helpers ------------
# The existing honest-unverifiable finish (`_maybe_honest_unverifiable_static_finish`)
# only fires at the FINISH GATE. When the plan's verify step is unsatisfiable on a
# browserless backend the model never reaches that gate — it churns and the actionless
# valve PAUSES it. These pure helpers feed the SAME honest-finish concept at the
# actionless valve (RCA option 3a), under conservative guards so a missing/broken
# deliverable or a genuine web failure never converts to a success (W-45 preserved).

# Distinctive substring of `browser.BROWSER_UNAVAILABLE_MSG` (kept inline rather than
# imported — core must not depend on the tools package). Matched against the error/
# content text a failed `browser` action leaves in the log.
_BROWSER_UNAVAILABLE_TEXT = "browser verification is unavailable"


def _missing_steps_all_verify(events: list[Event]) -> bool:
    """Bug 6 — True iff a plan exists, is INCOMPLETE, and EVERY not-done
    (missing/active) step is a verification-only step. A step qualifies iff it has a
    verification-ACTION framing (`_VERIFY_ACTION_RE`) AND has NO creation/content verb
    (`_CONTENT_VERB_RE`, a hard negative override). So "Verify the page renders
    correctly" qualifies, while "Add input validation", "Set up linting", "Create
    product renders", "Add a testimonials section" do NOT (content verb present) and a
    bare noun like "validation"/"lint" does NOT (no action framing). Conservative: any
    not-done step that isn't UNAMBIGUOUS verification → False (real work remains, so the
    actionless valve must PAUSE, never honest-finish). Returns False when the plan is
    complete (no missing steps — the `completed_via_notify` branch owns that)."""
    plan, states = effective_plan_progress(events)
    if plan is None or not plan.steps:
        return False
    missing = [i for i in range(len(plan.steps)) if states.get(i + 1) != "done"]
    if not missing:
        return False  # complete → not this path
    for i in missing:
        step = plan.steps[i]
        text = f"{step.title} {step.detail or ''}"
        if _CONTENT_VERB_RE.search(text):
            return False  # creation/content work → never verify-only (negative override)
        if not _VERIFY_ACTION_RE.search(text):
            return False  # no verification-action framing → not verify-only
    return True


def _last_edit_seq(events: list[Event]) -> int:
    """Seq of the last FILE write/edit action (a deliverable mutation), else 0.
    The non-browser validation must have RUN AFTER this for its pass to count."""
    for ev in reversed(events):
        if isinstance(ev, ActionEvent) and ev.tool_call.tool_name in _FILE_WRITE_TOOLS:
            return ev.seq or 0
    return 0


def _nonbrowser_static_validation_passed(events: list[Event]) -> bool:
    """Bug 6 — True iff, AFTER the last file write/edit, a NON-browser shell
    validation that GENUINELY inspects the static deliverable's CONTENT/STRUCTURE
    (an HTML/XML parser, a markup/structure check, or a content grep on
    `index.html`) RAN and SUCCEEDED, with no later FAILED such validation. This is
    the positive evidence the delivered file is well-formed when the browser is
    unavailable. A bare existence/dump (`ls`/`test -f`/`stat`/`cat`/`wc`) or a mere
    echo of the word "validate"/"markup" does NOT count — only a STRUCTURAL parser/
    validator invocation does (`_is_real_validation_command`, the codex catch). A
    FAILED validation yields False so a broken parse BLOCKS the honest finish; no
    qualifying validation at all → False (we require a passing one)."""
    edit_seq = _last_edit_seq(events)
    # action_id -> the shell action is a REAL content validation against index.html.
    validation_actions: set[str] = set()
    verdict: bool | None = None
    for ev in events:
        if isinstance(ev, ActionEvent) and ev.tool_call.tool_name in _SHELL_TOOLS:
            if _is_real_validation_command(_shell_command_text(ev)):
                validation_actions.add(ev.id)
        elif isinstance(ev, ObservationEvent):
            if (ev.seq or 0) > edit_seq and ev.action_id in validation_actions:
                verdict = bool(ev.tool_result.success)
        elif isinstance(ev, AgentErrorEvent):
            if (ev.seq or 0) > edit_seq and ev.action_id in validation_actions:
                verdict = False
    return verdict is True


def _browser_unavailable_observed(events: list[Event]) -> bool:
    """Bug 6 — True iff a `browser` action left an 'unavailable on this backend'
    signal in the log: an ObservationEvent carrying `structured.browser_unavailable`,
    or an Observation/AgentError whose text matches `BROWSER_UNAVAILABLE_MSG`. This
    is the second of the two "browser genuinely unavailable" signals (the first is
    `_browser_verification_unavailable` — no browser tool at all)."""
    for ev in reversed(events):
        if isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser":
            res = ev.tool_result
            if (res.structured or {}).get("browser_unavailable"):
                return True
            if _BROWSER_UNAVAILABLE_TEXT in (res.content or "") or _BROWSER_UNAVAILABLE_TEXT in (
                res.error or ""
            ):
                return True
        elif isinstance(ev, AgentErrorEvent):
            if _BROWSER_UNAVAILABLE_TEXT in (ev.error or ""):
                return True
    return False


def _real_web_failure_evidence(events: list[Event]) -> bool:
    """Bug 6 (W-45 guard) — True iff there is genuine 'the app is BROKEN' evidence
    (not mere infra-unavailability): a `verify_web_app` verdict or a SUCCESSFUL
    `browser` observation showing console errors, NETWORK failures, or a
    served-but-blank render. ANY such evidence BLOCKS the honest actionless finish —
    only an unverifiable (never a broken) build may finish honestly. A FAILED browser
    call is infra-unavailability (an AgentErrorEvent, not an ObservationEvent) and is
    NOT treated as app-broken here."""
    for ev in events:
        if not isinstance(ev, ObservationEvent):
            continue
        res = ev.tool_result
        st = res.structured or {}
        if res.tool_name == "verify_web_app":
            if _verify_web_app_verdict_failed(st):
                return True
        elif res.tool_name == "browser":
            if _browser_observation_failed(res.success, st):
                return True
    return False


def _browser_verified(
    events: list[Event], since_seq: int, target_key: tuple[str, int] | None = None
) -> tuple[bool, str | None]:
    """Scan the AGENT's browser observations since since_seq. Returns (ok,
    first_error_line). An observation is valid if it's from the browser tool,
    against the resolved preview target (`target_key`; :8000 when undetectable —
    see `_url_targets_preview`), and has zero console errors. If not ok, returns
    the first error from the LATEST qualifying observation.

    The finish gate's OWN driven probe (the verify-overclaim active check —
    ActionEvent tagged `verify_probe`) is EXCLUDED here: this helper answers "did
    the AGENT verify cleanly", and the gate judges its own probe separately (with
    the blank-render guard). Without the exclusion a clean-console-but-blank probe
    from a prior refusal cycle would read back as an agent pass and bypass the
    blank guard."""
    valid_obs = _qualifying_browser_observations(events, since_seq, target_key)
    return _browser_verified_verdict(valid_obs)


def _latest_browser_structured(
    events: list[Event], target_key: tuple[str, int] | None = None
) -> dict | None:
    """Structured payload of the LATEST successful browser observation on the
    resolved preview target (`target_key`; :8000 when undetectable — see
    `_url_targets_preview`), any seq. Feeds the active finish probe's blank-render
    judgement — "the gate's own observation". None when the agent/gate never
    produced a qualifying browser observation."""
    for ev in reversed(events):
        if not (isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser"):
            continue
        res = ev.tool_result
        if not (res.success and res.structured):
            continue
        url = str(res.structured.get("url", ""))
        if _url_targets_preview(url, target_key):
            return res.structured
    return None


def _vision_mode() -> bool:
    """W6 — runtime vision gate for the browser and finish gates.

    True when the driver or escalation model can accept images:
      • DISCO_DRIVER_VISION=1 (or legacy PMX_DRIVER_VISION=1): the local driver
        has a vision projection loaded.
      • DISCO_VISION_ESCALATION_MODEL (or legacy PMX_VISION_ESCALATION_MODEL) is
        set: a separate vision-capable model is configured for escalation.

    The env-var check is the contract surface here; wiring.py / config.py set
    these on startup from the resolved RouterConfig. Consumer code (this module,
    browser.py) reads them; config.py/wiring.py own them."""
    return disco_env("DRIVER_VISION") == "1" or bool(disco_env("VISION_ESCALATION_MODEL"))


def _latest_browser_screenshot(
    events: list[Event], target_key: tuple[str, int] | None = None
) -> str | None:
    """W6 — screenshot_path from the LATEST qualifying browser observation.

    Searches backward through events for a successful browser observation on the
    resolved preview target (`target_key`; :8000 when undetectable — see
    `_url_targets_preview`); returns the `screenshot_path` field if present. None
    when the agent never browsed the preview or the daemon didn't produce a
    screenshot."""
    for ev in reversed(events):
        if not (isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser"):
            continue
        res = ev.tool_result
        if not (res.success and res.structured):
            continue
        url = str(res.structured.get("url", ""))
        if not _url_targets_preview(url, target_key):
            continue
        path = res.structured.get("screenshot_path")
        if path:
            return str(path)
    return None


def _latest_browser_error(
    events: list[Event], target_key: tuple[str, int] | None = None
) -> str | None:
    """First error-level console line of the LATEST qualifying browser observation
    (any seq — full history). None if the agent never browsed the preview or its
    last look was clean. This feeds the gate's human-facing messages: the verdict
    is scoped to since-last-edit (_browser_verified), but "last console errors"
    must report what was actually last SEEN — a post-browse edit moves since_seq
    past the observation and would otherwise erase a real, observed error."""
    for ev in reversed(events):
        if not (isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser"):
            continue
        res = ev.tool_result
        if not (res.success and res.structured):
            continue
        url = str(res.structured.get("url", ""))
        if not _url_targets_preview(url, target_key):
            continue
        errors = [
            str(c.get("text", ""))
            for c in res.structured.get("console", [])
            if c.get("level") == "error"
        ]
        return errors[0] if errors else None
    return None


# W-45 — verify_web_app verdict readers. The build agent's `verify_web_app` tool
# returns a STRUCTURED pass/fail verdict (server-up + HTTP + console/network +
# blank-render → one decision). The finish gate consumes that verdict instead of
# re-deriving a verdict from raw browser observations (which it could never
# CONCLUDE on — the 25-40× reload loop). The marker StatusEvent carries the
# verdict's `failure_fingerprint` so a repeat of the SAME failure WITHOUT a
# productive edit is recognized as no-progress and halts, rather than re-looping.
_VERIFY_MARKER_PREFIX = "verify_no_progress:"


def _verdict_targets_preview(verdict: dict, target_url: str | None) -> bool:
    """P1-1 — does `verdict` address the CURRENT preview target?

    When `target_url` is None the live preview could not be detected (sandbox-less
    / legacy backend) → binding is NOT enforced and the verdict is accepted on the
    (since_seq) key alone (preserves the pre-binding behavior the pure-reader unit
    tests exercise). When the target IS known, the verdict's `url` must resolve to
    the SAME (host, port) preview key — a stale or foreign-port verdict (or one
    with no url) does NOT satisfy the gate, so the caller drives a fresh verify
    against the real preview instead of landing a stale PASS."""
    if target_url is None:
        return True
    vkey = _preview_key(str(verdict.get("url") or ""))
    tkey = _preview_key(target_url)
    return vkey is not None and tkey is not None and vkey == tkey


def _latest_verify_verdict(
    events: list[Event],
    since_seq: int,
    target_url: str | None = None,
    tool_name: str = "verify_web_app",
) -> dict | None:
    """Structured verdict of the LATEST `tool_name` observation since
    `since_seq` whose url BINDS to the current preview `target_url` (P1-1). None
    when no such verdict exists. `tool_name` is `verify_web_app` on normal Build
    and `verify_appkit_app` in strict AppKit mode; both emit the same verdict shape.

    The cache key is effectively (since_seq, server/port, url): a non-productive
    probe (browser/navigate/verify_web_app itself) does NOT move `since_seq`, AND
    a verdict for a DIFFERENT preview url/port (or with no url) is skipped — so a
    stale or foreign-URL PASS can never satisfy the finish gate. A productive edit
    advances `since_seq` past the stale verdict → None → the gate drives a fresh
    verify. `target_url=None` disables the url binding (sandbox-less / legacy
    callers + the pure unit tests)."""
    for ev in reversed(events):
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == tool_name:
            res = ev.tool_result
            if (
                res.success
                and res.structured
                and _verdict_targets_preview(res.structured, target_url)
            ):
                return res.structured
    return None


def _latest_host_verifier_verdict(events: list[Event], since_seq: int) -> str | None:
    """Verdict label of the LATEST host-verifier ``VerifierVerdictEvent`` since
    ``since_seq``, or None when the host gate has not recorded one for the
    current served output (a productive edit advances ``since_seq`` past stale
    verdicts, same cache-key rule as ``_latest_verify_verdict``)."""
    for ev in reversed(events):
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if isinstance(ev, VerifierVerdictEvent):
            return str(ev.verdict) if ev.verdict is not None else None
    return None


def _latest_host_verifier_event(events: list[Event], since_seq: int) -> VerifierVerdictEvent | None:
    """Latest complete host-verifier event after the current authority boundary."""

    for event in reversed(events):
        if event.seq is None or event.seq <= since_seq:
            continue
        if isinstance(event, VerifierVerdictEvent):
            return event
    return None


def _prior_verify_marker_fp(events: list[Event], since_seq: int) -> str | None:
    """failure_fingerprint stamped on the most recent `verify_no_progress:<fp>`
    marker since `since_seq`. None when the gate has not yet refused for the
    current served output. If a later marker carries the SAME fingerprint as the
    current verdict, the gate has already nudged for this exact failure with no
    productive edit in between → no new information → stop (the loop breaker)."""
    for ev in reversed(events):
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if (
            isinstance(ev, StatusEvent)
            and ev.detail
            and ev.detail.startswith(_VERIFY_MARKER_PREFIX)
        ):
            return ev.detail[len(_VERIFY_MARKER_PREFIX) :]
    return None


class _DoDWorkspaceUnavailable(Exception):
    """The C1c gate cannot resolve a workspace_root (no sandbox, no
    `workspace_path` attribute on the executor). Raised by
    `_build_dod_evaluator` and caught by `_finish_dod_gate_passed` to
    degrade the gate to a no-op (logged, never raised past the gate).

    Distinct from a predicate failure: the spec is set but the engine
    has no evidence surface to grade against. Refusing the finish in
    that state would be a silent fail — the agent would loop forever
    on a gate that cannot run. Logging + skipping is the honest
    behavior; the audit trail sees the log line."""


class _FinishGateComponent:
    """Base for finish services with an explicit coordinator reference."""

    def __init__(self, loop: Any, coordinator: Any) -> None:
        self._loop = loop
        self._coordinator = coordinator


class _FinishGateProto:
    if TYPE_CHECKING:
        _loop: FinishLoopFacet

        def _contract_required_deliverable_paths(self) -> list[str]: ...

        async def _detect_preview_url(self) -> str | None: ...

        async def _drive_finish_browser_probe(self, target_url: str | None = None) -> bool: ...

        async def finish_verify_passed(self, command: str) -> tuple[bool, bool]: ...

        async def finish_dod_gate_passed(self) -> bool: ...

        async def gate_execution_nudge(self, step: AgentStep, events: list[Event]) -> Disp: ...

        async def dictated_content_gate_passed(self, events: list[Event]) -> bool: ...

        async def run_finish_verify_gates(self, step: AgentStep, events: list[Event]) -> Disp: ...

        async def seal_gate_allows_finish(self) -> bool: ...

        async def normalize_finish_step(
            self, step: AgentStep, events: list[Event]
        ) -> tuple[AgentStep, Disp]: ...

        async def handle_finish_path(
            self, step: AgentStep, state: ConversationState, events: list[Event]
        ) -> Disp: ...

        def _active_verify_tool(self) -> str | None: ...

        async def _artifact_manifest_records(
            self, events: list[Event], *, consumer: str
        ) -> tuple[Any, ...] | None: ...

        def _host_verify_authoritative(self) -> bool: ...

        async def _host_verify_deliverable(
            self,
            step: AgentStep,
            events: list[Event],
            *,
            include_unverifiable: bool = False,
        ) -> HostVerificationDeliverable | None: ...

        async def gate_host_verify(self, step: AgentStep, events: list[Event]) -> Disp: ...

        async def gate_browser_verify(
            self,
            step: AgentStep,
            events: list[Event],
            *,
            appkit_prepared: (
                tuple[VerifierStartedEvent, HostVerificationDeliverable] | None
            ) = None,
        ) -> Disp: ...

        async def gate_export_render(self, step: AgentStep, events: list[Event]) -> Disp: ...

        async def _record_verifier_failure_to_context(
            self, *, message: str, rel_path: str | None
        ) -> None: ...


__all__ = [  # pyright: ignore[reportUnsupportedDunderAll]
    name for name in globals() if not name.startswith("__") and name not in {"annotations"}
]
