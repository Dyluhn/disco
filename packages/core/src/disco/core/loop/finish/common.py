"""Shared implementation for the finish path package.

Extracted from engine.py as a stateful collaborator: `FinishGate` holds a
back-ref to its `AgentLoop` and runs the affirmative-finish pipeline. The
module-level verify-command builders, the web-deliverable / browser-verify
helpers, the finish-cap constants, and the `_DoDWorkspaceUnavailable` exception
moved here too (re-exported from engine for back-compat). Method bodies are
byte-identical with `self.` rewritten to `self._loop.` (sibling calls stay
in-collaborator).
"""

# Intentional re-export surface consumed by split mixins via
# ``from .common import *``; locally-unused imports are part of that API.
# ruff: noqa: F401

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import re
from typing import TYPE_CHECKING, Any, cast

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

if TYPE_CHECKING:
    from ..engine import AgentLoop

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


def _appkit_scope_active(loop: AgentLoop) -> bool:
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

    def mutation_capability(event: ObservationEvent | AgentErrorEvent) -> bool:
        if isinstance(event, ObservationEvent):
            profile = event.tool_result.action_profile
            receipts = event.tool_result.effect_receipts
        else:
            profile = event.action_profile
            receipts = event.effect_receipts
        return bool(
            (profile is not None and EffectCapability.WORKSPACE_MUTATE in profile.capabilities)
            or any(
                getattr(receipt, "capability", None) is EffectCapability.WORKSPACE_MUTATE
                for receipt in receipts
            )
        )

    actions: dict[str, ActionEvent] = {}
    active_preview_sessions: set[str] = set()
    for event in events:
        if isinstance(event, ActionEvent):
            actions[event.id] = event
        elif isinstance(event, ObservationEvent):
            result = event.tool_result
            if mutation_capability(event) and type(event.seq) is int:
                authority_seq = max(authority_seq, event.seq)
            if result.tool_name not in {"preview_start", "preview_stop"}:
                continue
            action = actions.get(event.action_id)
            structured = result.structured
            if (
                action is None
                or action.tool_call.tool_name != result.tool_name
                or action.tool_call.call_id != result.call_id
                or type(action.seq) is not int
                or type(event.seq) is not int
                or action.seq >= event.seq
                or result.success is not True
                or not isinstance(structured, dict)
            ):
                continue
            if result.tool_name == "preview_start":
                name = structured.get("name")
                if (
                    structured.get("status") not in {"running", "unavailable"}
                    or not isinstance(name, str)
                    or not name
                ):
                    continue
                active_preview_sessions.add(name)
                authority_seq = max(authority_seq, event.seq)
                continue
            stopped = structured.get("stopped")
            if (
                not isinstance(stopped, list)
                or not all(isinstance(name, str) and name for name in stopped)
                or not stopped
            ):
                continue
            active_preview_sessions.difference_update(stopped)
            authority_seq = max(authority_seq, event.seq)
        elif isinstance(event, AgentErrorEvent):
            if mutation_capability(event) and type(event.seq) is int:
                authority_seq = max(authority_seq, event.seq)
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

    # H357: detect a successful preview_start action/observation pair.  The
    # observation must be bound to the matching action id and the tool name
    # must be "preview_start".  A failed, malformed, unpaired/orphaned,
    # wrong-tool, or action-only preview record must not create this signal.
    # Uses a SINGLE forward scan so that an observation before its causal
    # action (forged/reordered) never qualifies — only an action-then-
    # observation pair counts.
    preview_action_ids: set[str] = set()
    for ev in events:
        if isinstance(ev, ActionEvent) and ev.tool_call.tool_name == "preview_start":
            preview_action_ids.add(ev.id)
        elif (
            isinstance(ev, ObservationEvent)
            and ev.tool_result.tool_name == "preview_start"
            and ev.tool_result.success
            and isinstance(ev.action_id, str)
            and ev.action_id in preview_action_ids
        ):
            return True

    # Existing: index.html was written/edited OR port 8000 owned by non-preview.
    for ev in reversed(events):
        if isinstance(ev, ActionEvent):
            if ev.tool_call.tool_name in (
                "file_write",
                "file_edit",
                "file_append",
                "file_replace_lines",
                "file_insert_lines",
            ):
                path = ev.tool_call.arguments.get("path")
                # Canonical workspace-root paths
                if isinstance(path, str) and _safe_deliverable_file_path(path) == "index.html":
                    return True
        elif isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "server_status":
            content = ev.tool_result.content
            # "  - 8000: OWNED by pid 123 (python) [session: dev]"
            for line in content.splitlines():
                if "8000: OWNED" in line and "[session: " in line:
                    session = line.split("[session: ")[1].split("]")[0]
                    if session != "preview":
                        return True
    return False


