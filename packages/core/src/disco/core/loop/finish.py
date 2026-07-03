"""The finish path: verify-on-finish, the C1c DoD gate, the plan-mode execution
nudge, the browser-verify gate, and the auto-continue finalizer.

Extracted from engine.py as a stateful collaborator: `FinishGate` holds a
back-ref to its `AgentLoop` and runs the affirmative-finish pipeline. The
module-level verify-command builders, the web-deliverable / browser-verify
helpers, the finish-cap constants, and the `_DoDWorkspaceUnavailable` exception
moved here too (re-exported from engine for back-compat). Method bodies are
byte-identical with `self.` rewritten to `self._loop.` (sibling calls stay
in-collaborator).
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from typing import TYPE_CHECKING, Any, cast

from ..contract.export_render import (
    EXPORT_GATE_MAX_REFUSALS,
    ExportRenderFacts,
    count_export_gate_refusals,
    export_gate_refusal_reminder,
    export_gate_release_warning,
    export_render_facts_for_path,
    latest_export_render_facts,
    latest_export_render_index,
)
from ..dod import FileExistsPredicate
from ..dod_evaluator import DoDEvaluator, HttpProbeResult
from ..env import disco_env
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    DeliverableEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    ToolCall,
    VerifierShadowEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
)
from ..llm import OperatingMode
from ..state import ConversationState
from ..view import effective_plan_progress
from . import signals
from .boundaries import AgentStep, HostVerificationDeliverable
from .control import Disp
from .plan_conditions import (
    DictatedContentCondition,
    dictated_content_conditions_from_events,
)
from .preview_target import (
    PREVIEW_PORTS,
    backend_shares_host_network,
    parse_port_ownership,
    port_ownership_probe_command,
    resolve_preview_port,
)
from .signals import _NON_PRODUCTIVE_TOOLS

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

# finish-verify cap (issue B). The model-authored `verify` gate was the ONLY uncapped
# gate in the loop — a broken/always-failing verify command could refuse `finish`
# forever. Mirror the existing _browser_verify_refusals release valve: after N REAL
# failures, finish anyway with a LOUD warning (the failure stays visible — important
# for weak local models — rather than grinding to max_iterations). A MALFORMED verify
# (SyntaxError / command-not-found) is a broken check, not a failed task, so it is
# auto-stripped (bounded by _finish_verify_strips so it can't be gamed as a free finish).
_FINISH_VERIFY_CAP = 3

# C1c DoD-gate refusal cap. The external Definition-of-Done gate refuses `finish`
# while the spec is unmet, but — exactly like the verify cap above — it MUST be
# bounded: an agent that cannot satisfy the external DoD would otherwise be
# trapped in an unbounded refuse-and-continue loop, accumulating events without
# end (this is the OOM the uncapped first cut caused). After N consecutive
# refusals the gate RELEASES (finish lands) with a LOUD warning; the prior
# refusal events remain the visible audit trail.
_DOD_REFUSAL_CAP = 3

# REL-RC-O — quoted user literals are hard content floors at finish, but the
# refusal must be bounded so a model that cannot repair does not deadlock.
_DICTATED_CONTENT_REFUSAL_CAP = 3

_HOST_VERIFY_AUTHORITATIVE_FLAG = "HOST_VERIFY_AUTHORITATIVE"
_FALSY = frozenset({"0", "false", "no", "off"})


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
        "(len(b.strip())>=20 or sys.exit('empty body from '+u));"
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


def _is_web_deliverable(events: list[Event]) -> bool:
    """Web deliverable if index.html was written/edited OR port 8000 owned by non-preview.
    Derived from events to keep the check pure (event-list in, verdict out)."""
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
                if path in ("index.html", "./index.html"):
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
    for ev in events:
        if not isinstance(ev, PlanEvent):
            continue
        for step in ev.steps:
            dc = getattr(step, "done_condition", None)
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
        p = _safe_deliverable_file_path(
            ev.path,
            app_root=(ev.artifact_kind == "app"),
        )
        if p is not None:
            out.append(p)
    return out


def _latest_app_deliverable_event(events: list[Event]) -> DeliverableEvent | None:
    for ev in reversed(events):
        if isinstance(ev, DeliverableEvent) and ev.artifact_kind == "app":
            return ev
    return None


def _latest_deliverable_event(events: list[Event]) -> DeliverableEvent | None:
    for ev in reversed(events):
        if isinstance(ev, DeliverableEvent):
            return ev
    return None


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
    {"echo", "printf", ":", "true", "false", "cat", "ls", "stat", "test", "[", "[[",
     "wc", "file", "head", "tail", "touch", "cp", "mv", "rm", "dd", "tee"}
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
        return url.startswith("http://127.0.0.1:8000") or url.startswith(
            "http://localhost:8000"
        )
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
    combined) OR interactive structure. The browser daemon's element walker
    (`_get_elements`) indexes links/forms/buttons/inputs/clickables, so a
    non-empty `elements` list means the page has actionable structure even when
    its text is sparse. (`links`/`forms` count fields are honored too if a daemon
    variant supplies them.)"""
    title = str(structured.get("title", "") or "")
    text = str(structured.get("text", "") or "")
    if len((title + " " + text).strip()) >= 20:
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
    return (
        disco_env("DRIVER_VISION") == "1"
        or bool(disco_env("VISION_ESCALATION_MODEL"))
    )


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
            if res.success and res.structured and _verdict_targets_preview(
                res.structured, target_url
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


def _prior_verify_marker_fp(events: list[Event], since_seq: int) -> str | None:
    """failure_fingerprint stamped on the most recent `verify_no_progress:<fp>`
    marker since `since_seq`. None when the gate has not yet refused for the
    current served output. If a later marker carries the SAME fingerprint as the
    current verdict, the gate has already nudged for this exact failure with no
    productive edit in between → no new information → stop (the loop breaker)."""
    for ev in reversed(events):
        if ev.seq is None or ev.seq <= since_seq:
            continue
        if isinstance(ev, StatusEvent) and ev.detail and ev.detail.startswith(
            _VERIFY_MARKER_PREFIX
        ):
            return ev.detail[len(_VERIFY_MARKER_PREFIX):]
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


class FinishGate:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def finish_verify_passed(self, command: str) -> tuple[bool, bool]:
        """Run the agent's stated acceptance check before allowing `finish`
        (verify-on-finish post-condition gate). The agent attaches a shell
        command to finish whose exit 0 means the deliverable is good; we run it,
        VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
        the real problem instead of declaring a broken build complete.

        The verify command is NOT privileged: it passes the same hard-deny gate
        AND the same confirmation policy as any action. A command that would
        normally require confirmation is refused here (we don't silently run a
        gated command as a 'verification') — the agent is told to run it as an
        ordinary, gated action first. Ordinary test/build/lint checks assess as
        MEDIUM and run unimpeded. Returns (passed, malformed): `passed` is True
        iff the check ran and passed; `malformed` is True iff the verify command
        itself is broken (command-not-found / SyntaxError) rather than the task."""
        call = ToolCall(tool_name="shell", arguments={"command": command})
        # meta marker: this shell action is the GATE'S probe, not the agent's
        # work. Phase-B re-run #6 (2026-06-10): an unmarked probe counted as a
        # real action in _actions_since_last_resume, so a refused first-move
        # finish UNLOCKED the withheld meta tools and the model remember-spammed
        # straight into the valve. The probe must never flip fresh-session.
        action = ActionEvent(
            thought=f"Verifying completion: {command}",
            tool_call=call,
            meta={"verify_probe": True},
        )

        deny = signals.hard_deny_reason(action)
        if deny is not None:
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"The verify command attached to finish is hard-denied ({deny}); it "
                        "will not run. Provide a safe verify command, or finish without one.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        risk = self._loop.analyzer.assess(action)
        if self._loop.policy.should_confirm(risk):
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "The verify command attached to finish needs confirmation to run "
                        "and won't be executed silently as a verification. Run that check "
                        "as a normal action first (it will go through the confirm gate), "
                        "then finish.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        # Find the observation correlated to THIS verify action (robust against a
        # trailing sandbox-restart notice that _execute_and_observe may append).
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        passed = isinstance(obs, ObservationEvent) and obs.tool_result.success
        # malformed = the verify COMMAND ITSELF is broken (not the deliverable):
        # command-not-found (127) or an interpreter SyntaxError. A non-zero exit from
        # an unrunnable check is NOT evidence the task failed — the carrier was bad.
        # The caller auto-strips a malformed verify rather than counting it as a
        # failed acceptance. Detected from the result text.
        malformed = False
        # Malformed = the verify CARRIER is broken, which only makes sense if the
        # shell actually RAN the command and reported it (an ObservationEvent). An
        # AgentErrorEvent means the executor raised BEFORE any observation (sandbox
        # down, transport error) — that's an environmental failure, NOT a malformed
        # verify, and must stay a real failure so it isn't auto-stripped into a false
        # "done". (Earlier this read AgentErrorEvent.error text and a stray "command
        # not found" substring there would wrongly strip an environmental failure.)
        if not passed and isinstance(obs, ObservationEvent):
            st = obs.tool_result.structured or {}
            ec = st.get("exit_code")
            exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
            low = f"{obs.tool_result.content or ''} {obs.tool_result.error or ''}".lower()
            # The shell couldn't find/parse the command: exit 127 (command-not-found,
            # authoritative from the structured result — locale-independent, can't be
            # faked by output text) or an interpreter SyntaxError. Use the real exit
            # code, NOT a regex over output: "exit 1, 127 tests failed" is a REAL
            # failure, not a malformed carrier, and must NOT become a false success.
            malformed = (
                exit_code == 127
                or "syntaxerror" in low
                # content fallbacks only when the structured exit code is unavailable
                or (exit_code is None and "command not found" in low)
                or (exit_code is None and ": not found" in low)
            )
        return passed, malformed

    async def _drive_finish_browser_probe(self, target_url: str | None = None) -> bool:
        """ACTIVE finish-verify: instead of TRUSTING the agent to have browsed the
        deliverable, the gate DRIVES a `browser navigate <preview>` itself and
        judges the result on ground truth. The preview platform assigns a RANDOM
        port, so the gate navigates to the RESOLVED preview (`target_url`, from
        `_detect_preview_url` — backend-aware, never the agent-server's :8000 on the
        shared-host backend); only when no preview is detectable does it fall back
        to the legacy `http://127.0.0.1:8000/` (the isolated-backend app port). This
        closes the
        verification-overclaim hole: an agent that declares done without ever
        looking can no longer land a JS-broken/blank page as FINISHED — the gate
        looks for it.

        Returns True iff the probe RAN and produced a usable browser observation
        (ObservationEvent.tool_result.success). The caller then re-judges via
        `_browser_verified` (console errors) + `_browser_content_meaningful`
        (blank render). Returns False (no-op) when the browser backend is
        unavailable (the process backend ships no `browser` tool) or the probe is
        hard-denied — so the gate degrades to its prior passive nudge/release
        behavior on browserless backends rather than hanging or false-refusing.

        The probe ActionEvent is tagged `meta={"verify_probe": True}` so signals.py
        keeps it OUT of agent-work accounting (actions_since_last_resume /
        productive_actions_since_approval) — the same marker finish_verify_passed
        uses. Running the gate's own probe is never evidence the AGENT did work."""
        # Browserless backend? (process backend exposes no `browser` tool) → no-op,
        # let the gate fall back to today's passive behavior.
        try:
            tool_names = {
                getattr(t, "name", None) for t in self._loop.executor.available_tools()
            }
        except Exception:  # noqa: BLE001 — any introspection failure → degrade safely
            tool_names = set()
        if "browser" not in tool_names:
            return False

        nav_url = target_url or "http://127.0.0.1:8000/"
        call = ToolCall(
            tool_name="browser",
            arguments={"action": "navigate", "url": nav_url},
        )
        action = ActionEvent(
            thought=f"Verifying the app renders: navigating to {nav_url}",
            tool_call=call,
            meta={"verify_probe": True},
        )
        # A hard-denied probe (should never happen for a browser navigate, but the
        # contract surface is shared) is a no-op → degrade to passive.
        if signals.hard_deny_reason(action) is not None:
            return False

        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        return isinstance(obs, ObservationEvent) and obs.tool_result.success

    def _available_tool_names(self) -> set[str | None]:
        try:
            return {
                getattr(t, "name", None) for t in self._loop.executor.available_tools()
            }
        except Exception:  # noqa: BLE001 — introspection failure → degrade safely
            return set()

    def _verify_tool_available(self) -> bool:
        """W-45 — is a structured app verifier in the execution set?"""
        return self._active_verify_tool() is not None

    def _active_verify_tool(self) -> str | None:
        """Return the structured verifier active for this executor.

        Strict AppKit mode advertises `verify_appkit_app` from the AppKit-only
        allowlist. Prefer it; otherwise fall back to normal Build's `verify_web_app`.
        """
        tool_names = self._available_tool_names()
        if "verify_appkit_app" in tool_names:
            return "verify_appkit_app"
        if "verify_web_app" in tool_names:
            return "verify_web_app"
        return None

    async def _host_artifact_file_exists(self, path: str) -> bool | None:
        sbx = getattr(self._loop.executor, "sandbox", None)
        file_exists: Any = getattr(sbx, "file_exists", None)
        if file_exists is None:
            return None
        try:
            return bool(await file_exists(path))
        except Exception:  # noqa: BLE001 — path resolution is advisory; skip only on known absence
            return None

    async def _host_verify_artifact_path(self, raw_path: str) -> str | None:
        path = _safe_deliverable_file_path(raw_path, app_root=True)
        if path is None:
            return None
        raw = (raw_path or "").strip()
        norm = posixpath.normpath(raw) if raw else "."
        directory_like = (
            norm in ("", ".")
            or posixpath.basename(norm.rstrip("/")) != "index.html"
        )
        if directory_like and await self._host_artifact_file_exists(path) is False:
            return None
        return path

    def _host_verify_authoritative(self) -> bool:
        return bool(getattr(self._loop, "_host_verify_authoritative", False))

    async def _host_verify_deliverable(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        include_unverifiable: bool = False,
    ) -> HostVerificationDeliverable | None:
        """REL-1c — reconstruct the web-like deliverable for host shadow verify.

        The host verifier is advisory in this PR, so missing/ambiguous delivery
        evidence simply skips the shadow path. A first-class app handoff wins;
        otherwise the existing web-finish convention (index.html/server_status) is
        treated as index.html. App-root handoffs such as "." or "dist" are
        resolved to their primary index.html-style file, never verified as the
        directory path itself.
        """

        app_event = _latest_app_deliverable_event(events)
        any_event = _latest_deliverable_event(events) if include_unverifiable else app_event
        if any_event is None and not _is_web_deliverable(events):
            return None
        if any_event is not None and any_event.artifact_kind != "app":
            path = _safe_deliverable_file_path(any_event.path)
        else:
            path = await self._host_verify_artifact_path(
                any_event.path if any_event is not None else "index.html"
            )
        if path is None:
            return None
        deployment_url = any_event.deployment_url if any_event is not None else ""
        artifact_kind = any_event.artifact_kind if any_event is not None else "app"
        return HostVerificationDeliverable(
            conversation_id=self._loop.conversation_id,
            artifact_path=path,
            artifact_kind=artifact_kind,
            deployment_url=deployment_url or "",
            requested_verification=step.requested_verification,
        )

    @staticmethod
    def _verdict_label(verdict: dict | None) -> str | None:
        if not verdict:
            return None
        label = verdict.get("verdict")
        if label is not None:
            return str(label)
        if "passed" in verdict:
            return "pass" if verdict.get("passed") else "fail"
        return None

    @staticmethod
    def _verdict_failures(verdict: dict) -> list[dict[str, object]]:
        failures: list[dict[str, object]] = []
        for err in verdict.get("console_errors") or []:
            if isinstance(err, dict):
                failures.append({"kind": "console_error", **err})
        for err in verdict.get("network_failures") or []:
            if isinstance(err, dict):
                failures.append({"kind": "network_failure", **err})
        if not verdict.get("passed") and not failures:
            summary = str(verdict.get("summary") or verdict.get("detail") or "").strip()
            if summary:
                failures.append({"kind": "summary", "message": summary})
        return failures

    @staticmethod
    def _host_unavailable_verdict(
        deliverable: HostVerificationDeliverable, detail: str
    ) -> dict[str, object]:
        return {
            "passed": False,
            "verdict": "unavailable",
            "url": deliverable.deployment_url,
            "http_status": 0,
            "summary": detail,
            "next_action": "",
            "console_errors": [],
            "network_failures": [],
            "failure_fingerprint": "host_verifier_unavailable",
        }

    @staticmethod
    def _host_unverifiable_verdict(
        deliverable: HostVerificationDeliverable,
    ) -> dict[str, object]:
        return {
            "passed": False,
            "verdict": "unverifiable",
            "url": deliverable.deployment_url,
            "http_status": 0,
            "summary": (
                f"No host validator is available for artifact kind "
                f"{deliverable.artifact_kind!r}; verification was not claimed."
            ),
            "next_action": "",
            "console_errors": [],
            "network_failures": [],
            "failure_fingerprint": f"host_verifier_unverifiable:{deliverable.artifact_kind}",
        }

    @staticmethod
    def _verdict_first_failure(verdict: dict) -> str:
        errs = verdict.get("console_errors") or []
        nets = verdict.get("network_failures") or []
        if errs:
            e0 = errs[0]
            if isinstance(e0, dict):
                where = f" @ {e0.get('source')}" if e0.get("source") else ""
                return f"{e0.get('text', '')}{where}".strip()
        if nets:
            n0 = nets[0]
            if isinstance(n0, dict):
                marker = n0.get("status") or n0.get("failure") or "failed"
                return f"{n0.get('method', 'GET')} {n0.get('url', '')} -> {marker}".strip()
        failures = FinishGate._verdict_failures(verdict)
        if failures:
            message = failures[0].get("message")
            if message:
                return str(message)
        return ""

    async def _host_verify_failure_disposition(
        self, deliverable: HostVerificationDeliverable, verdict: dict
    ) -> Disp:
        label = self._verdict_label(verdict) or "fail"
        summary = str(
            verdict.get("summary")
            or verdict.get("detail")
            or "host verifier did not pass"
        )
        next_action = str(verdict.get("next_action") or "")
        first_failure = self._verdict_first_failure(verdict)
        await self._record_verifier_failure_to_context(
            message=(first_failure or summary), rel_path=None
        )
        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            payload = (
                "<system-reminder>\n"
                f"Host verification did not pass for {deliverable.artifact_kind} "
                f"artifact {deliverable.artifact_path!r} ({label}). {summary}\n"
                + (f"first failure: {first_failure}\n" if first_failure else "")
                + (
                    f"next step: {next_action}\n"
                    if next_action
                    else "Fix the issue surfaced by the host verifier, then finish again.\n"
                )
                + "The task is NOT complete until the host verifier passes.\n"
                "</system-reminder>"
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=payload),
                )
            )
            return Disp.CONTINUE

        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        warn = (
            "⚠ Finished WITHOUT a passing host verifier verdict (3 attempts) — "
            f"the deliverable is UNVERIFIED and may be INCOMPLETE. {summary}"
            + (f" Outstanding: {next_action}" if next_action else "")
            + " Note this clearly in your summary."
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=warn),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def gate_host_verify(self, step: AgentStep, events: list[Event]) -> Disp:
        """REL-1c/1e — run the host verifier and emit audit telemetry.

        Authoritative (the default since 2026-07-03): a real host ``fail`` on an
        app deliverable refuses the finish (bounded by the 3-refusal release
        valve); ``unavailable`` (verifier infrastructure could not run) degrades
        to FALLTHROUGH so the inline browser gate stays the enforcement path on
        browserless installs; non-app/no-validator handoffs record an honest
        ``unverifiable`` verdict without claiming verification. With
        DISCO_HOST_VERIFY_AUTHORITATIVE explicitly off (REL-1c shadow posture)
        this gate is FALLTHROUGH-only telemetry, preserving the inline
        self-verify/browser gate as sole enforcement.
        """

        authoritative = self._host_verify_authoritative()
        host_verifier = getattr(self._loop, "_host_verifier", None)
        if host_verifier is None and not authoritative:
            return Disp.FALLTHROUGH

        deliverable = await self._host_verify_deliverable(
            step, events, include_unverifiable=authoritative
        )
        if deliverable is None:
            return Disp.FALLTHROUGH

        await self._loop._emit(
            VerifierStartedEvent(
                artifact_path=deliverable.artifact_path,
                artifact_kind=deliverable.artifact_kind,
                requested_by_event_id=None,
                meta={"requested_verification": deliverable.requested_verification},
            )
        )

        if deliverable.artifact_kind != "app":
            if not authoritative:
                return Disp.FALLTHROUGH
            host_verdict = self._host_unverifiable_verdict(deliverable)
        elif host_verifier is None:
            return Disp.FALLTHROUGH
        else:
            try:
                raw_host_verdict = await asyncio.wait_for(
                    host_verifier.verify(deliverable),
                    timeout=max(
                        0.001,
                        float(getattr(self._loop, "_host_verify_timeout_s", 30.0)),
                    ),
                )
                if not isinstance(raw_host_verdict, dict):
                    host_verdict = self._host_unavailable_verdict(
                        deliverable,
                        "verification could not run: host verifier did not return a usable verdict.",
                    )
                else:
                    host_verdict = raw_host_verdict
            except asyncio.TimeoutError:
                host_verdict = self._host_unavailable_verdict(
                    deliverable,
                    "verification could not run: host verifier timed out.",
                )
            except Exception as exc:  # noqa: BLE001 — shadow verifier never crashes finish flow
                _LOG.warning(
                    "REL-1c host verifier failed for %s:%s",
                    self._loop.conversation_id,
                    deliverable.artifact_path,
                    exc_info=True,
                )
                host_verdict = self._host_unavailable_verdict(
                    deliverable,
                    f"verification could not run: host verifier failed ({exc}).",
                )

        inline_verdict = _latest_verify_verdict(
            events,
            _last_productive_seq(events),
            target_url=None,
            tool_name=self._active_verify_tool() or "verify_web_app",
        )
        host_label = self._verdict_label(host_verdict)
        inline_label = self._verdict_label(inline_verdict)
        agreement = (
            host_label == inline_label
            if host_label is not None and inline_label is not None
            else None
        )
        detail = str(host_verdict.get("summary") or host_verdict.get("detail") or "")

        await self._loop._emit(
            VerifierShadowEvent(
                artifact_path=deliverable.artifact_path,
                artifact_kind=deliverable.artifact_kind,
                inline_verdict=inline_label,
                host_verdict=host_label,
                agreement=agreement,
                detail=detail or None,
                meta={"requested_verification": deliverable.requested_verification},
            )
        )
        verdict_event = await self._loop._emit(
            VerifierVerdictEvent(
                artifact_path=deliverable.artifact_path,
                artifact_kind=deliverable.artifact_kind,
                verified=bool(host_verdict.get("passed")),
                verdict=host_label,
                detail=detail or None,
                failures=self._verdict_failures(host_verdict),
                meta={"requested_verification": deliverable.requested_verification},
            )
        )
        hook = getattr(self._loop, "_host_verifier_verdict_hook", None)
        if hook is not None:
            try:
                await hook(cast(VerifierVerdictEvent, verdict_event))
            except Exception:  # noqa: BLE001 — REL-1d canary bookkeeping is non-authoritative
                _LOG.warning(
                    "REL-1d host verifier canary hook failed for %s:%s",
                    self._loop.conversation_id,
                    deliverable.artifact_path,
                    exc_info=True,
                )
        if authoritative and deliverable.artifact_kind == "app":
            if host_verdict.get("passed"):
                self._loop._browser_verify_refusals = 0
                return Disp.FALLTHROUGH
            if host_label == "unavailable":
                # Missing host-verifier infrastructure is not proof the app is broken;
                # fall through so the inline browser gate remains the enforcement path.
                return Disp.FALLTHROUGH
            return await self._host_verify_failure_disposition(deliverable, host_verdict)
        return Disp.FALLTHROUGH

    def _contract_required_deliverable_paths(self) -> list[str]:
        """Best-effort bridge from a contract finalizer alias to required files.

        The loop only stores the finalizer alias, not the whole contract. When
        present, match it against the registry and reuse the contract's required
        files as primary deliverable candidates. No alias/no match is inert.
        """

        alias = getattr(self._loop, "_finish_alias", None)
        if not alias:
            return []
        try:
            from ..contract.registry import BuildContractRegistry

            reg = BuildContractRegistry.default()
            out: list[str] = []
            for kind in reg.kinds():
                c = reg.get(kind)
                if c is None or c.verify.finalizer != alias:
                    continue
                for p in c.artifact.required_files:
                    safe = _safe_deliverable_file_path(p)
                    if safe is not None:
                        out.append(safe)
            return out
        except Exception:  # noqa: BLE001 — finish gate degrades to other path sources
            return []

    async def _dictated_content_deliverable_paths(self, events: list[Event]) -> list[str]:
        """Primary deliverable files for dictated-content checking.

        Sources are deliberately narrow: files named by Plan/DoD/contract
        deliverable declarations, explicit handoff events, plus the existing web
        finish-gate convention that an index.html write makes a static web
        deliverable. This avoids scanning arbitrary workspace files.
        """

        paths: list[str] = []
        paths.extend(_plan_file_exists_paths(events))
        paths.extend(_deliverable_event_paths(events))
        spec = await self._loop.store.get_dod_spec(self._loop.conversation_id)
        if spec is not None:
            for pred in spec.predicates:
                if isinstance(pred, FileExistsPredicate):
                    p = _safe_deliverable_file_path(pred.path)
                    if p is not None:
                        paths.append(p)
        paths.extend(self._contract_required_deliverable_paths())
        if _is_web_deliverable(events):
            paths.append("index.html")

        out: list[str] = []
        seen: set[str] = set()
        for path in paths:
            norm = posixpath.normpath(path)
            if norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
        return out

    async def _read_deliverable_bytes(self, path: str) -> bytes | None:
        """Read one deliverable file, returning None only when no host/sandbox
        read surface is available. Missing/empty files return b"" so the content
        condition fails loudly against the named file."""

        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is not None and hasattr(sbx, "read_file"):
            try:
                data = await sbx.read_file(path)
            except FileNotFoundError:
                return b""
            except Exception as exc:  # noqa: BLE001 — gate is best-effort if read infra breaks
                _LOG.warning(
                    "dictated-content read failed for %s:%s via sandbox: %s",
                    self._loop.conversation_id,
                    path,
                    exc,
                )
                return b""
            if isinstance(data, bytes):
                return data
            return str(data).encode("utf-8", "surrogatepass")

        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            return None
        from pathlib import Path

        try:
            root = Path(workspace).resolve()
            candidate = (root / path).resolve()
            root_s = str(root)
            cand_s = str(candidate)
            if not (cand_s == root_s or cand_s.startswith(root_s.rstrip("/") + "/")):
                return b""
            if not candidate.is_file():
                return b""
            return candidate.read_bytes()
        except OSError as exc:
            _LOG.warning(
                "dictated-content read failed for %s:%s from workspace: %s",
                self._loop.conversation_id,
                path,
                exc,
            )
            return b""

    async def _first_dictated_content_miss(
        self,
        conditions: list[DictatedContentCondition],
        paths: list[str],
    ) -> tuple[DictatedContentCondition, list[str]] | None:
        readable = False
        contents: list[tuple[str, bytes]] = []
        for path in paths:
            data = await self._read_deliverable_bytes(path)
            if data is None:
                continue
            readable = True
            contents.append((path, data))
        if not readable:
            _LOG.warning(
                "dictated-content conditions present for %s, but no readable "
                "deliverable file surface is available; skipping gate.",
                self._loop.conversation_id,
            )
            return None

        for cond in conditions:
            needle = cond.literal.encode("utf-8", "surrogatepass")
            if any(needle in data for _, data in contents):
                continue
            checked = [path for path, _ in contents] or paths
            return cond, checked
        return None

    async def dictated_content_gate_passed(self, events: list[Event]) -> bool:
        """REL-RC-O finish gate: quoted user literals must be present verbatim.

        Conditions are reconstructed from USER messages bound to PlanEvent
        revisions. The current revision inherits all prior revisions, so a later
        phase cannot silently drop content quoted earlier in the conversation.
        """

        if not self._loop._planning_tools or self._loop.mode == OperatingMode.PLANNING:
            return True
        current_revision = _latest_plan_revision(events)
        if current_revision is None:
            return True
        conditions = [
            c
            for c in dictated_content_conditions_from_events(events)
            if c.revision <= current_revision
        ]
        if not conditions:
            self._loop._dictated_content_refusals = 0
            return True
        paths = await self._dictated_content_deliverable_paths(events)
        if not paths:
            return True

        miss = await self._first_dictated_content_miss(conditions, paths)
        if miss is None:
            self._loop._dictated_content_refusals = 0
            return True

        cond, checked_paths = miss
        file_word = "file" if len(checked_paths) == 1 else "files"
        files = ", ".join(f"`{p}`" for p in checked_paths)
        literal = cond.literal

        if self._loop._dictated_content_refusals >= _DICTATED_CONTENT_REFUSAL_CAP:
            if self._loop._dictated_content_refusals == _DICTATED_CONTENT_REFUSAL_CAP:
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="dictated_content_release",
                    )
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Finished despite missing dictated content after "
                                f"{self._loop._dictated_content_refusals} refusals: "
                                f"literal {literal!r} from plan revision {cond.revision} "
                                f"still was not found in deliverable {file_word} {files}. "
                                "Releasing the finish gate to avoid an unbounded loop; "
                                "the deliverable may fail content review."
                            ),
                        ),
                    )
                )
                self._loop._dictated_content_refusals += 1
            return True

        self._loop._dictated_content_refusals += 1
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You called finish, but a quoted user literal is missing "
                        f"from the deliverable {file_word} {files}: {literal!r}.\n\n"
                        f"This literal was dictated in the user instruction for plan "
                        f"revision {cond.revision} and is carried forward into the "
                        "current revision. The match is case-sensitive and exact. "
                        "Update the deliverable so it contains that exact text, then "
                        "finish again.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return False

    async def _detect_preview_url(self) -> str | None:
        """P1-1 — detect the URL the live deliverable currently serves on, using the
        SAME backend-aware resolver the `verify_web_app` tool uses
        (`preview_target.resolve_preview_port`). Duck-typed over the executor's
        sandbox (`exec_shell`): core never imports `tools`, so the gate runs the
        shared in-sandbox ownership probe and feeds the result to the shared
        resolver.

        On a SHARED-host backend (process/local — `sandbox.workspace_path` is set)
        the resolver NEVER returns a reserved control port (8000 = the agent-server):
        it prefers a CONVERSATION-OWNED served port, else any non-reserved owned
        port, else None — so a stale verdict about `:8000` (the agent-server, Bug 7)
        can never bind the finish gate. On an ISOLATED backend (gVisor/Podman —
        `workspace_path` is None) `:8000` IS the app, so the legacy first-reachable
        socket probe is kept.

        Returns `http://127.0.0.1:<port>/` or None (no sandbox / no exec_shell /
        nothing detected), in which case binding is NOT enforced (see
        `_verdict_targets_preview`) and the gate drives a fresh verify against the
        real preview. Never raises (a detection failure must not wedge the gate)."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None or not hasattr(sbx, "exec_shell"):
            return None
        host_shared = backend_shares_host_network(sbx)
        if host_shared:
            # Process backend (shares host net): ownership-aware — never bind the
            # agent-server's 8000. Isolated containers fall to the branch below where
            # 8000 IS the app (the old `workspace_path` heuristic wrongly sent them here).
            try:
                res = await sbx.exec_shell(
                    port_ownership_probe_command(_PREVIEW_PORTS), timeout_s=10
                )
            except Exception:  # noqa: BLE001 — detection failure → no binding (degrade safe)
                return None
            owned = parse_port_ownership(str(getattr(res, "stdout", "") or ""))
            port = resolve_preview_port(
                host_shared=True,
                owned=owned,
                conversation_id=str(getattr(sbx, "conversation_id", "") or ""),
            )
            return f"http://127.0.0.1:{port}/" if port is not None else None

        # Isolated backend: 8000 is the app inside the box — first reachable wins.
        import shlex

        ports = list(_PREVIEW_PORTS)
        script = (
            "import socket,sys\n"
            f"for p in {ports!r}:\n"
            "    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
            "    s.settimeout(0.3)\n"
            "    try:\n"
            "        s.connect(('127.0.0.1',p))\n"
            "        print(p)\n"
            "        sys.exit(0)\n"
            "    except Exception:\n"
            "        pass\n"
            "    finally:\n"
            "        s.close()\n"
        )
        try:
            res = await sbx.exec_shell(
                f"python3 -c {shlex.quote(script)}", timeout_s=10
            )
        except Exception:  # noqa: BLE001 — detection failure → no binding (degrade safe)
            return None
        for line in str(getattr(res, "stdout", "") or "").splitlines():
            line = line.strip()
            if line.isdigit():
                return f"http://127.0.0.1:{int(line)}/"
        return None

    async def _drive_verify_web_app(
        self, target_url: str | None = None, tool_name: str = "verify_web_app"
    ) -> bool:
        """W-45 ACTIVE verify: DRIVE one structured verifier call when the agent
        declares done without a fresh verdict. Mirrors `_drive_finish_browser_probe`
        — the probe ActionEvent is tagged `verify_probe` so it never counts as
        agent work. Returns True iff the call produced a usable observation.

        When the gate has already resolved the real preview target (`target_url`,
        backend-aware — never the agent-server's 8000 on the process backend), it is
        passed EXPLICITLY so the tool verifies the build's actual served port instead
        of repeating its own auto-detect (Bug 7). None ⇒ `{}` ⇒ the tool auto-detects
        (which itself uses the same backend-aware resolver)."""
        call = ToolCall(tool_name=tool_name, arguments={"url": target_url} if target_url else {})
        action = ActionEvent(
            thought=f"Verifying the app: running {tool_name} on the running preview.",
            tool_call=call,
            meta={"verify_probe": True},
        )
        if signals.hard_deny_reason(action) is not None:
            return False
        await self._loop._emit(action)
        await self._loop._execute_and_observe(action)
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        return isinstance(obs, ObservationEvent) and obs.tool_result.success

    async def finish_dod_gate_passed(self) -> bool:
        """C1c DoD gate. Returns True iff the finish should be allowed.

        Algorithm (in order):
          1. Read the DoD spec from the store. None → no spec → True
             (LEGACY BYTE-IDENTICAL PATH — no events emitted, no state
             changed, control flow identical to pre-C1c).
          2. Build a DoDEvaluator. Factory-injected if the loop was
             constructed with one; otherwise build a default over the
             executor's sandbox workspace_root (or skip the gate if the
             sandbox doesn't expose a path — defensive, never a crash).
          3. Run the verdict against the spec.
          4. Verdict passed → True (the finish lands).
          5. Verdict failed → emit a MessageEvent carrying the SPECIFIC
             unmet predicates (visible to the agent AND the audit), bump
             the refusal streak, return False so the caller `continue`s.

        Visible: the refusal is a `<system-reminder>` MessageEvent with the
        spec fingerprint, the number of unmet predicates, and a per-
        predicate line naming kind + reason. Never silent: a refused finish
        ALWAYS leaves a trace event. Same shape as verify-on-finish's
        refusal, distinct content (the predicates are external, not the
        agent's own command)."""
        spec = await self._loop.store.get_dod_spec(self._loop.conversation_id)
        if spec is None:
            # LEGACY: no DoD spec for this conversation → the gate is a
            # no-op. Today's finish path is reproduced EXACTLY — no events,
            # no state change, no log query beyond a single SELECT. The
            # read is observable as a side-effect-free DB query; it does
            # NOT change the events, status transitions, or final state.
            return True
        # Spec exists → run the evaluator. The factory seam is the test
        # injection point (fakes for command_runner / http_probe); the
        # default builds a real DoDEvaluator over the executor's workspace.
        try:
            evaluator = await self.build_dod_evaluator()
        except _DoDWorkspaceUnavailable:
            # No workspace to grade against. This is a misconfiguration
            # (the spec was set but the sandbox doesn't expose a path),
            # not a predicate failure. We log and skip the gate rather
            # than refusing forever — refusing without a reason would
            # also be a silent failure mode. The audit trail will see the
            # log line; the agent sees no gate, so the run can finish.
            _LOG.warning(
                "DoD spec set for %s but no workspace_root available; "
                "skipping C1c gate (refusing without evidence would be a "
                "silent fail).",
                self._loop.conversation_id,
            )
            return True
        verdict = await evaluator.evaluate(spec, conversation_id=self._loop.conversation_id)
        if verdict.passed:
            self._loop._dod_refusals = 0  # clean pass → reset the streak (mirror verify)
            return True
        # INFRA-vs-TASK release (DoD v2.1): if EVERY unmet predicate is UNVERIFIABLE — the
        # check could not be RUN (a hard-denied command, an unprobeable/not-yet-serving URL,
        # egress denied) — do NOT block the finish on infra noise. Only a real TASK failure
        # (a command that RAN and exited wrong; a server that SERVED the wrong status) is a
        # genuine "not done". This is what lets `command`/`http_ok` predicates gate safely.
        failed = [r for r in verdict.results if not r.passed]
        task_failures = [r for r in failed if not getattr(r, "unverifiable", False)]
        if not task_failures:
            _LOG.info(
                "DoD for %s: %d unmet predicate(s), ALL unverifiable (infra, not a task "
                "verdict) — releasing the finish gate rather than blocking on a check that "
                "could not run.",
                self._loop.conversation_id,
                len(failed),
            )
            if failed:
                # HONEST-INCOMPLETE surface (the research's UNVERIFIED third state): we are
                # allowing finish, but the run did NOT verify these acceptance checks. Record
                # an ADVISORY note (visible, non-blocking, NOT a refusal) so the user/audit
                # sees "finished, but couldn't confirm X" instead of a silent clean pass.
                unverified = "\n".join(
                    f"  - {r.predicate!r}\n      could not verify: {r.reason}" for r in failed
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                f"<note>\nFinished, but {len(failed)} acceptance check(s) "
                                "could NOT be verified (the check could not run — e.g. a "
                                "denied command or a server that was not serving). These are "
                                "NOT failures, but they were NOT confirmed either:\n\n"
                                f"{unverified}\n</note>"
                            ),
                        ),
                        meta={"advisory": "dod_unverified_at_finish"},
                    )
                )
            self._loop._dod_refusals = 0
            return True
        # Cap the refusal streak (mirror _FINISH_VERIFY_CAP): after N consecutive
        # DoD refusals, RELEASE the gate so an agent that cannot satisfy the
        # external DoD is not trapped in an unbounded refuse-and-continue loop
        # (that loop accumulates events without end — the OOM the uncapped first
        # cut caused). The release is logged LOUDLY; the prior refusal events
        # remain the visible audit trail of the unmet predicates.
        if self._loop._dod_refusals >= _DOD_REFUSAL_CAP:
            _LOG.warning(
                "DoD for %s still unmet after %d refusals (cap %d) — releasing the "
                "finish gate to avoid an unbounded refuse loop.",
                self._loop.conversation_id,
                self._loop._dod_refusals,
                _DOD_REFUSAL_CAP,
            )
            return True
        # Refuse + keep working. The agent sees the SPECIFIC unmet
        # predicates (named by `kind` + the frozen-predicate `repr`); the
        # audit sees the spec fingerprint + the per-predicate results.
        self._loop._dod_refusals += 1  # bounded by _DOD_REFUSAL_CAP (see above)
        unmet_lines: list[str] = []
        for result in verdict.results:
            if result.passed or result.unverifiable:
                # Skip passes AND unverifiable (infra) failures — name only the actionable
                # TASK failures the agent can actually fix (we only reach here BECAUSE there
                # is at least one). An infra failure named here would mislead the model into
                # "fixing" something that simply could not be checked.
                continue
            # The frozen predicate's repr names kind + fields. Pair with
            # the verdict's reason (the human explanation).
            unmet_lines.append(f"  - {result.predicate!r}\n      reason: {result.reason}")
        unmet_block = "\n".join(unmet_lines) if unmet_lines else "  - (no per-predicate results)"
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "You called finish, but the external Definition-of-Done "
                        f"evaluator found {len(verdict.unmet)} unmet acceptance "
                        f"predicate(s) (spec fingerprint {verdict.spec_fingerprint}):\n\n"
                        f"{unmet_block}\n\n"
                        "The task is NOT complete. These predicates were captured at "
                        "task start and live outside the agent's tool surface — you "
                        "cannot edit them, you can only satisfy them. Fix what they "
                        "surface (the predicates name the gap), then finish again.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return False

    async def build_dod_evaluator(self) -> DoDEvaluator:
        """Construct the DoDEvaluator. Two paths:

          * `_dod_evaluator_factory` is set (test seam): call it, ignore args.
          * Otherwise: derive the workspace_root from the executor's sandbox
            (the in-cluster `workspace_path`); if absent, raise
            `_DoDWorkspaceUnavailable` and the gate degrades to "skip".

        The factory is the dependency-injection point — tests close over
        a tmp_path + fake command_runner / http_probe and return a fully
        configured `DoDEvaluator`. Production callers leave the factory
        None and the engine does the workspace resolution here.
        """
        if self._loop._dod_evaluator_factory is not None:
            # The factory is an async-callable in the common case (tests
            # want to close over a `tmp_path` + fakes without performing
            # any I/O at construction time), but a sync callable is also
            # accepted — production callers may want to keep the
            # construction cheap. Awaiting a non-awaitable raises
            # TypeError, which the gate's `_DoDWorkspaceUnavailable`-
            # style `try/except` doesn't catch; the explicit
            # `inspect.iscoroutine` check keeps both shapes working.
            import inspect

            result = self._loop._dod_evaluator_factory()
            if inspect.iscoroutine(result):
                result = await result
            # inspect.iscoroutine is a TypeGuard (narrows only the positive
            # branch), so the awaited-away Coroutine lingers in the static type;
            # at runtime `result` is always the resolved DoDEvaluator here.
            return cast(DoDEvaluator, result)
        sbx = getattr(self._loop.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            raise _DoDWorkspaceUnavailable(
                f"no sandbox.workspace_path on executor {type(self._loop.executor).__name__}"
            )
        from pathlib import Path

        # An `http_ok` serve-check must probe from INSIDE the sandbox: a build's dev
        # server binds the SANDBOX's localhost, not the host's. The default host-side
        # urlopen would hit the agent-server (404 on `/`), false-failing every sandboxed
        # serve and spinning the model into re-serve/re-verify loops. Route the probe
        # through `exec_shell` + curl when the backend supports it; otherwise fall back
        # to the default host probe (e.g. the process backend shares the host network).
        http_probe = None
        if sbx is not None and hasattr(sbx, "exec_shell"):
            import shlex

            async def _in_sandbox_http_probe(
                url: str, expected_status: int
            ) -> HttpProbeResult:
                cmd = (
                    "curl -s -o /dev/null -w '%{http_code}' --max-time 10 "
                    + shlex.quote(url)
                )
                try:
                    res = await sbx.exec_shell(cmd, timeout_s=15)
                except Exception as exc:  # noqa: BLE001 — a probe failure is "not probed", never a crash
                    return HttpProbeResult(
                        status_code=None,
                        error_message=f"sandbox http probe failed: {exc}",
                    )
                out = (res.stdout or "").strip()
                if not out.isdigit() or out == "000":
                    return HttpProbeResult(
                        status_code=None,
                        error_message=(
                            f"sandbox curl produced no HTTP status "
                            f"(got {out!r}, exit {res.exit_code})"
                        ),
                    )
                return HttpProbeResult(status_code=int(out))

            http_probe = _in_sandbox_http_probe

        return DoDEvaluator(Path(workspace), http_probe=http_probe)

    async def resolve_verify_command(self, args: dict) -> str:
        verify_cmd = str(args.get("verify") or "").strip()
        # E4: a `static` directive verifies a static page WITHOUT a server
        # (files present + HTML parses) — the honest check for a page build.
        if verify_cmd == _STATIC_VERIFY_PREFIX or verify_cmd.startswith(
            _STATIC_VERIFY_PREFIX + ":"
        ):
            _, _, _path = verify_cmd.partition(":")
            verify_cmd = _static_verify_command(_path)
        # app / app:<url> — verify the RUNNING deliverable actually serves
        # (HTTP 200 + non-trivial body), not just that a file exists.
        elif verify_cmd == _APP_VERIFY_PREFIX or verify_cmd.startswith(
            _APP_VERIFY_PREFIX + ":"
        ):
            _, _, _url = verify_cmd.partition(":")
            _url = _url.strip()
            if not _url:
                # Bare `verify="app"` (no explicit URL). The platform assigns the preview
                # port — there is NO fixed :8000 inside the sandbox (a curl there 404s),
                # so resolve the URL the live deliverable ACTUALLY serves on (the same
                # backend-aware detection the verify_web_app gate uses). If no live preview
                # can be located, degrade to the server-free static check rather than
                # asserting :8000 — a 404 on a guessed port is a FALSE failure that would
                # spin the model into re-serve/re-verify loops.
                _url = await self._detect_preview_url() or ""
                if not _url:
                    return _static_verify_command("index.html")
            verify_cmd = _app_verify_command(_url)
        return verify_cmd

    async def normalize_finish_step(
        self, step: AgentStep, events: list[Event]
    ) -> tuple[AgentStep, Disp]:
        # VERIFY-ON-FINISH (post-condition gate). If the agent attached
        # a `verify` check to finish, RUN it first and refuse the finish
        # if it doesn't pass — the "run the tests before you claim done"
        # forcing function. The check is visible in the trace; on
        # failure the agent sees exactly what broke and adapts, instead
        # of declaring a broken build complete.
        assert step.tool_call is not None  # caller (engine loop) enters only on the finish tool
        verify_cmd = await self.resolve_verify_command(step.tool_call.arguments)
        if verify_cmd:
            passed, malformed = await self.finish_verify_passed(verify_cmd)
            if passed:
                self._loop._finish_verify_refusals = 0  # reset streak on clean pass
            elif malformed and self._loop._finish_verify_strips < 3:
                # Broken CHECK, not a failed task → auto-strip and finish.
                self._loop._finish_verify_strips += 1
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"Your verify command `{verify_cmd}` is not runnable "
                                "(syntax error / command-not-found) — that is a broken "
                                "CHECK, not a failed task, so it is "
                                "being ignored and the "
                                "run is finishing. Next time pass a "
                                "valid shell command "
                                "if you want real verification.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                # fall through to finish
            elif self._loop._finish_verify_refusals < _FINISH_VERIFY_CAP:
                # Real failure: refuse + keep working (the forcing function).
                self._loop._finish_verify_refusals += 1
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"You called finish, but the verify "
                                f"command `{verify_cmd}` "
                                "did not pass (see the result above). The task is NOT "
                                "complete. Fix what it surfaced, then "
                                "finish again — or "
                                "finish without a verify command if "
                                "the check itself is "
                                "wrong.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                return step, Disp.CONTINUE
            else:
                # Cap reached: LOUD release — don't grind forever on a gate the
                # model can't satisfy (mirrors the browser-verify valve). The
                # failure stays visible (status detail + reminder + summary).
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        detail="finish_verify_release",
                    )
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"The verify command `{verify_cmd}` has failed "
                                f"{self._loop._finish_verify_refusals} times. "
                                "Finishing anyway "
                                "so the run does not loop forever — "
                                "but the deliverable "
                                "may be incomplete. Note this clearly "
                                "in your summary.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                # ⚠ human-facing: surfaces in the UI as a warning chip so the
                # user knows the run finished with a failing check.
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠ Finished despite the verification check failing "
                                f"{self._loop._finish_verify_refusals}× — "
                                "the deliverable may "
                                "be incomplete; review it."
                            ),
                        ),
                    )
                )
                self._loop._finish_verify_refusals = 0
                # fall through to finish
        # C1c — external DoD evaluator gate. Runs AFTER the
        # agent's own verify check (which grades the agent's
        # own command) but BEFORE the step is committed to a
        # FINISHED status. The spec is captured at task start
        # and lives outside the agent's tool surface, so the
        # predicates are NOT the agent's own — they're a
        # structural, write-once acceptance bar (see
        # `core/dod.py`). When the spec exists and the verdict
        # fails, the finish is REFUSED and the run CONTINUES —
        # the same refuse-and-continue discipline verify-on-
        # finish uses. When no spec is set for the
        # conversation, the gate is a no-op (legacy path is
        # byte-identical). See `_finish_dod_gate_passed` for
        # the full algorithm + the byte-identical-no-spec
        # proof.
        if not await self.finish_dod_gate_passed():
            return step, Disp.CONTINUE
        summary = str(step.tool_call.arguments.get("summary") or "").strip()
        step = step.model_copy(
            update={
                "finished": True,
                "tool_call": None,
                "thought": summary or step.thought,
            }
        )
        return step, Disp.FALLTHROUGH

    async def gate_execution_nudge(self, step: AgentStep, events: list[Event]) -> Disp:
        # PLAN-MODE EXECUTION GATE — a forcing function, NOT a prompt. If
        # the loop is in execution mode (planning_tools configured) and the
        # agent declares "done" without any productive action since plan
        # approval, refuse the finish: append an IMPLICIT system-reminder
        # and re-enter the loop. W5: capped at _EXECUTION_NUDGE_CAP (3) for
        # parity with the other finish-path gates — after cap the gate
        # RELEASES with a visible warning rather than running forever.
        if (
            self._loop._planning_tools  # plan-first lifecycle is configured
            and self._loop.mode != OperatingMode.PLANNING  # we're executing
            and not signals.productive_action_since_approval(events)
        ):
            # W5 cap: after _EXECUTION_NUDGE_CAP nudges without productive
            # action, TERMINALIZE the run as a FAILURE — NOT a false FINISHED.
            # Spec §11.2: after approval, execution must produce ≥1 action OR
            # a terminal explicit failure. A plan that is approved but never
            # executed is the latter, so land STUCK (bounded no-progress /
            # model-stall — same family as the other no-progress terminals;
            # ERROR is reserved for thrown exceptions). The cap is the
            # terminal exit for this path: HALT so the engine exits the loop
            # and `get_state()` surfaces STUCK/approve_plan_no_execution.
            if self._loop._execution_nudges >= _EXECUTION_NUDGE_CAP:
                _LOG.warning(
                    "execution-nudge cap (%d) reached for %s — plan approved but "
                    "never executed; terminalizing STUCK (approve_plan_no_execution).",
                    _EXECUTION_NUDGE_CAP,
                    self._loop.conversation_id,
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                "Plan approved but no execution action was taken after "
                                f"{self._loop._execution_nudges} execution reminders. "
                                "The plan was not executed.\n"
                                "</system-reminder>"
                            ),
                        ),
                    )
                )
                await self._loop._emit(
                    StatusEvent(
                        status=ConversationStatus.STUCK,
                        detail="approve_plan_no_execution",
                    )
                )
                return Disp.HALT

            self._loop._execution_nudges += 1  # telemetry + cap counter
            # Surface the model's reasoning before nudging (don't
            # discard it) — mirror the plan-nudge sibling.
            if step.thought.strip():
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(
                            role="assistant", content=step.thought
                        ),
                    )
                )
            else:
                # An empty finish-step persists nothing, so it's
                # invisible to every event-derived detector; the
                # instance counter has to carry it (the (g) no-op
                # path does the same).
                self._loop._invisible_steps += 1
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_EXECUTION_NUDGE),
                )
            )
            # The execution-nudge cap (_EXECUTION_NUDGE_CAP) is the
            # backstop for this path: after N nudges the gate emits
            # FINISHED and halts above. The old noop valve call has
            # been removed — it fired at _ACTIONLESS_BREAK_CAP (3),
            # same count as the cap, and would PAUSED the run before
            # the cap's FINISHED release could land (W5 fix).
            return Disp.CONTINUE
        # Productive action found — reset the nudge streak so a fresh
        # plan-approval cycle gets a full _EXECUTION_NUDGE_CAP budget.
        self._loop._execution_nudges = 0
        return Disp.FALLTHROUGH

    async def _gate_verify_web_app(
        self, events: list[Event], tool_name: str = "verify_web_app"
    ) -> Disp:
        """W-45 — the verdict-consuming finish gate (the loop-killer).

        Replaces `_browser_verified()` as the PRIMARY completion check for web
        builds with "the latest structured verifier verdict since the last productive
        edit". If none exists when the agent calls finish, the gate DRIVES
        the verifier itself (ONE call). pass → finish; fail → emit the verdict's
        summary + first concrete error + screenshot + next_action and CONTINUE.

        LOOP BREAKER: the verdict is cached by `_last_productive_seq` — a browser/
        navigate/verify probe is NOT a productive edit, so re-verifying without an
        edit reads the SAME cached verdict (no real re-run). When the SAME
        `failure_fingerprint` recurs without a productive edit that changes the
        served output, the gate marks the run STUCK/no_progress (W-31 detail
        naming) instead of reloading 25-40×. The 3-refusal release no longer
        silently converts repeated failed verification into 'done' — it finishes
        ONLY with an explicit blocked/incomplete summary."""
        since_seq = _last_productive_seq(events)
        # P1-1: bind the accepted verdict to the CURRENT preview target. Detect the
        # live preview (same _PREVIEW_PORTS detection the tool uses) and accept only
        # a verdict whose url matches it; a stale / foreign-port (or url-less) PASS
        # must NOT satisfy the gate — it drives a fresh verify against the real
        # preview instead. target_url=None (preview undetectable) disables binding.
        target_url = await self._detect_preview_url()
        verdict = _latest_verify_verdict(events, since_seq, target_url, tool_name)
        if verdict is None:
            # No fresh verdict bound to the current preview — the agent may have
            # overclaimed, or only a stale/foreign-url verdict exists. Drive ONE
            # against the resolved real preview (target_url is backend-aware — never
            # the agent-server's 8000 on the process backend, Bug 7).
            if await self._drive_verify_web_app(target_url, tool_name):
                events = await self._loop._events()
                # The freshly driven verify auto-detected + tested the CURRENT
                # preview, so its verdict IS bound by construction — read it
                # unconditionally (target_url=None) rather than re-binding against a
                # detection that could disagree with the tool's own auto-detect.
                verdict = _latest_verify_verdict(
                    events, since_seq, target_url=None, tool_name=tool_name
                )
        if verdict is None:
            # P1-2: the verifier could not produce a usable verdict (execution
            # error / empty / the driven verify failed). This must NOT fall through
            # to a clean finish — on the build/web surface a FINISH requires a real
            # PASS verdict (W-32: route completion THROUGH the gate). Refuse-and-
            # continue (bounded), then an EXPLICIT unverified release at the cap;
            # never a silent done, never a fall-back to the legacy browser gate.
            return await self._verifier_unavailable_disposition(tool_name)

        if verdict.get("passed"):
            self._loop._browser_verify_refusals = 0  # clean pass → reset the streak
            if _vision_mode():
                shot = str(verdict.get("screenshot_path") or "")
                if shot:
                    await self._loop._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING,
                            detail=f"vision_artifact:{shot}",
                        )
                    )
            return Disp.FALLTHROUGH

        # Bug 6 — HONEST unverifiable finish for a delivered-but-unverifiable static
        # build. If the ONLY failure is "not serving" (server unreachable; the
        # browser never even ran ⇒ no console/network errors and no blank-render
        # judgement) AND this backend cannot run a headless browser AND the static
        # deliverable file exists on disk, then the build is UNVERIFIABLE (infra),
        # not BROKEN — finish honestly with an explicit marker instead of refusing →
        # STUCK. A REAL fail (console errors, network failures, blank render, or a
        # MISSING deliverable) never reaches here, so W-45 is not weakened. This runs
        # only after `gate_execution_nudge` (so an approved-but-unexecuted plan still
        # STUCKs there) and requires index.html on disk (so a zero-action run cannot
        # finish).
        honest = await self._maybe_honest_unverifiable_static_finish(verdict)
        if honest is not None:
            return honest

        # FAIL / DEGRADED. Build the concrete next-step payload from the verdict.
        fp = str(verdict.get("failure_fingerprint") or "")
        summary = str(verdict.get("summary") or f"{tool_name} did not pass")
        next_action = str(verdict.get("next_action") or "")
        screenshot = str(verdict.get("screenshot_path") or "")
        errs = verdict.get("console_errors") or []
        nets = verdict.get("network_failures") or []
        first_error = ""
        if errs:
            e0 = errs[0]
            where = f" @ {e0.get('source')}" if e0.get("source") else ""
            first_error = f"{e0.get('text', '')}{where}"
        elif nets:
            n0 = nets[0]
            marker = n0.get("status") or n0.get("failure") or "failed"
            first_error = f"{n0.get('method', 'GET')} {n0.get('url', '')} -> {marker}"

        # CXT-7: record the failure to durable context (.disco/context/verifier_failures.json),
        # best-effort — NEVER alters the gate verdict/flow. CXT-4's assembler surfaces these
        # unresolved failures into the model's ContextPack on later turns/resume.
        await self._record_verifier_failure_to_context(
            message=(first_error or summary), rel_path=(screenshot or None)
        )

        prior_fp = _prior_verify_marker_fp(events, since_seq)
        if prior_fp is not None and prior_fp == fp:
            # LOOP BREAKER: the SAME failure verdict has recurred since the last
            # productive edit and the gate already nudged for it — no new
            # information. Halt STUCK (named) instead of re-loading forever.
            blocked = (
                f"⚠ Stopped: {tool_name} keeps returning the SAME failure with no "
                "progress since the last edit — re-loading won't help.\n"
                f"{summary}\n"
                + (f"error: {first_error}\n" if first_error else "")
                + (f"next: {next_action}" if next_action else "")
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=blocked),
                )
            )
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.STUCK,
                    detail=f"{_VERIFY_MARKER_PREFIX}{fp}",
                )
            )
            return Disp.HALT

        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            payload = (
                f"{tool_name} did not pass ({verdict.get('verdict')}). {summary}\n"
                + (f"first error: {first_error}\n" if first_error else "")
                + (f"screenshot: {screenshot}\n" if screenshot else "")
                + (f"next step: {next_action}" if next_action else "Fix the issue, then finish.")
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=payload),
                )
            )
            # Stamp the fingerprint so a repeat WITHOUT a productive edit trips the
            # loop breaker above on the next finish attempt.
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=f"{_VERIFY_MARKER_PREFIX}{fp}",
                )
            )
            return Disp.CONTINUE

        # 3-refusal release — but NOT a silent 'done' (P1-3). The release emits a
        # DISTINCT terminal signal (StatusEvent detail="unverified_release") AND a
        # visible INCOMPLETE message BEFORE finalization, so a repeatedly-failing
        # web build can never present as a clean verified FINISHED — the UI/harness
        # keys on the marker, not on the agent's pre-gate summary text. Bounded:
        # the run still releases after the cap so it cannot hang forever.
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        warn = (
            f"⚠ Finished WITHOUT a passing {tool_name} verdict (3 attempts) — the "
            f"deliverable is INCOMPLETE. {summary}"
            + (f" Outstanding: {next_action}" if next_action else "")
            + " Note this clearly in your summary."
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=warn),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    def _browser_verification_unavailable(self) -> bool:
        """True when this backend cannot run browser-based verification (the process
        backend ships no `browser` tool). Mirrors `_drive_finish_browser_probe`'s
        availability check — the precise "the render/console check cannot run here"
        signal that distinguishes an UNVERIFIABLE delivery from a BROKEN app."""
        try:
            tool_names = {
                getattr(t, "name", None) for t in self._loop.executor.available_tools()
            }
        except Exception:  # noqa: BLE001 — introspection failure → assume available (cautious)
            return False
        return "browser" not in tool_names

    async def _static_deliverable_present(self) -> bool:
        """True when the static web deliverable (index.html) exists on disk in the
        sandbox workspace — the file-truth half of the honest unverifiable finish (a
        zero-action run wrote nothing, so this is False and it cannot finish)."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None or not hasattr(sbx, "file_exists"):
            return False
        try:
            return bool(await sbx.file_exists("index.html"))
        except Exception:  # noqa: BLE001 — existence probe failure → cannot confirm
            return False

    async def _record_verifier_failure_to_context(
        self, *, message: str, rel_path: str | None
    ) -> None:
        """CXT-7 — persist a verify_web_app failure to .disco/context/verifier_failures.json
        (best-effort; never alters the gate flow). Survives truncation/resume so the
        CXT-4 assembler can surface the unresolved failure to the model later."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return
        try:
            from ..context import ArtifactMemoryStore, Severity, VerifierFailureRef

            await ArtifactMemoryStore(sbx).record_verifier_failures(
                (
                    VerifierFailureRef(
                        kind="verify_web_app",
                        message=(message or "verify_web_app did not pass")[:500],
                        rel_path=rel_path or None,
                        severity=Severity.ERROR,
                    ),
                )
            )
        except Exception:
            _LOG.warning(
                "CXT-7 verifier-failure context write failed for %s",
                self._loop.conversation_id,
                exc_info=True,
            )

    async def _maybe_honest_unverifiable_static_finish(self, verdict: dict) -> Disp | None:
        """Bug 6 — when a web build's verify FAILS ONLY because nothing is serving
        (no console/network errors, no blank-render judgement: the browser never ran)
        AND this backend cannot run a headless browser AND index.html exists on disk,
        return FALLTHROUGH with an explicit honest-unverifiable marker so a delivered
        static build FINISHES instead of pausing/STUCKing. Returns None (let the
        normal refuse/loop-break path run) for every other case — a real fail
        (console/network/blank) or a missing deliverable is NEVER converted to a
        success (W-45 preserved)."""
        http_ok = 200 <= int(verdict.get("http_status") or 0) < 400
        not_serving = (
            str(verdict.get("verdict")) == "fail"
            and not (verdict.get("console_errors") or [])
            and not (verdict.get("network_failures") or [])
            and not http_ok
        )
        if not not_serving:
            return None
        if not self._browser_verification_unavailable():
            return None
        if not await self._static_deliverable_present():
            return None
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverifiable_static_finish",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "⚠ Finished WITHOUT a live browser verification — the static "
                        "deliverable (index.html) exists but no preview server is "
                        "reachable and this backend cannot run a headless browser, so "
                        "the render could not be checked here. The files are delivered; "
                        "note clearly in your summary that the build is UNVERIFIED."
                    ),
                ),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def maybe_honest_unverifiable_static_actionless_finish(
        self, events: list[Event]
    ) -> bool:
        """Bug 6 — the ACTIONLESS-VALVE twin of `_maybe_honest_unverifiable_static_finish`.

        The finish-gate honest path only runs when the model REACHES the finish gate.
        On a browserless backend with a final browser-verify plan step the model never
        does — it churns on the unsatisfiable step and the actionless valve would PAUSE
        a substantively-complete build. This applies the SAME honest-finish concept at
        the valve: when (and ONLY when) the conservative conditions below all hold, emit
        the honest marker + a clean terminal FINISHED and return True; otherwise return
        False so the valve keeps its existing pause/stuck behavior.

        Conservative conditions (ALL required — any failure ⇒ False ⇒ no honest finish):
          1. a plan exists, is INCOMPLETE, and EVERY not-done step is verify-only;
          2. real productive work happened since approval (APPROVE_PLAN_NO_EXECUTION —
             a zero-action run can never honest-finish here);
          3. the static deliverable (index.html) exists on disk;
          4. a NON-browser validation PASSED after the last write/edit (a failed or
             absent validation blocks);
          5. browser verification is GENUINELY unavailable (no browser tool, OR a
             browser observation/error carried the unavailable signal);
          6. NO real web-failure evidence (console/network errors or a served-but-blank
             render) — W-45: a genuinely BROKEN app is never converted to a success.
        """
        if not _missing_steps_all_verify(events):
            return False
        if signals.productive_actions_since_approval(events) <= 0:
            return False
        if not await self._static_deliverable_present():
            return False
        if not _nonbrowser_static_validation_passed(events):
            return False
        if not (
            self._browser_verification_unavailable()
            or _browser_unavailable_observed(events)
        ):
            return False
        if _real_web_failure_evidence(events):
            return False
        # All guards hold — finish honestly instead of pausing actionless. Same honest
        # marker as the finish-gate path, then a clean terminal FINISHED (NOT PAUSED).
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverifiable_static_finish",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "⚠ Finished WITHOUT a live browser verification — the static "
                        "deliverable (index.html) exists and a non-browser validation "
                        "passed, but this backend cannot run a headless browser and no "
                        "preview server is reachable, so the only remaining plan step "
                        "(browser verification) could not run here. The files are "
                        "delivered; note clearly in your summary that the render is "
                        "UNVERIFIED."
                    ),
                ),
            )
        )
        await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
        self._loop._browser_verify_refusals = 0
        return True

    async def _verifier_unavailable_disposition(
        self, tool_name: str = "verify_web_app"
    ) -> Disp:
        """P1-2 — disposition when the structured verifier is advertised but produced NO
        usable verdict (verifier execution error / empty / the driven verify
        failed). On the build/web surface a clean FINISH requires a real PASS
        verdict, so this must NOT fall through to finalization (the W-32 regression
        codex found). Refuse-and-continue with a "verification could not run"
        reminder while under the cap; at the cap, release EXPLICITLY as unverified
        (distinct status marker + visible message) rather than a silent clean
        finish. Bounded by the shared `_browser_verify_refusals` cap so a verifier
        that can never run still terminates."""
        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            f"verification could not run: {tool_name} did not return a "
                            "usable verdict (the verifier failed to execute, or the preview "
                            "server is not reachable on its port). The build is NOT verified "
                            "— start/repair the dev server on the preview port, then finish "
                            "again and it will re-verify."
                        ),
                    ),
                )
            )
            return Disp.CONTINUE
        # Cap reached — bounded release, but EXPLICITLY unverified (never a clean
        # done): distinct terminal marker + visible message, mirroring the FAIL
        # release above.
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        f"⚠ Finished WITHOUT a {tool_name} verdict — the verifier could "
                        "not run after 3 attempts, so the deliverable is UNVERIFIED and may "
                        "be INCOMPLETE. Note this clearly in your summary."
                    ),
                ),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def _browser_verify_delegated_to_host(
        self, step: AgentStep, events: list[Event]
    ) -> bool:
        if not self._host_verify_authoritative():
            return False
        if getattr(self._loop, "_host_verifier", None) is None:
            return False
        deliverable = await self._host_verify_deliverable(step, events)
        if deliverable is None or deliverable.artifact_kind != "app":
            return False
        # Delegate only when the host verifier actually RAN on the current
        # output (recorded pass/fail). An ``unavailable`` verdict (verifier
        # infrastructure could not run) or no verdict at all must keep the
        # inline browser gate as enforcement — otherwise unavailable would slip
        # through BOTH gates and an app could finish with no verification.
        verdict = _latest_host_verifier_verdict(events, _last_productive_seq(events))
        return verdict in ("pass", "fail")

    async def gate_browser_verify(self, step: AgentStep, events: list[Event]) -> Disp:
        # BROWSER-VERIFY GATE — §BP-05. If web deliverable holds, refuse finish
        # until a clean browser observation (zero console errors) exists
        # since the last state-changing edit.
        verify_tool = self._active_verify_tool()
        # Strict AppKit mode is detected from the EXECUTOR (duck-typed phase
        # attribute), not only the advertised tool set: before a successful
        # app_create the phase allowlist hides verify_appkit_app, and a finish
        # in that window must still be gated (a zero-work appkit FINISH slipped
        # through here, live 2026-07-03).
        is_appkit = verify_tool == "verify_appkit_app" or (
            getattr(self._loop.executor, "appkit_phase", None) is not None
        )
        if (
            self._loop._planning_tools
            and self._loop.mode != OperatingMode.PLANNING
            and (is_appkit or _is_web_deliverable(events))
        ):
            # W-45: when the structured `verify_web_app` tool is in the execution
            # set (the build surface), consume its VERDICT as the primary check —
            # the clean actionable signal that kills the reload loop. The legacy
            # raw-observation path below stays for browserless backends / tests
            # without the tool (non-web + assist paths are untouched: this whole
            # block is gated on _is_web_deliverable / strict AppKit mode).
            if await self._browser_verify_delegated_to_host(step, events):
                return Disp.FALLTHROUGH
            if verify_tool is not None:
                return await self._gate_verify_web_app(events, verify_tool)
            since_seq = _last_productive_seq(events)
            # The preview platform assigns a RANDOM port — there is NO fixed :8000
            # inside the sandbox (a curl there 404s). Resolve the live preview the
            # SAME backend-aware way the verify_web_app gate does and bind every
            # browser-observation check + the driven probe + the nudge text to it.
            # None ⇒ undetectable (sandbox-less / legacy / isolated where :8000 IS
            # the app) ⇒ the readers fall back to the historical :8000 acceptance.
            target_url = await self._detect_preview_url()
            target_key = _preview_key(target_url) if target_url else None
            ok, _ = _browser_verified(events, since_seq, target_key)
            if not ok:
                # ACTIVE verify (verification-overclaim fix): the AGENT has NOT
                # produced a clean preview browser observation since the last edit.
                # Rather than wait/trust it to browse (it may have overclaimed and
                # never looked), DRIVE the browse ourselves (against the resolved
                # preview, not a dead :8000) and judge the probe on ground truth —
                # zero console errors AND a non-blank render (a page can serve 200
                # with a clean console yet mount nothing). Degrades to the prior
                # passive nudge/release on browserless backends (the probe is a
                # no-op there). Bounded by the existing 3-refusal cap below.
                if await self._drive_finish_browser_probe(target_url):
                    events = await self._loop._events()
                    probe = _latest_browser_structured(events, target_key)
                    if probe is not None:
                        probe_console_clean = not any(
                            c.get("level") == "error" for c in probe.get("console", [])
                        )
                        ok = probe_console_clean and _browser_content_meaningful(probe)
            # Messaging reads the FULL history: a post-browse edit
            # invalidates the verification but not what was seen.
            first_error = _latest_browser_error(events, target_key)
            if ok:
                self._loop._browser_verify_refusals = 0  # reset on clean pass
                # W6 vision artifact: when the browser observation includes a
                # screenshot, emit a StatusEvent so the UI / post-run harness can
                # find it (satisfies "finish-gate captures a screenshot" in the
                # no-test UI finish-gate). Only emitted when vision mode is active.
                if _vision_mode():
                    shot = _latest_browser_screenshot(events, target_key)
                    if shot:
                        await self._loop._emit(
                            StatusEvent(
                                status=ConversationStatus.RUNNING,
                                detail=f"vision_artifact:{shot}",
                            )
                        )
            elif self._loop._browser_verify_refusals < 3:
                self._loop._browser_verify_refusals += 1
                # Point the agent at the RESOLVED preview (random platform port), not
                # a dead :8000; fall back to :8000 only when undetectable.
                preview_url = target_url or "http://127.0.0.1:8000/"
                if first_error:
                    # Variant (2): quote the error
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        f"Use the browser tool to navigate to {preview_url}, "
                        "read the CONSOLE output, and fix any errors you see. "
                        f"The last load had errors: {first_error}"
                    )
                else:
                    # Variant (1): verbatim from order
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        f"Use the browser tool to navigate to {preview_url}, "
                        "read the CONSOLE output, and fix any errors you see. "
                        "Finish only after a clean load."
                    )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=nudge),
                    )
                )
                return Disp.CONTINUE
            else:
                # 3-refusal release valve (3): allow but warn visibly
                warn_msg = (
                    "⚠ finished WITHOUT a clean browser verification — "
                    f"last console errors: {first_error or 'none seen'}"
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=warn_msg),
                    )
                )
        return Disp.FALLTHROUGH

    async def gate_export_render(self, step: AgentStep, events: list[Event]) -> Disp:
        """[P10] Refuse FINISHED when a rendered export (deck/document) is BLANK,
        TRUNCATED, or CORRUPT — the "looks done but the file is empty" false
        completeness. The real executor for the inert ``ExportContract.validate``
        stage.

        Reads the render facts the producer stamped from the ACTUAL output bytes
        (``latest_export_render_facts``), NOT the model's declared slide_count. A
        broken export re-enters the loop with a concrete steer; a good one (or none
        produced) falls through. Bounded by ``EXPORT_GATE_MAX_REFUSALS`` so a
        genuinely-broken renderer can't trap the run — it releases with a loud
        UNVERIFIED warning, exactly like the browser-verify valve. Decision/message
        logic is pure (``contract.export_render``); this method only emits."""
        # The latest deliverable AFTER the export decides which facts govern this
        # finish (only the latest counts: a newer files handoff means the broken deck
        # is current and must be gated).
        export_idx = latest_export_render_index(events)
        latest_post_export_deliverable: DeliverableEvent | None = None
        for i in range(len(events) - 1, export_idx, -1):
            ev = events[i]
            if isinstance(ev, DeliverableEvent):
                latest_post_export_deliverable = ev
                break
        # An app deliverable emitted AFTER the broken export means the app is the
        # current handoff and the deck is superseded — fall through (the app gates
        # own that path).
        if (
            latest_post_export_deliverable is not None
            and latest_post_export_deliverable.artifact_kind == "app"
        ):
            return Disp.FALLTHROUGH

        # A FILES handoff naming a specific stamped file is gated on THAT file's facts,
        # so delivering a known-bad export can't clear on a newer sibling's good facts.
        # Otherwise the latest stamped export governs.
        facts: ExportRenderFacts | None = None
        if (
            latest_post_export_deliverable is not None
            and latest_post_export_deliverable.artifact_kind == "files"
        ):
            facts = export_render_facts_for_path(
                events, latest_post_export_deliverable.path
            )
        if facts is None:
            facts = latest_export_render_facts(events)
        if facts is None or facts.ok:
            return Disp.FALLTHROUGH  # no export stamped, or it renders fine

        if count_export_gate_refusals(events, since=export_idx) >= EXPORT_GATE_MAX_REFUSALS:
            await self._loop._emit(
                StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_export"),
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=export_gate_release_warning(facts)),
                )
            )
            return Disp.FALLTHROUGH

        await self._record_verifier_failure_to_context(message=facts.detail, rel_path=None)
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=export_gate_refusal_reminder(facts)),
            )
        )
        return Disp.CONTINUE

    async def finalize_finish(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        if await self._loop._stop_allowed(state, events):
            # Record the agent's final message (the answer) before
            # finishing — the deliverable text belongs on the log, not
            # discarded on the finish signal. (When the model just
            # answers a question, this IS the response the UI renders.)
            if step.thought.strip():
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(role="assistant", content=step.thought),
                    )
                )
            # runthru-v2 (#3): "done" is driven by the REAL finish gate —
            # `_stop_allowed` plus the DoD/verify gates already run in
            # `handle_finish_path` (gate_execution_nudge, gate_browser_verify,
            # finish_dod_gate) — NOT by plan_step bookkeeping. EVERY model tier
            # (Qwen 27B through DeepSeek/GPT/Claude) under-reports per-step
            # progress, so gating finish on "all steps marked done" bounced
            # genuinely-complete builds up to the auto-continue cap (≈3×),
            # burning rework (a chunk of the "too slow" complaint) and leaving
            # the run looking half-done. The plan is now an explanation marked
            # done when the build actually finishes; per-step progress (capable
            # models only, via the declarative update_plan_progress tool) is
            # advisory UI and never gates anything. Real incompleteness is still
            # caught by the verify gates above, not by a bookkeeping proxy.
            await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
            return Disp.HALT
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=self._loop._veto_feedback),
            )
        )
        return Disp.CONTINUE

    async def run_finish_verify_gates(self, step: AgentStep, events: list[Event]) -> Disp:
        """The render-verify gate sequence shared by EVERY finish path: host+browser
        app-verify (order depends on the authoritative flag) THEN the P10 export-render
        gate for file deliverables.

        Extracted so ``handle_finish_path`` (affirmative ``finish()``) and
        ``_completed_via_notify_finish`` (the actionless/notify valve) run the SAME
        gates — they had DRIFTED: the notify path historically skipped both
        ``gate_host_verify`` (so shadow agreement was never measurable on
        notify-completed builds, and an authoritative flip would leave a verify
        bypass) AND ``gate_export_render`` (a blank deck finishing via notify escaped
        the P10 check). Returns CONTINUE (a gate refused — caller must not finish),
        HALT (a gate landed the terminal status / loop-breaker), or FALLTHROUGH (all
        render-verify gates clear)."""
        if self._host_verify_authoritative():
            disp = await self.gate_host_verify(step, events)
            if disp is Disp.CONTINUE or disp is Disp.HALT:
                return disp
            events = await self._loop._events()
            disp = await self.gate_browser_verify(step, events)
        else:
            disp = await self.gate_browser_verify(step, events)
            events = await self._loop._events()
            await self.gate_host_verify(step, events)  # shadow: advisory, records telemetry
        if disp is Disp.CONTINUE or disp is Disp.HALT:
            return disp
        # [P10] Export render-correctness gate — for a files-deliverable (deck/
        # document) the app/browser gates above fall through, so THIS is the check
        # that a blank/truncated/corrupt export can't report FINISHED.
        events = await self._loop._events()
        return await self.gate_export_render(step, events)

    async def handle_finish_path(
        self, step: AgentStep, state: ConversationState, events: list[Event]
    ) -> Disp:
        disp = await self.gate_execution_nudge(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT

        events = await self._loop._events()
        if not await self.dictated_content_gate_passed(events):
            return Disp.CONTINUE

        events = await self._loop._events()
        disp = await self.run_finish_verify_gates(step, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            # W-45 loop breaker: a verify gate marked the run STUCK (same failure
            # fingerprint with no productive edit) and already emitted the terminal
            # status — propagate the halt so the engine exits the loop.
            return Disp.HALT
        events = await self._loop._events()

        disp = await self.finalize_finish(step, state, events)
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT
        return Disp.FALLTHROUGH

    async def synthetic_finish_after_actionless_pauses(
        self, state: ConversationState, events: list[Event]
    ) -> Disp:
        """REL-RC-P — synthesize a finish call after repeated actionless pauses.

        This emits an explicit host reminder and durable marker, then routes a
        synthetic ``finish(summary=...)`` through the same two-stage path a real
        model finish call uses: ``normalize_finish_step`` followed by
        ``handle_finish_path``. Gate refusal therefore lands the existing concrete
        reminder and returns CONTINUE; gate success lands FINISHED normally.
        """
        summary = signals.latest_agent_prose_message(events) or "work complete"
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "REL-RC-P SYNTHETIC FINISH: host is attempting completion: "
                        "work appears done and the loop has paused actionless twice. "
                        "Routing a synthetic finish(summary=...) through the normal "
                        "finish gates; any refusal below is authoritative and should "
                        "be fixed before finishing.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail=signals.SYNTHETIC_FINISH_ATTEMPT_DETAIL,
            )
        )
        step = AgentStep(
            tool_call=ToolCall(tool_name="finish", arguments={"summary": summary})
        )
        step, disp = await self.normalize_finish_step(step, await self._loop._events())
        if disp is Disp.CONTINUE:
            return Disp.CONTINUE
        if disp is Disp.HALT:
            return Disp.HALT
        return await self.handle_finish_path(
            step, state, await self._loop._events()
        )