def _latest_plan_revision(events: list[Event]) -> int | None:
    rev: int | None = None
    for ev in events:
        if isinstance(ev, PlanEvent):
            rev = ev.revision if rev is None else max(rev, ev.revision)
    return rev


def _safe_deliverable_file_path(path: str, *, app_root: bool = False) -> str | None:
    raw = (path or "").strip()
    if not raw:
        return None
    # File/shell tools publicly accept canonical /workspace-rooted paths. Strip
    # only that exact capability root, then apply the same traversal jail as a
    # relative path. Other absolute paths remain forbidden.
    if raw == "/workspace":
        raw = "."
    elif raw.startswith("/workspace/"):
        raw = raw.removeprefix("/workspace/")
    norm = posixpath.normpath(raw)
    if norm in ("", "."):
        return "index.html" if app_root else None
    if norm.startswith("/") or norm == ".." or norm.startswith("../"):
        return None
    if app_root:
        base = norm.rstrip("/")
        if posixpath.basename(base) == "index.html":
            return base
        return posixpath.normpath(posixpath.join(base, "index.html"))
    return norm


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

# Tools that mutate the static deliverable on disk (mirror of `_is_web_deliverable`).
_FILE_WRITE_TOOLS = frozenset(
    {"file_write", "file_edit", "file_append", "file_replace_lines", "file_insert_lines"}
)
# Shell tools that can run a non-browser (HTMLParser/static-parse) validation.
_SHELL_TOOLS = frozenset({"shell", "shell_exec"})
# Verify-only step classification — STRUCTURAL, not a word list. The recurring
# false-positive class is a verification WORD appearing as a CONTENT noun: test→
# testimonials, render→product renders, validation→input validation, lint→lint config,
# check→checkout. Whack-a-moling individual words never converges, so a step is
# "verify-only" iff BOTH hold:
#   (A) it has a verification-ACTION framing (a verify verb acting on the deliverable,
#       or a clear verification-outcome phrase) — `_VERIFY_ACTION_RE`; AND
#   (B) it has NO creation/content verb at all — `_CONTENT_VERB_RE` is a hard NEGATIVE
#       OVERRIDE: a step that adds/creates/builds/sets-up/etc. is content work, never
#       verification, even when it also contains a verify-ish word ("Add input
#       validation", "Set up linting").
# Bare nouns alone (`validation`, `lint`, `render`, `test`, `check`) never match — only
# the action framing in (A) does. When in doubt → NON-verify (stay paused, the safe
# choice that can never false-finish real work).
_CONTENT_VERB_RE = re.compile(
    r"\b(?:add(?:s|ed|ing)?|create(?:s|d)?|creating|build(?:s|ing)?|built"
    r"|implement(?:s|ed|ing)?|writ(?:e|es|ing)|wrote|design(?:s|ed|ing)?"
    r"|styl(?:e|es|ed|ing)|mak(?:e|es|ing)|made|configure(?:s|d)?|configuring|config"
    r"|install(?:s|ed|ing)?|includ(?:e|es|ed|ing)|insert(?:s|ed|ing)?"
    r"|append(?:s|ed|ing)?|generat(?:e|es|ed|ing)|develop(?:s|ed|ing)?"
    r"|scaffold(?:s|ed|ing)?|integrat(?:e|es|ed|ing)|updat(?:e|es|ed|ing)"
    r"|fix(?:es|ed|ing)?|refactor(?:s|ed|ing)?|polish(?:es|ed|ing)?"
    r"|setup|set[\s-]?up|wire[\s-]?up)\b",
    re.IGNORECASE,
)
_VERIFY_ACTION_RE = re.compile(
    # (A1) a verification VERB (precise stems — `check(?:s|ed|ing)?` won't match
    # "checkout"/"checkbox"; `test(?:s|ed|ing)?` won't match "testimonials") followed
    # by a verification TARGET ("that/the/it/…") — "Verify the page", "Check that
    # links work", "Validate the HTML", "Ensure it renders".
    r"\b(?:verif(?:y|ies|ied|ying)|validat(?:e|es|ed|ing)|check(?:s|ed|ing)?"
    r"|confirm(?:s|ed|ing)?|ensure(?:s|d)?|ensuring|test(?:s|ed|ing)?"
    r"|review(?:s|ed|ing)?|inspect(?:s|ed|ing)?)\s+"
    r"(?:that|the|it|its|all|each|every|whether|if|for|no|cross)\b"
    # (A2) recognized standalone verification tokens/actions.
    r"|\bqa\b"
    r"|\bsmoke[\s-]?tests?\b"
    r"|\brun(?:s|ning)? (?:the |a )?(?:linter|lint|tests?|test suite|checks?)\b"
    # (A3) verification-OUTCOME phrases (a state asserted, not content created).
    r"|\b(?:renders?|displays?) (?:correctly|properly|as expected|fine|cleanly|well)\b"
    r"|\b(?:the )?(?:page|site|app|layout|content|everything|it) (?:renders?|displays?)\b"
    r"|\btests? pass(?:es|ed)?\b"
    r"|\blint(?:er)? pass(?:es|ed)?\b"
    r"|\bno console errors?\b",
    re.IGNORECASE,
)
# A shell command counts as a REAL non-browser CONTENT/STRUCTURE validation only when
# it STRUCTURALLY invokes a markup parser/validator (the parser is the EXECUTABLE/module
# actually run) or a content grep that reads index.html — NOT merely because a word like
# "validate"/"markup" appears in the text (`echo validate index.html` must NOT count —
# the codex catch), and never a bare existence/dump (`ls`/`test -f`/`stat`/`cat`/`wc`).
# Commands whose first token is a no-op/echo are excluded outright; see
# `_is_real_validation_command`.
# First token = a dedicated markup validator/linter invoked directly.
_VALIDATOR_EXECUTABLES = frozenset(
    {"xmllint", "tidy", "html5validator", "html5check", "vnu", "htmlhint", "htmllint"}
)
# First token = a content-search tool (a grep/assertion that actually reads the file).
_GREP_EXECUTABLES = frozenset({"grep", "egrep", "fgrep", "rg", "ripgrep", "ag"})
# First token = a python interpreter (only a real parser-module invocation counts).
_PYTHON_EXECUTABLES = frozenset({"python", "python3", "py", "python2"})
# First token = an explicit no-op / text-echo / existence-or-dump → NEVER a validation.
_NONVALIDATION_EXECUTABLES = frozenset(
    {
        "echo",
        "printf",
        ":",
        "true",
        "false",
        "cat",
        "ls",
        "stat",
        "test",
        "[",
        "[[",
        "wc",
        "file",
        "head",
        "tail",
        "touch",
        "cp",
        "mv",
        "rm",
        "dd",
        "tee",
    }
)
# A python -c/-m body that actually IMPORTS/USES an HTML/XML parser or markup validator.
_PYTHON_PARSER_RE = re.compile(
    r"htmlparser|html\.parser|html5lib|html5validator|\blxml\b|beautifulsoup|\bbs4\b"
    r"|xml\.etree|elementtree|\betree\b|xmllint|markupsafe",
    re.IGNORECASE,
)


def _is_real_validation_command(cmd: str) -> bool:
    """True iff `cmd` STRUCTURALLY runs a content/structure validation against
    index.html — the invoked tool is a markup parser/validator (or a grep that reads
    the file), not a word echoed in text. Rejects `echo validate index.html`,
    `printf "markup" index.html`, no-ops, and bare existence/dump commands."""
    cmd = cmd.strip()
    if "index.html" not in cmd:
        return False
    tokens = cmd.split()
    if not tokens:
        return False
    first = tokens[0].rsplit("/", 1)[-1]  # strip any leading path
    if first.startswith("#") or first in _NONVALIDATION_EXECUTABLES:
        return False
    if first in _VALIDATOR_EXECUTABLES or first in _GREP_EXECUTABLES:
        return True
    if first in _PYTHON_EXECUTABLES:
        # Must be a -c/-m invocation (actually executing code) AND name a real parser
        # module — `python -c "print('validate')"` must NOT pass.
        ran_code = bool(re.search(r"(?:^|\s)-[cm]\b", cmd))
        return ran_code and bool(_PYTHON_PARSER_RE.search(cmd))
    return False


def _shell_command_text(action: ActionEvent) -> str:
    """The command string a shell action ran — preferring the canonical command arg
    (so the first-token executable analysis is reliable), else the joined values."""
    args = action.tool_call.arguments or {}
    for key in ("command", "cmd", "script", "code"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return " ".join(str(v) for v in args.values())


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
            if st.get("console_errors") or st.get("network_failures"):
                return True
            http_ok = 200 <= int(st.get("http_status") or 0) < 400
            if http_ok and st.get("meaningful_content") is False:
                return True  # served HTTP 200 but rendered nothing → broken, not unverifiable
        elif res.tool_name == "browser":
            if not res.success:
                continue  # a failed/unavailable browser call is infra, not app-broken
            # console errors — the app threw at runtime.
            if any(c.get("level") == "error" for c in (st.get("console") or [])):
                return True
            # NETWORK failures — the daemon's `network` ring holds failed/4xx-5xx
            # requests (B7). A page that loaded with broken requests is NOT a clean
            # unverifiable delivery.
            if st.get("network") or st.get("network_failures"):
                return True
            # BLANK render — the page served but nothing a user would see mounted
            # (empty/trivial DOM: no meaningful text, no elements/links/forms).
            if not _browser_content_meaningful(st):
                return True
    return False


def _url_targets_preview(url: str, target_key: tuple[str, int] | None) -> bool:
    """Does a browser observation `url` address the CURRENT preview target?

    The preview platform assigns a RANDOM port — there is NO fixed :8000 inside the
    sandbox. When the live preview port has been resolved (`target_key`, derived from
    `_detect_preview_url` via `_preview_key` — backend-aware: never the agent-server's
    :8000 on the shared-host/process backend) an observation counts ONLY if its url
    resolves to the SAME (host, port) preview key — a foreign / wrong-port observation
    does NOT satisfy the browser gate.

    When the preview is undetectable (`target_key is None` — sandbox-less / legacy
    backend / the pure-reader unit tests) the historical :8000 acceptance is kept as a
    safe fallback rather than asserting a wrong port (on an ISOLATED backend :8000 IS
    the app; the resolver returns it as the target_key there, so this also matches)."""
    if target_key is None:
        return url.startswith("http://127.0.0.1:8000") or url.startswith("http://localhost:8000")
    return _preview_key(url) == target_key


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
    probe_action_ids = {
        ev.id for ev in events if isinstance(ev, ActionEvent) and ev.meta.get("verify_probe")
    }
    valid_obs = []
    for ev in events:
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if isinstance(ev, ObservationEvent) and ev.tool_result.tool_name == "browser":
            if ev.action_id in probe_action_ids:
                continue  # the gate's own probe — judged actively, not as agent work
            res = ev.tool_result
            if res.success and res.structured:
                url = str(res.structured.get("url", ""))
                if _url_targets_preview(url, target_key):
                    valid_obs.append(res.structured)

    if not valid_obs:
        return False, None

    # ok = at least one observation with zero console errors
    ok = any(
        not any(c.get("level") == "error" for c in obs.get("console", [])) for obs in valid_obs
    )

    # first_error_line = from the LATEST qualifying-URL observation that has error-level entries
    first_error_line = None
    for obs in reversed(valid_obs):
        errors = [
            str(c.get("text", "")) for c in obs.get("console", []) if c.get("level") == "error"
        ]
        if errors:
            first_error_line = errors[0]
            break

    return ok, first_error_line


def _browser_content_meaningful(structured: dict) -> bool:
    """Blank-render guard for the ACTIVE finish probe. A page can serve HTTP 200
    with a CLEAN console yet render nothing a user would see — an SPA whose JS
    never mounted, or an empty shell. `_browser_verified` (console-errors only)
    passes that page; this guard catches it.

    Meaningful iff there is real visible content (title + body text >= 20 chars
    combined), a non-empty browser-rendered ``text/plain`` document, rendered
    content-bearing semantics, OR interactive structure. The
    browser daemon reports ``visible_semantic_elements`` after checking geometry,
    viewport intersection, and computed visibility for content-bearing headings.
    This distinguishes a short but valid rendered heading from an empty SPA shell.
    The browser daemon's element walker
    (`_get_elements`) indexes links/forms/buttons/inputs/clickables, so a
    non-empty `elements` list means the page has actionable structure even when
    its text is sparse. (`links`/`forms` count fields are honored too if a daemon
    variant supplies them.)"""
    title = str(structured.get("title", "") or "")
    text = str(structured.get("text", "") or "")
    if len((title + " " + text).strip()) >= 20:
        return True
    # A plain-text HTTP response is itself the served document, not an empty SPA
    # shell.  ``document_content_type`` is captured from browser-owned main-response
    # metadata, and the body text is rendered DOM/accessibility evidence.  Keep
    # the sparse exception exact to text/plain so a short HTML loading placeholder
    # does not bypass the existing blank-render guard.
    raw_content_type = structured.get("document_content_type")
    if (
        isinstance(raw_content_type, str)
        and raw_content_type.split(";", 1)[0].strip().lower() == "text/plain"
        and bool(text.strip())
    ):
        return True
    semantic_count = structured.get("visible_semantic_elements")
    if (
        isinstance(semantic_count, int)
        and not isinstance(semantic_count, bool)
        and semantic_count > 0
    ):
        return True
    elements = structured.get("elements")
    if isinstance(elements, list) and len(elements) > 0:
        return True
    if structured.get("links") or structured.get("forms"):
        return True
    return False


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

# Preview ports the user-visible deliverable may serve on (ordered by preference;
# 8000 is Disco's canonical user-visible port INSIDE an isolated sandbox). SINGLE
# SOURCE OF TRUTH now lives in `preview_target.PREVIEW_PORTS` alongside the
# backend-aware resolver; re-exported here under the historical name so the
# `verify_web_app` tool's existing `from ...finish import _PREVIEW_PORTS` keeps
# working (the gate's preview detection and the tool's auto-detect share BOTH the
# port set AND the resolver).
_PREVIEW_PORTS: tuple[int, ...] = PREVIEW_PORTS


def _preview_key(url: str) -> tuple[str, int] | None:
    """Normalize a preview URL to a comparable (host, port) key. Loopback aliases
    (localhost / 127.0.0.1 / 0.0.0.0 / ::1 / empty host) collapse to one host so a
    verdict on http://localhost:8000/ matches a target of http://127.0.0.1:8000/.
    Returns None for an empty / unparseable URL (a verdict with no usable url can
    never bind to a target)."""
    from urllib.parse import urlsplit

    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "http://" + u
    try:
        parts = urlsplit(u)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", ""):
        host = "127.0.0.1"
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (host, port)


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


class _FinishGateProto:
    if TYPE_CHECKING:
        _loop: AgentLoop

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
