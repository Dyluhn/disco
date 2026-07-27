"""disco_api.py — async client that drives the LIVE Disco agent-server (PR S3).

The runner ACTS AS THE USER over the product's real surfaces (REST verbs verified in
routes/conversations.py; the plan gate + steer are WS-ONLY, verified in routes/ws.py
`_handle_frame` — there is no REST approval route):

  create_build_conversation  POST /conversations (surface="build")    conversations.py:49
                             + POST /conversations/{cid}/messages      conversations.py:149
                               (appends the user message AND kicks the loop)
  approve_plan               WS {"type":"approve_plan"}    ws.py:95 -> runtime.approve_plan
  confirm                    WS {"type":"confirm"}         ws.py:89 -> runtime.confirm (clears
                             WAITING_FOR_CONFIRMATION — the confirm analogue of approve_plan;
                             a plain message does NOT clear this gate)
  send_followup (message)    WS {"type":"send_message"}    ws.py:48 -> store.append + kick
  send_followup (steer)      WS {"type":"steer",...}       ws.py:58 -> store.append(steer)+kick
  send_followup (request_plan) WS {"type":"request_plan"}  ws.py:98 -> runtime.request_plan
  poll_until_terminal        GET /conversations/{cid}/state           conversations.py:195
  collect_events             read disco.db DIRECTLY, post-terminal (race-free; trace pattern)
  collect_state              GET /conversations/{cid}/state
  collect_workspace          read the host ProjectStore SNAPSHOT directly (authoritative;
                             Bug 9 fix — NOT the dev-server preview proxy, which 404s when
                             the served app isn't up). No generated-content fallback.
  collect_preview            GET /conversations/{cid}/preview + isolated path capability

approve_plan / request_plan are WS-ONLY (there is no REST approval route — verified
in routes/conversations.py: only create/messages/followup/events/state/kill/resume
exist; the plan gate lives in routes/ws.py `_handle_frame`). So the runner opens a
short-lived WS, sends the one control frame, and closes — the loop state machine
acts on it; truth is read back from the durable event log / state.

INFRA GATE (codex #3): `pre_create_probe` is the ONLY place INFRA_FAILURE can come
from — a RUNNER-SIDE failure BEFORE a conversation_id exists. A post-create
driver-preflight error surfaces as StatusEvent(ERROR) AFTER the user event and is a
PRODUCT outcome (classified by the oracle), never infra.

Transport is abstracted so the deterministic tests can drive the orchestration with
a FAKE transport (no live model spend); the live path uses HttpTransport (httpx +
websockets) behind the opt-in `@pytest.mark.live` marker.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shlex
import sqlite3
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx  # the adapter MAY import an http client (oracle path stays disco/http-free)
from disco.core.auth import SESSION_COOKIE, validated_canonical_preview_url
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.core.workspace_paths import strip_redundant_workspace_prefix
from disco.tools.projects.store import tree_digest as project_tree_digest
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS

from ..efficiency import LiveEfficiencyProgress, efficiency_record
from ..events import (
    KIND_ACTION,
    KIND_AGENT_ERROR,
    KIND_OBSERVATION,
    NormalizationError,
    action_id_of,
    kind_of,
    normalize_events,
    seq_of,
    tool_name_of,
)
from ..oracles.thrash import ThrashOracle, progress_epoch_boundary

# Pre-create infra error hierarchy (codex P1#2): the runner-side health probe fails
# with built-in OSError/ConnectionError/TimeoutError (fake transport, raw sockets) OR,
# via HttpTransport, with the httpx hierarchy — httpx.ConnectError / ConnectTimeout /
# TimeoutException / TransportError all subclass httpx.HTTPError, which does NOT
# subclass OSError. Catch BOTH so a DEAD server pre-create yields INFRA_FAILURE (§9),
# never a raw crash.
_PRECREATE_INFRA_ERRORS: tuple[type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
    httpx.HTTPError,
)

_LOG = logging.getLogger("build_soak.disco_api")

# ---- status vocabulary (mirrors disco.core.ConversationStatus as plain strings) --

TERMINAL_STATES = frozenset({"FINISHED", "VERIFIED", "ERROR", "STUCK", "IDLE"})
LIVE_THRASH_STOP = "LIVE_THRASH_STOP"
# A cooperative / no-progress PAUSE (the actionless valve). NOT terminal — it is
# RESUMABLE, so the runner (acting as the user) resumes it a bounded number of times.
PAUSED_STATE = "PAUSED"
# The terminals that mean WORK ENDED (vs IDLE, which is ALSO the pre-kick resting
# state). The drive stops on these unconditionally; IDLE only after the run started.
_WORK_TERMINALS = frozenset({"FINISHED", "VERIFIED", "ERROR", "STUCK"})
# Gates the runner must ACT on (approve / answer / confirm) rather than wait through.
GATE_STATES = frozenset(
    {
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_USER_DECISION",
        "AWAITING_USER_QUESTION",
        "WAITING_FOR_CONFIRMATION",
    }
)
AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
# The mid-build CLARIFY / CONFIRM gates the runner answers as the user (Bug 17): a
# capable model may ask a clarifying question (AWAITING_USER_QUESTION) or pause for a
# go-ahead (WAITING_FOR_CONFIRMATION) BEFORE/while building. The runner answers them so
# an interactive build proceeds instead of false-stalling into NO_PLAN.
AWAITING_USER_QUESTION = "AWAITING_USER_QUESTION"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
# The structured user-choice gate: after repeated tool failure the model proposes 2–3
# concrete next-step alternatives (an AlternativesEvent) and the loop parks at
# AWAITING_USER_DECISION until the user picks one (the WS `pick_alternative` frame →
# runtime.pick_alternative). A non-interactive soak auto-resolves it (resolve_decision).
AWAITING_USER_DECISION = "AWAITING_USER_DECISION"

# H347: the harness may not call a durable action inactive before the product's
# executor can legally finish it and persist its typed observation/error. These
# values are pinned to the product definitions by cross-module regression tests.
_DEFAULT_TOOL_TIMEOUT_S = 300.0
_SLIDES_GENERATE_TIMEOUT_S = 900.0
_TOOL_TIMEOUT_OVERRIDES_S = {"slides_generate": _SLIDES_GENERATE_TIMEOUT_S}
_ACTION_RESULT_PERSISTENCE_GRACE_S = 10.0

# ---- progress-aware terminal-wait sentinels (Bug 15) ------------------------
# The terminal wait is PROGRESS-AWARE, not a blind wall-clock: while the conversation
# is still emitting NEW events (max seq / event count advancing) or its status is
# actively transitioning, we KEEP WAITING. A wall-clock cutoff only ends the wait when
# the run is GENUINELY INACTIVE (no progress for the inactivity window) or a generous
# HARD CAP bounds a truly-hung run. The two timeout outcomes are NOT the same finding:
#   * PROGRESSING_TIMEOUT — the hard cap was hit while the build was STILL actively
#     progressing (events advanced within the inactivity window). The runner could NOT
#     obtain a terminal verdict; this is INCONCLUSIVE (model-speed / harness limitation),
#     NOT a product BUILD_DID_NOT_FINISH → the caller records INVALID_RUN so §17 re-runs it.
#   * INACTIVE_TIMEOUT — the build went genuinely SILENT (no new events) for the inactivity
#     window: a real wedge. The caller falls through to normal classification (the
#     collected non-terminal run is a genuine finding: BUILD_DID_NOT_FINISH / STUCK).
PROGRESSING_TIMEOUT = "PROGRESSING_TIMEOUT"
INACTIVE_TIMEOUT = "INACTIVE_TIMEOUT"

# ---- after-terminal follow-up SERIALIZATION sentinels (H1) -------------------
# The runner sends each `after_terminal` follow-up as its own re-plan→approve→terminal
# cycle. The bug: the post-terminal poll returned IMMEDIATELY on the STALE prior FINISHED
# status, so follow-up 2 was sent before the engine even started processing follow-up 1;
# the engine then only processes the LATEST unprocessed user turn, COLLAPSING the piled-up
# follow-ups into ONE plan revision (rev 2 vs an expected 3) → false PLAN_REVISION_NOT_INCREMENTED.
# `wait_for_followup_pickup` blocks AFTER each send until the engine actually PICKS UP the
# follow-up (relative to a baseline captured BEFORE the send) so the follow-ups are serialized.
FOLLOWUP_PICKED_UP = "FOLLOWUP_PICKED_UP"  # the build left its prior terminal status (processing)
FOLLOWUP_REPLANNED = "FOLLOWUP_REPLANNED"  # a re-plan signal (planning re-entry / revision bump)
FOLLOWUP_PICKUP_TIMEOUT = "FOLLOWUP_PICKUP_TIMEOUT"  # no pickup within the bound → caller proceeds
# enter_planning stamps this status DETAIL on a re-plan re-entry (RUNNING + detail="planning");
# the very first build turn is a plain RUNNING with no detail — so this detail is a genuine
# RE-PLAN signal, never the initial plan. Mirrors events.PLANNING_DETAIL / the product's loop.
_PLANNING_DETAIL = "planning"
# Bound for the after-terminal pickup wait — POLICY-DRIVEN (env-overridable), NOT a brittle
# literal. The pickup signal (plan-rev bump / planning re-entry / first non-user progress past
# the baseline seq) is detected correctly but arrives LATE on a slow driver: measured ~37s on
# MiniMax-M3 (finalize + replan latency). The old 25s literal therefore ALWAYS timed out, which
# (under the now-removed re-send branch) duplicated the user turn and could stall the next
# follow-up. Default 75s covers the measured ~37s with margin; override via the env var with a
# safe float parse. It MUST stay BELOW the build `--timeout` (the per-drive inactivity window)
# so a genuinely stuck follow-up HARD-FAILS as a sequencing failure (INVALID_RUN) before the
# drive's own wait would mask it. Resolved per-call so an override is honored live (testable).
_FOLLOWUP_PICKUP_TIMEOUT_ENV = "DISCO_SOAK_PICKUP_TIMEOUT_S"
_FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S = 75.0


def _followup_pickup_timeout_s() -> float:
    """The pickup-wait bound: ``$DISCO_SOAK_PICKUP_TIMEOUT_S`` (positive float seconds) else the
    documented default. A missing/blank/non-numeric/non-positive value falls back safely so a
    fat-fingered override never silently disables the bound."""
    raw = os.environ.get(_FOLLOWUP_PICKUP_TIMEOUT_ENV)
    if raw is None:
        return _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S
    return val if val > 0 else _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S


# Workspace-snapshot manifest bounds (Bug 9 fix): cap per-file captured content and the
# number of files walked so a pathological workspace can't blow up the dossier.
_WS_MANIFEST_MAX_BYTES = 5 * 1024 * 1024  # capture content for files up to 5 MiB
_WS_MANIFEST_MAX_FILES = 2000
# H190: screenshot bytes referenced by successful browser/verifier observations are
# durable campaign evidence, not ordinary workspace text.  Keep the capture bounded,
# but never truncate a PNG: an over-limit file/run is unadjudicable and therefore
# INVALID_RUN rather than a misleading partial visual proof.
_BROWSER_EVIDENCE_MAX_FILES = 64
_BROWSER_EVIDENCE_MAX_FILE_BYTES = 16 * 1024 * 1024
_BROWSER_EVIDENCE_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_BROWSER_EVIDENCE_TOOLS = frozenset({"browser", "verify_web_app", "verify_appkit_app"})
_PREVIEW_RESET_BODY_RE = re.compile(r'\bbody:("(?:\\.|[^"\\])*")')
_MAX_PREVIEW_HANDOFF_CHARS = 16 * 1024
_SNAPSHOT_POLL_S = 0.5  # re-read cadence while a just-finished build's snapshot flushes
# Consecutive identical reads required before a declared file with NO content-precise signal
# (a partial mutator with no provable post-edit content — `present_unproven` — or no event
# signal at all — `unknown`) is treated as SETTLED. These can't be content-verified, so we
# demand SEVERAL consecutive-stable reads (the bytes stopped changing across multiple poll
# intervals) before accepting + log a warning, rather than fail-fast — a legitimate
# edit-without-readback build must NOT become INVALID_RUN.
_SNAPSHOT_UNPROVEN_STABLE_POLLS = 4
# The agent's FINAL on-disk state per declared file is reconstructed from the durable event
# log. file_write persists its FULL content in the action payload (`_snip_args` elides only
# at LLM-render time — verified in core.events: "The full content stays in the event
# payload"), so a successful file_write gives a sha-PRECISE expected identity. The partial
# mutators rewrite only part of the file, so their post-state can't be reconstructed from a
# single action → they fall back to content-stability.
# safe_write_file writes its FULL `content` arg verbatim (via _atomic_write tmp+rename), exactly
# like file_write → sha-precise expected identity. Without it, a revision whose FINAL touch is a
# safe_write_file was invisible and `expected` pinned to an EARLIER file_write's sha → false
# WORKSPACE_SNAPSHOT_NOT_READY on a snapshot that actually holds the correct latest bytes.
_FILE_WRITE_FULL_TOOLS = frozenset({"file_write", "safe_write_file"})
# exact_replace is a TARGETED partial mutator (the promoted replacement for file_edit on revise
# follow-ups) — its post-state can't be reconstructed from one action, so it degrades to
# content-stability (present_unproven, accepted best-effort) instead of resurrecting a stale sha.
_FILE_WRITE_PARTIAL_TOOLS = frozenset(
    {
        "file_append",
        "file_edit",
        "file_replace_lines",
        "file_insert_lines",
        "file_str_replace",
        "exact_replace",
    }
)
_FILE_MUTATION_TOOLS = frozenset((*_FILE_WRITE_FULL_TOOLS, *_FILE_WRITE_PARTIAL_TOOLS))
# A partial mutator can't be reconstructed from its action alone, but a later FULL
# ``file_read`` readback carries the agent's TRUE post-edit bytes — in the file_read RENDERED
# view (``<line-no>\t<line>``), NOT raw bytes (FileReadTool numbers every line). So a
# readback-derived expected is compared in RENDERED space (see `_render_numbered`), not by
# raw sha256. We only promote a read that is FULL and GENUINELY RENDERED:
#   * FULL — the read header is ``[lines 1-N of N]`` with the two counts EQUAL (started at
#     line 1, showed every line); a budget-truncated read appends ``; read more with
#     offset=…`` and a pressure read appends ``— HEAD-ONLY under context pressure`` (counts
#     unequal / extra text), so both fail this exact match. The read's args must also carry
#     no offset/limit.
#   * GENUINELY RENDERED — the content is the real file_read body, NOT a synthetic template.
#     The F9 read-dedup pointer (``[F9 dedup: …]``, emitted for duplicate reads on assist
#     runs) carries NO bytes and must NEVER be promoted; it is rejected by the header match.
# A qualifying readback is used ONLY when its seq is AFTER the path's last mutation (a
# readback before a later edit is stale → ignored).
_FILE_READ_TOOLS = frozenset({"file_read"})
# Matches ONLY a full read header: ``[lines 1-N of N]`` with N == N. The first capture must
# equal the second (start==1 already pinned by the literal ``1-``).
_FILE_READ_FULL_HEADER_RE = re.compile(r"^\[lines 1-(\d+) of (\d+)\]$")
# An elision placeholder (e.g. "<1,234 chars elided …>") copied into a file_write content arg
# is NOT real bytes — mirrors core.events._ELISION_MARKER_RE structurally. When matched we
# decline the sha gate for that file and fall back to stability (defensive: a SUCCESSFUL
# file_write already carries full bytes, so this is belt-and-suspenders).
_ELISION_MARKER_RE = re.compile(r"<\s*\d[\d,]*\s*chars\b[^>]*?\b(?:elided|full content)\b[^>]*>")
_RAW_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_STRICT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINAL_SEAL_EFFECT_KINDS = frozenset({"action", "observation", "agent_error", "workspace_mutation"})
_FINAL_SEAL_KEYS = frozenset(
    {
        "schema_version",
        "scope",
        "terminal_seq",
        "latest_effect_seq",
        "version_seq",
        "tree_digest",
        "file_count",
        "total_bytes",
    }
)
_FINAL_SEAL_SCOPE_KEYS = frozenset({"namespace", "identifier"})
# Tools that DON'T count as a "file write" for the mid-run steer trigger (the
# planning-safe read/ask set; mirrors ToolScopeOracle.PLANNING_SAFE_TOOLS).
_NON_MUTATING_TOOLS = frozenset(
    {"submit_plan", "file_read", "file_list", "search", "extract", "ask_user", "clarify", "think"}
)


class InconclusiveRunError(Exception):
    """A POST-create terminal-wait that could NOT obtain a verdict because the build was
    cut off by the HARD CAP while it was STILL ACTIVELY PROGRESSING (Bug 15). This is NOT
    a product failure (the loop never failed to finish — it simply hadn't finished by the
    safety ceiling) and NOT pre-create INFRA_FAILURE; the runner degrades it to INVALID_RUN
    so the §17 no-fluke policy re-runs it instead of recording a false BUILD_DID_NOT_FINISH.
    `facts` carries the progress evidence (last seq seen, elapsed) for the dossier."""

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}
        # Populated by drive_scenario after it establishes the explicit diagnostic
        # stop boundary and freezes every safely collectable evidence slice.
        self.collected_run: CollectedRun | None = None


class FollowupPickupError(Exception):
    """An after-terminal follow-up was SENT (exactly ONCE) but the engine never PICKED IT UP
    within the policy-driven bound — no progress event past the baseline seq, status stuck at the
    prior terminal. This is a SEQUENCING failure, NOT a product verdict. The runner does NOT
    re-send (a second send_followup would DUPLICATE the user turn — there is no legal
    kick-without-append on a FINISHED run) and does NOT drive on the STALE terminal (which is
    exactly what lets the NEXT follow-up collapse into this unprocessed one → V1's false single
    plan revision); it records INVALID_RUN instead. `facts` carries the baseline evidence
    (status, seq, revision) for the dossier."""

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}


# The one seal-evidence reason meaning "this run has no agent-final state at all".
# Named so the producer (the seal check) and the consumer (the snapshot wait, which
# must not demand convergence on a state that cannot exist) cannot drift apart.
_NO_SYSTEM_FINISHED_TERMINAL = "latest status is not exact SYSTEM FINISHED"


class SnapshotNotReadyError(Exception):
    """The host ProjectStore workspace SNAPSHOT never reached the build's AGENT-FINAL state
    within `snapshot_wait_s`: for at least one declared file the snapshot's on-disk bytes
    never matched what the agent LAST wrote to it (a slow/failed flush, or a multi-revision
    build whose later-revision bytes hadn't flushed when the collect deadline hit). This is
    a deterministic readiness FAILURE, NOT a product verdict — accepting the stale capture
    would launder it into a false ARTIFACT_TRUTH_MISMATCH or a silent stale PASS. The runner
    records INVALID_RUN (`WORKSPACE_SNAPSHOT_NOT_READY`) so §17 re-runs it. `facts` carries
    the per-file expected/observed evidence for the dossier. Bounded — never hangs."""

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}


@dataclass(frozen=True)
class _FinalWorkspaceSealEvidence:
    """Strictly validated event-log half of one final workspace seal.

    Filesystem facts are deliberately checked separately, after this structure has
    selected the exact immutable version.  Keeping the two halves independent
    prevents a valid tree from laundering a stale/legacy marker, or a valid marker
    from blessing mutable live-workspace bytes.
    """

    terminal_seq: int | None = None
    latest_effect_seq: int | None = None
    event_seq: int | None = None
    version_seq: int | None = None
    tree_digest: str | None = None
    file_count: int | None = None
    total_bytes: int | None = None
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None and self.event_seq is not None


def _event_status_value(event: dict[str, Any]) -> str:
    """The status string of a normalized status event (dict or scalar shape)."""

    value = event.get("status")
    if isinstance(value, dict):
        value = value.get("value")
    return str(value or "")


def _latest_run_intent_id(events: list[dict[str, Any]]) -> str | None:
    """The most recent run-intent id carried by any event, or None.

    Run authority changes when a new intent is claimed; the freeze horizon is only
    honest if authority did not move underneath it.
    """

    latest: str | None = None
    for event in events:
        raw = event.get("run_intent_id")
        if isinstance(raw, str) and raw:
            latest = raw
    return latest


def _latest_agent_view_id(events: list[dict[str, Any]]) -> str | None:
    """The most recent model-view generation carried by any event, or None.

    The product treats the agent view as a separate authority axis from the run
    intent (``current_workspace_agent_view_id`` in ``disco.core.events``): a view
    can be superseded while the intent id is unchanged, and a control targeting a
    superseded view is not current.  A freeze that ignored this would bind evidence
    to a horizon whose model-view authority had already moved.
    """

    latest: str | None = None
    for event in events:
        raw = event.get("agent_view_id")
        if isinstance(raw, str) and raw:
            latest = raw
    return latest


def _freeze_horizon_violation(
    events: list[dict[str, Any]],
    *,
    pre_seq: int,
    paused_seq: int,
    horizon_seq: int,
    pre_intent: str | None,
    pre_view: str | None,
) -> str | None:
    """Why this freeze horizon is not certifiable, or None when it holds.

    One choke point for every authority race, so a new race is fenced by adding a
    case here rather than by scattering checks through the freeze sequence.  All
    four are supersession of the same kind: something took authority over the run
    between the pre-pause watermark and the accepted WorkspaceVersionEvent, which
    means the frozen bytes are not a truthful snapshot of *this* run's work.
    """

    for event in events:
        try:
            seq = int(event.get("seq", -1))
        except (TypeError, ValueError):
            continue
        if not (pre_seq < seq <= horizon_seq):
            continue

        # (a) a new user turn: the user changed the request mid-freeze.
        if (
            event.get("kind") == "message"
            and str((event.get("message") or {}).get("role") or "") == "user"
            and str(event.get("source") or "") == "user"
        ):
            return f"user turn at seq {seq} crossed the freeze horizon"

        # (b) a resume: the run left PAUSED before the snapshot event landed, so
        # the version does not describe a quiesced workspace.
        if seq > paused_seq:
            status = _event_status_value(event)
            if status and status != PAUSED_STATE:
                return (
                    f"run left {PAUSED_STATE} (status {status} at seq {seq}) "
                    "before the freeze horizon"
                )

    # (c) run authority moved to a newer intent.
    if _latest_run_intent_id(events) != pre_intent:
        return "run intent changed across the freeze horizon"

    # (d) the model-view generation was superseded.
    if _latest_agent_view_id(events) != pre_view:
        return "agent view generation changed across the freeze horizon"

    return None


@dataclass(frozen=True)
class _VerifiedWorkspaceVersion:
    workspace: Path
    file_count: int
    total_bytes: int
    tree_digest: str


def _strict_final_workspace_seal(
    events: list[dict[str, Any]], conversation_id: str
) -> _FinalWorkspaceSealEvidence:
    """Validate the frozen live lane's non-bypass final-seal contract.

    Historical unsealed workspace-version events may remain in the log for schema
    compatibility, but the *latest* marker must carry a complete schema-v1 seal
    bound to the latest exact SYSTEM/FINISHED status and every effect fence.
    """

    def exact_seq(event: dict[str, Any]) -> int | None:
        raw = event.get("seq")
        return raw if type(raw) is int and raw > 0 else None

    semantic = [
        event
        for event in events
        if event.get("kind") in {"status", "workspace_version", *_FINAL_SEAL_EFFECT_KINDS}
    ]
    if any(exact_seq(event) is None for event in semantic):
        return _FinalWorkspaceSealEvidence(error="semantic event has no canonical sequence")

    statuses = [event for event in events if event.get("kind") == "status"]
    if not statuses:
        return _FinalWorkspaceSealEvidence(error="event log has no status event")
    latest_status = max(statuses, key=lambda event: exact_seq(event) or -1)
    terminal_seq = exact_seq(latest_status)
    if (
        terminal_seq is None
        or latest_status.get("source") != "system"
        or latest_status.get("status") != "FINISHED"
    ):
        return _FinalWorkspaceSealEvidence(error=_NO_SYSTEM_FINISHED_TERMINAL)

    effects = [event for event in events if event.get("kind") in _FINAL_SEAL_EFFECT_KINDS]
    latest_effect_seq = max((exact_seq(event) or -1 for event in effects), default=-1)
    exact_latest_effect_seq = latest_effect_seq if latest_effect_seq > 0 else None
    if exact_latest_effect_seq is not None and exact_latest_effect_seq >= terminal_seq:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            error="effect event exists at or after terminal FINISHED",
        )

    versions = [event for event in events if event.get("kind") == "workspace_version"]
    if not versions:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            error="event log has no workspace version marker",
        )
    marker = max(versions, key=lambda event: exact_seq(event) or -1)
    event_seq = exact_seq(marker)
    if event_seq is None or marker.get("source") != "system":
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            error="latest workspace version is not a canonical system event",
        )
    if event_seq <= terminal_seq:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            error="workspace version does not follow terminal FINISHED",
        )
    if marker.get("trigger") != "finish":
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            error="workspace version is not finish-triggered",
        )

    version_seq = marker.get("version_seq")
    tree_digest = marker.get("tree_digest")
    if type(version_seq) is not int or version_seq < 1:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            error="workspace version has an invalid version sequence",
        )
    if not isinstance(tree_digest, str) or _STRICT_SHA256_RE.fullmatch(tree_digest) is None:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            version_seq=version_seq,
            error="workspace version has an invalid tree digest",
        )

    seal = marker.get("final_seal")
    if not isinstance(seal, dict):
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            version_seq=version_seq,
            tree_digest=tree_digest,
            error="latest workspace version has no final seal",
        )
    if set(seal) != _FINAL_SEAL_KEYS:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            version_seq=version_seq,
            tree_digest=tree_digest,
            error="final seal fields do not match schema v1",
        )
    scope = seal.get("scope")
    if not isinstance(scope, dict) or set(scope) != _FINAL_SEAL_SCOPE_KEYS:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            version_seq=version_seq,
            tree_digest=tree_digest,
            error="final seal scope is malformed",
        )
    if scope.get("namespace") != "workspace.tree" or scope.get("identifier") != conversation_id:
        return _FinalWorkspaceSealEvidence(
            terminal_seq=terminal_seq,
            latest_effect_seq=exact_latest_effect_seq,
            event_seq=event_seq,
            version_seq=version_seq,
            tree_digest=tree_digest,
            error="final seal scope does not match the conversation workspace",
        )

    seal_version_seq = seal.get("version_seq")
    seal_digest = seal.get("tree_digest")
    file_count = seal.get("file_count")
    total_bytes = seal.get("total_bytes")
    if type(seal.get("schema_version")) is not int or seal.get("schema_version") != 1:
        error = "final seal schema version is not 1"
    elif type(seal.get("terminal_seq")) is not int or seal.get("terminal_seq") != terminal_seq:
        error = "final seal terminal sequence does not match the event log"
    elif (
        "latest_effect_seq" not in seal
        or (exact_latest_effect_seq is None and seal.get("latest_effect_seq") is not None)
        or (
            exact_latest_effect_seq is not None
            and (
                type(seal.get("latest_effect_seq")) is not int
                or seal.get("latest_effect_seq") != exact_latest_effect_seq
            )
        )
    ):
        error = "final seal latest effect sequence does not match the event log"
    elif type(seal_version_seq) is not int or seal_version_seq != version_seq:
        error = "final seal version sequence does not match its event"
    elif not isinstance(seal_digest, str) or seal_digest != tree_digest:
        error = "final seal tree digest does not match its event"
    elif type(file_count) is not int or file_count < 0:
        error = "final seal file count is invalid"
    elif type(total_bytes) is not int or total_bytes < 0:
        error = "final seal total bytes is invalid"
    else:
        error = None

    return _FinalWorkspaceSealEvidence(
        terminal_seq=terminal_seq,
        latest_effect_seq=exact_latest_effect_seq,
        event_seq=event_seq,
        version_seq=version_seq,
        tree_digest=tree_digest,
        file_count=file_count if type(file_count) is int else None,
        total_bytes=total_bytes if type(total_bytes) is int else None,
        error=error,
    )


class BrowserEvidenceCollectionError(Exception):
    """A referenced browser screenshot could not be captured byte-for-byte.

    Screenshot references are claims about visual evidence.  If their source path
    escapes the authoritative workspace snapshot, is absent, exceeds a capture bound,
    or no longer matches the workspace manifest identity, the run is not reproducible.
    Callers turn this into INVALID_RUN; they must never silently omit the screenshot.
    """

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}


class InfraProbeError(Exception):
    """A runner-side, pre-create infrastructure failure that matched an infra
    signature. Carries the matched signature id + a machine-readable detail so the
    runner can emit a §9-compliant INFRA_FAILURE record (never a product failure)."""

    def __init__(self, signature_id: str, detail: dict[str, Any]) -> None:
        super().__init__(f"infra signature matched: {signature_id}")
        self.signature_id = signature_id
        self.detail = detail


# ---- transport abstraction --------------------------------------------------


class Transport(Protocol):
    """The minimal surface the client needs. Real impl = HttpTransport; tests
    supply a fake so orchestration runs with no live model spend."""

    async def post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...

    async def get_json(self, path: str) -> tuple[int, dict[str, Any]]: ...

    async def get_text(self, path: str) -> tuple[int, str, dict[str, str]]: ...

    async def post_file(
        self,
        path: str,
        *,
        field: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> tuple[int, dict[str, Any]]: ...

    async def get_bytes(self, path: str) -> tuple[int, bytes, dict[str, str]]: ...

    async def fetch_isolated_preview(
        self, conversation_id: str
    ) -> tuple[int, str, dict[str, str]]: ...

    async def ws_control(self, conversation_id: str, frame: dict[str, Any]) -> None: ...

    async def health(self) -> tuple[int, dict[str, Any]]:
        """Liveness of the agent-server itself (pre-create)."""
        ...


@dataclass
class CollectedRun:
    """Everything the dossier-assembler + classifier need from one live run."""

    conversation_id: str
    events: list[dict[str, Any]]
    state_initial: dict[str, Any]
    state_final: dict[str, Any]
    workspace_manifest: dict[str, Any]
    preview: dict[str, Any] | None
    inspect_trace: dict[str, Any] | None = None
    thrash_monitor: dict[str, Any] = field(default_factory=dict)
    timeline: list[str] = field(default_factory=list)
    # Each AWAITING_USER_DECISION gate the runner AUTO-RESOLVED (Part B): one dict per
    # resolution {alternatives_id, option_id, attempt}. A PASS that REQUIRED auto-resolution
    # is distinguishable from a clean PASS by a non-empty list (surfaced as
    # `auto_resolved_decisions` in the classification).
    decision_resolutions: list[dict[str, Any]] = field(default_factory=list)
    # Revision-anchor metadata (harness-only): the user-message seqs of the DECLARED scenario
    # follow-ups the runner sent (with each one's `requires_plan_revision` flag, parallel), and
    # the seqs of HARNESS-INJECTED auto-answers (clarification answers / decision picks). The
    # RevisionOracle anchors revision checks on the DECLARED follow-ups ONLY and EXCLUDES the
    # injected turns, so an auto-answer is never mis-anchored as a revision follow-up.
    declared_followup_seqs: list[int] = field(default_factory=list)
    declared_followup_requires_revision: list[bool] = field(default_factory=list)
    harness_injected_user_seqs: list[int] = field(default_factory=list)
    # Byte-identical browser screenshots explicitly referenced by successful
    # browser/verifier observations. Empty by default and deliberately appended last
    # so legacy positional CollectedRun construction retains its old field ordering.
    browser_evidence: dict[str, bytes] = field(default_factory=dict)
    # Terminal lifecycle/sidecar/cleanup measurements populated after the main
    # run is frozen but before dossier assembly.  Explicit (rather than a dynamic
    # attribute) so INVALID retention and replay share one typed evidence contract.
    # Appended for positional compatibility with every pre-existing fixture.
    product_evidence: dict[str, Any] = field(default_factory=dict)
    # A harness-owned, non-product stop boundary used only for INVALID diagnostic
    # retention.  It never makes a run promotion-eligible and is deliberately
    # distinct from a real Build terminal or a confirmed live-thrash stop.
    diagnostic_stop: str | None = None
    # Explicit wall-clock boundary returned by the hard-cap stop.  Follow-up runs
    # may retain an older FINISHED event, so provider accounting must never infer
    # this boundary from the generic terminal-event scan.
    diagnostic_stop_epoch: float | None = None
    diagnostic_stop_seq: int | None = None
    # True only when the kill endpoint acknowledged a 2xx killed+IDLE response.
    # Cleanup uses this to avoid redundant teardown while leaving the finally
    # fallback armed when the stop request was rejected or ambiguous.
    diagnostic_release_confirmed: bool = False
    # A browser screenshot capture failure is normally verdict-invalidating.  The
    # sole exception is a run whose strict live-thrash monitor already established
    # and durably stopped a product failure.  In that case the runner retains the
    # collection error as a distinct hash-locked diagnostic artifact; it is never
    # admitted as browser proof and therefore cannot satisfy a screenshot contract.
    # Appended last so every historical positional fixture retains its field mapping.
    browser_evidence_collection_error: dict[str, Any] | None = None


_INSPECT_PROJECTIONS = {
    "routing": "routing_decisions",
    "span": "spans",
    "tool_scope": "tool_scopes",
    "progress_shadow": "progress_shadows",
}
_INSPECT_AGGREGATION_REASON_ORDER = (
    "first_snapshot_already_dropped",
    "event_content_conflict",
    "eviction_gap_no_overlap",
    "source_reset",
    "dropped_count_regression",
    "impossible_dropped_transition",
    "snapshot_sequence_regression",
    "conversation_mismatch",
    "ambiguous_event_discriminator",
    "malformed_snapshot",
    "inspect_unavailable",
    "active_stop_unconfirmed",
    "poller_cancelled",
    "no_valid_snapshot",
)


@dataclass
class _InspectTraceAggregation:
    """Canonical, overlap-proven aggregate of bounded inspect snapshots."""

    conversation_id: str
    canonical_events: dict[int, bytes] = field(default_factory=dict)
    sample_count: int = 0
    accepted_sample_count: int = 0
    event_bearing_sample_count: int = 0
    pretrace_unavailable_count: int = 0
    unavailable_sample_count: int = 0
    malformed_sample_count: int = 0
    overlap_sample_count: int = 0
    conflict_count: int = 0
    source_max_dropped_count: int = 0
    last_source_dropped_count: int | None = None
    last_snapshot_seqs: tuple[int, ...] = ()
    reasons: set[str] = field(default_factory=set)
    finalized: bool = False
    # Single-use license for a harness-declared stack restart; see
    # note_expected_stack_restart. Counted for auditability, not rendered.
    restart_pending: bool = False
    restart_outage_seen: bool = False
    declared_restart_count: int = 0

    @staticmethod
    def _canonical_event(event: dict[str, Any]) -> bytes:
        return json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _projection_for_event(event: dict[str, Any]) -> tuple[str, bool]:
        """Return (projection field, kind_is_outer_discriminator).

        Inspect currently flattens ``{seq, kind=outer, **span_fields}``. A
        request-budget span legitimately has a payload field named ``kind``
        (for example ``soft``), which overwrites the outer ``span`` label. The
        exact span signature remains closed: non-empty ``span`` plus one of the
        three observability event phases. Any other discriminator collision is
        ambiguous and therefore rejected rather than silently losing a
        projection.
        """

        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("inspect event discriminator is not a non-empty string")
        span_signature = (
            isinstance(event.get("span"), str)
            and bool(event["span"])
            and event.get("event") in {"start", "end", "point"}
        )
        if kind == "span":
            if not span_signature:
                raise ValueError("inspect span event has no exact span signature")
            return "spans", True
        projection = _INSPECT_PROJECTIONS.get(kind)
        if projection is not None:
            if span_signature:
                raise ValueError("inspect event discriminator collides with a span")
            return projection, True
        if span_signature:
            return "spans", False
        raise ValueError("inspect event discriminator is ambiguous")

    def note_expected_pretrace_absence(self) -> None:
        self.sample_count += 1
        self.pretrace_unavailable_count += 1

    def note_expected_stack_restart(self) -> None:
        """Arm a single-use license for the outage THIS harness is about to cause.

        A `restart_after_terminal` scenario kills the agent-server on purpose, so
        the poller's next samples cannot reach it. That unreachability is not
        evidence loss and must not be scored as such — but the exemption has to be
        exactly as narrow as the declared act, or it becomes a blanket excuse.

        So: armed only by the drive, immediately before it restarts the stack;
        covers only unreachability, never a content conflict or a sequence
        regression; and consumed by the first accepted post-restart sample, after
        which unavailability is a real reason again. The samples still increment
        `unavailable_sample_count`, so the window stays auditable in the rendered
        aggregate without any change to its shape.

        This is only sound because the trace itself now survives the restart (the
        inspect journal): the post-restart stream continues the same monotonic
        sequence rather than renumbering from 1. If that durability regresses, the
        conflict and regression reasons still fire and still fail the run.
        """
        self.declared_restart_count += 1
        self.restart_pending = True

    def note_unavailable(self) -> None:
        self.sample_count += 1
        self.unavailable_sample_count += 1
        if not self.restart_pending:
            self.reasons.add("inspect_unavailable")
            return
        # The declared outage has now actually been observed. Until this point
        # the license must NOT be consumable: the drive arms it before issuing
        # the restart, and the poller keeps taking accepted samples for the
        # seconds the old server takes to die.
        self.restart_outage_seen = True

    def note_trace_disappeared(self) -> None:
        self.note_unavailable()
        if not self.restart_pending:
            self.reasons.add("source_reset")

    def note_poller_cancelled(self) -> None:
        self.reasons.add("poller_cancelled")

    def _reject_malformed(self, *reasons: str) -> None:
        self.malformed_sample_count += 1
        self.reasons.add("malformed_snapshot")
        self.reasons.update(reasons)

    def add_snapshot(self, snapshot: object) -> None:
        self.sample_count += 1
        if not isinstance(snapshot, dict):
            self._reject_malformed()
            return
        if snapshot.get("conversation_id") != self.conversation_id:
            self._reject_malformed("conversation_mismatch")
            return
        events = snapshot.get("events")
        dropped = snapshot.get("dropped_event_count")
        event_count = snapshot.get("event_count")
        if (
            not isinstance(events, list)
            or type(dropped) is not int
            or dropped < 0
            or type(event_count) is not int
            or event_count != len(events)
        ):
            self._reject_malformed()
            return

        seqs: list[int] = []
        encoded: dict[int, bytes] = {}
        try:
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("inspect event is not an object")
                seq = event.get("seq")
                if type(seq) is not int or seq <= 0:
                    raise ValueError("inspect sequence is not an exact positive integer")
                if seqs and seq <= seqs[-1]:
                    self._reject_malformed("snapshot_sequence_regression")
                    return
                try:
                    self._projection_for_event(event)
                except ValueError:
                    self._reject_malformed("ambiguous_event_discriminator")
                    return
                seqs.append(seq)
                encoded[seq] = self._canonical_event(event)
        except (TypeError, ValueError):
            self._reject_malformed()
            return

        self.source_max_dropped_count = max(self.source_max_dropped_count, dropped)
        if self.event_bearing_sample_count == 0 and dropped > 0:
            self.reasons.add("first_snapshot_already_dropped")

        if self.last_source_dropped_count is not None and dropped < self.last_source_dropped_count:
            self.reasons.update({"dropped_count_regression", "source_reset"})
            return

        conflicts = [
            seq
            for seq, content in encoded.items()
            if seq in self.canonical_events and self.canonical_events[seq] != content
        ]
        if conflicts:
            self.conflict_count += len(conflicts)
            self.reasons.add("event_content_conflict")
            return

        current = tuple(seqs)
        previous = self.last_snapshot_seqs
        if previous:
            previous_set = set(previous)
            current_set = set(current)
            overlap = tuple(seq for seq in current if seq in previous_set)
            if overlap:
                self.overlap_sample_count += 1
                first_overlap = overlap[0]
                previous_suffix = previous[previous.index(first_overlap) :]
                current_prefix = current[: len(overlap)]
                if previous_suffix != overlap or current_prefix != overlap:
                    self.reasons.add("snapshot_sequence_regression")
                    return
            elif current != previous:
                if dropped > (self.last_source_dropped_count or 0):
                    self.reasons.add("eviction_gap_no_overlap")
                else:
                    self.reasons.add("source_reset")
                return

            previous_max = previous[-1]
            if any(seq <= previous_max and seq not in self.canonical_events for seq in current):
                self.reasons.add("snapshot_sequence_regression")
                return
            if current and current[-1] < previous_max:
                self.reasons.update({"snapshot_sequence_regression", "source_reset"})
                return
            if dropped > (self.last_source_dropped_count or 0) and not overlap:
                self.reasons.add("eviction_gap_no_overlap")
                return

            # A fixed ring evicts oldest-first and only while appending. A risen
            # drop count therefore requires exactly the leading `evicted` prior
            # members to disappear AND a genuinely new suffix to have arrived. A
            # byte-identical window (or any other surviving set) cannot raise
            # the counter; that transition is a reset/corruption, never green.
            # The rule holds whether or not the earlier sample was below ring
            # capacity: it uses only the delta-versus-last-accepted facts.
            if overlap and dropped > (self.last_source_dropped_count or 0):
                evicted = dropped - (self.last_source_dropped_count or 0)
                if (
                    overlap != previous[min(evicted, len(previous)) :]
                    or current[-1] <= previous[-1]
                ):
                    self.reasons.update({"impossible_dropped_transition", "source_reset"})
                    return

            # Without source eviction, a bounded ring cannot discard a prior
            # member. Missing members therefore prove reset/regression even when
            # the two windows happen to overlap at one sequence.
            if dropped == self.last_source_dropped_count and not previous_set <= current_set:
                self.reasons.add("snapshot_sequence_regression")
                return

        self.canonical_events.update(encoded)
        self.accepted_sample_count += 1
        # The license closes only once the declared outage has been observed AND
        # the trace answers again — that is the window's true far edge. Clearing
        # it on any accepted sample would close it before the server even died.
        if self.restart_outage_seen:
            self.restart_pending = False
            self.restart_outage_seen = False
        if current:
            self.event_bearing_sample_count += 1
        self.last_source_dropped_count = dropped
        self.last_snapshot_seqs = current

    def finish(self) -> None:
        if self.finalized:
            return
        if self.accepted_sample_count == 0:
            self.reasons.add("no_valid_snapshot")
        self.finalized = True

    def active_agent_step_request_id(self) -> str | None:
        """Return the one currently open host-owned driver request, if provable.

        An ``agent.step`` span starts before the provider stream and ends in the
        span context manager on success, failure, timeout, or cancellation.  A
        latest unmatched start therefore proves that the product is still doing
        model work even though no durable ActionEvent exists yet.

        This is deliberately strict.  A tainted aggregate, a missing/foreign
        request id, duplicate starts, multiple unmatched requests, or any later
        driver-span event fails closed.  Those shapes must never turn an old
        observability record into an unbounded inactivity exemption.
        """

        if self.reasons or self.accepted_sample_count == 0:
            return None

        open_requests: dict[str, int] = {}
        latest: tuple[int, str, str] | None = None
        for seq in sorted(self.canonical_events):
            try:
                event = json.loads(self.canonical_events[seq].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if (
                not isinstance(event, dict)
                or event.get("span") != "agent.step"
                or event.get("role") != "agent_driver"
            ):
                continue
            phase = event.get("event")
            request_id = event.get("request_id")
            if phase not in {"start", "end"} or not isinstance(request_id, str) or not request_id:
                return None
            if phase == "start":
                if request_id in open_requests:
                    return None
                open_requests[request_id] = seq
            else:
                if request_id not in open_requests:
                    return None
                del open_requests[request_id]
            latest = (seq, phase, request_id)

        if latest is None or latest[1] != "start" or len(open_requests) != 1:
            return None
        request_id = latest[2]
        return request_id if open_requests.get(request_id) == latest[0] else None

    def render(self) -> dict[str, Any]:
        events = [
            json.loads(self.canonical_events[seq].decode("utf-8"))
            for seq in sorted(self.canonical_events)
        ]
        projections: dict[str, list[dict[str, Any]]] = {
            field_name: [] for field_name in _INSPECT_PROJECTIONS.values()
        }
        for event in events:
            projection, kind_is_outer = self._projection_for_event(event)
            projections[projection].append(
                {
                    key: value
                    for key, value in event.items()
                    if key != "seq" and (key != "kind" or not kind_is_outer)
                }
            )

        reasons = [reason for reason in _INSPECT_AGGREGATION_REASON_ORDER if reason in self.reasons]
        lossless = self.finalized and self.accepted_sample_count > 0 and not reasons
        if lossless:
            continuity_reason = (
                "overlap_proven"
                if self.source_max_dropped_count > 0
                else "no_source_eviction_observed"
            )
        else:
            continuity_reason = reasons[0] if reasons else "collection_not_finalized"
        seqs = sorted(self.canonical_events)
        result: dict[str, Any] = {
            "conversation_id": self.conversation_id,
            "event_count": len(events),
            # This is the aggregate loss counter, not the source ring's.  It is
            # exactly zero only after the continuity proof closes.
            "dropped_event_count": 0 if lossless else None,
            "source_dropped_event_count": self.source_max_dropped_count,
            "events": events,
            **projections,
            "aggregation": {
                "schema_version": 1,
                "sample_count": self.sample_count,
                "accepted_sample_count": self.accepted_sample_count,
                "event_bearing_sample_count": self.event_bearing_sample_count,
                "pretrace_unavailable_count": self.pretrace_unavailable_count,
                "unavailable_sample_count": self.unavailable_sample_count,
                "malformed_sample_count": self.malformed_sample_count,
                "overlap_sample_count": self.overlap_sample_count,
                "source_max_dropped_count": self.source_max_dropped_count,
                "unique_event_count": len(events),
                "first_retained_seq": seqs[0] if seqs else None,
                "last_retained_seq": seqs[-1] if seqs else None,
                "continuity": "complete" if lossless else "incomplete",
                "continuity_reason": continuity_reason,
                "conflict_count": self.conflict_count,
                # Disclosed so the independent gate can tell a harness-declared
                # outage from silently swallowed evidence loss.
                "declared_restart_count": self.declared_restart_count,
                "failure_reasons": reasons,
                "lossless": lossless,
                "finalized": self.finalized,
            },
        }
        return result


_INSPECT_TRACE_TOP_LEVEL_KEYS = frozenset(
    {
        "conversation_id",
        "event_count",
        "dropped_event_count",
        "source_dropped_event_count",
        "events",
        "routing_decisions",
        "spans",
        "tool_scopes",
        "progress_shadows",
        "aggregation",
    }
)
_INSPECT_AGGREGATION_KEYS = frozenset(
    {
        "schema_version",
        "sample_count",
        "accepted_sample_count",
        "event_bearing_sample_count",
        "pretrace_unavailable_count",
        "unavailable_sample_count",
        "malformed_sample_count",
        "overlap_sample_count",
        "source_max_dropped_count",
        "unique_event_count",
        "first_retained_seq",
        "last_retained_seq",
        "continuity",
        "continuity_reason",
        "conflict_count",
        "declared_restart_count",
        "failure_reasons",
        "lossless",
        "finalized",
    }
)
_INSPECT_AGGREGATION_COUNTER_KEYS = (
    "sample_count",
    "accepted_sample_count",
    "event_bearing_sample_count",
    "pretrace_unavailable_count",
    "unavailable_sample_count",
    "malformed_sample_count",
    "overlap_sample_count",
    "source_max_dropped_count",
    "unique_event_count",
    "conflict_count",
    "declared_restart_count",
)


def _exact_nonneg_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _inspect_events_and_projections(
    trace: dict[str, Any], flag: Callable[[str], None]
) -> tuple[list[dict[str, Any]] | None, dict[str, list[dict[str, Any]]]]:
    """Validate the canonical event list and re-derive every projection.

    Returns ``(events, derived_projections)``; ``events`` is ``None`` when the
    list itself is unusable (further event-derived checks are meaningless)."""

    derived: dict[str, list[dict[str, Any]]] = {
        field_name: [] for field_name in _INSPECT_PROJECTIONS.values()
    }
    events = trace.get("events")
    if not isinstance(events, list):
        flag("events_not_a_list")
        return None, derived
    last_seq: int | None = None
    for event in events:
        if not isinstance(event, dict):
            flag("event_shape")
            return None, derived
        seq = event.get("seq")
        if type(seq) is not int or seq <= 0 or (last_seq is not None and seq <= last_seq):
            flag("event_sequence")
            return None, derived
        last_seq = seq
        try:
            _canonical_json_bytes(event)
            projection, kind_is_outer = _InspectTraceAggregation._projection_for_event(event)
        except (TypeError, ValueError):
            flag("event_not_canonical")
            return None, derived
        derived[projection].append(
            {
                key: value
                for key, value in event.items()
                if key != "seq" and (key != "kind" or not kind_is_outer)
            }
        )
    return [dict(event) for event in events], derived


def inspect_aggregate_violations(trace: object, *, conversation_id: str) -> list[str]:
    """Bounded internal-consistency violations of a rendered inspect aggregate.

    The required-inspect gate must not trust the caller-owned ``lossless`` /
    ``finalized`` / ``dropped_event_count`` assertions: a forged or tampered
    dict can wear those values around an internally inconsistent trace.  This
    validator re-derives every projection and coherence fact from the canonical
    events and returns a deduplicated, bounded list of short violation labels;
    an empty list means the aggregate is exactly the shape ``render`` produces
    for this conversation.  It shares the aggregation's own constants so the
    producer and the gate cannot drift apart.
    """

    if not isinstance(trace, dict):
        return ["trace_not_an_object"]
    violations: list[str] = []

    def flag(label: str) -> None:
        if label not in violations:
            violations.append(label)

    if set(trace) != _INSPECT_TRACE_TOP_LEVEL_KEYS:
        flag("top_level_keys")
    cid = trace.get("conversation_id")
    if not isinstance(cid, str) or cid != conversation_id:
        flag("conversation_id")
    aggregation = trace.get("aggregation")
    if not isinstance(aggregation, dict):
        flag("aggregation_missing")
        return violations
    if set(aggregation) != _INSPECT_AGGREGATION_KEYS:
        flag("aggregation_keys")
    schema_version = aggregation.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        flag("schema_version")
    counters_valid = True
    for key in _INSPECT_AGGREGATION_COUNTER_KEYS:
        if not _exact_nonneg_int(aggregation.get(key)):
            flag(f"counter:{key}")
            counters_valid = False
    lossless = aggregation.get("lossless")
    finalized = aggregation.get("finalized")
    if type(lossless) is not bool or type(finalized) is not bool:
        flag("lossless_finalized_types")
        return violations

    reasons_raw = aggregation.get("failure_reasons")
    reasons: list[str] | None = None
    if isinstance(reasons_raw, list) and all(isinstance(item, str) for item in reasons_raw):
        canonical = [
            reason for reason in _INSPECT_AGGREGATION_REASON_ORDER if reason in set(reasons_raw)
        ]
        if list(reasons_raw) == canonical:
            reasons = canonical
    if reasons is None:
        flag("failure_reasons")

    events, derived = _inspect_events_and_projections(trace, flag)
    if events is not None:
        count = len(events)
        event_count = trace.get("event_count")
        if (
            type(event_count) is not int
            or event_count != count
            or aggregation.get("unique_event_count") != count
            or type(aggregation.get("unique_event_count")) is not int
        ):
            flag("event_count")
        expected_first = events[0]["seq"] if events else None
        expected_last = events[-1]["seq"] if events else None
        for key, expected in (
            ("first_retained_seq", expected_first),
            ("last_retained_seq", expected_last),
        ):
            actual = aggregation.get(key)
            if expected is None:
                if actual is not None:
                    flag(f"retained_seq:{key}")
            elif type(actual) is not int or actual != expected:
                flag(f"retained_seq:{key}")
        for field_name in _INSPECT_PROJECTIONS.values():
            try:
                actual_bytes = _canonical_json_bytes(trace.get(field_name))
            except (TypeError, ValueError):
                flag(f"projection:{field_name}")
                continue
            if actual_bytes != _canonical_json_bytes(derived[field_name]):
                flag(f"projection:{field_name}")

    if reasons is None or not counters_valid:
        return violations

    def counter(key: str) -> int:
        # counters_valid proved every counter is an exact non-negative int; the
        # type re-check only narrows for the type checker.
        value = aggregation.get(key)
        return value if type(value) is int else 0

    accepted = counter("accepted_sample_count")
    expected_lossless = finalized and accepted > 0 and not reasons
    if lossless != expected_lossless:
        flag("lossless_incoherent")
    dropped = trace.get("dropped_event_count")
    if lossless:
        if type(dropped) is not int or dropped != 0:
            flag("dropped_event_count")
    elif dropped is not None:
        flag("dropped_event_count")
    if aggregation.get("continuity") != ("complete" if lossless else "incomplete"):
        flag("continuity")
    source_max = counter("source_max_dropped_count")
    if lossless:
        expected_reason = "overlap_proven" if source_max > 0 else "no_source_eviction_observed"
    else:
        expected_reason = reasons[0] if reasons else "collection_not_finalized"
    if aggregation.get("continuity_reason") != expected_reason:
        flag("continuity_reason")
    top_source = trace.get("source_dropped_event_count")
    if type(top_source) is not int or top_source != source_max:
        flag("source_dropped_event_count")
    if finalized and accepted == 0 and "no_valid_snapshot" not in reasons:
        flag("no_valid_snapshot_missing")

    sample_count = counter("sample_count")
    event_bearing = counter("event_bearing_sample_count")
    if (
        accepted
        + counter("pretrace_unavailable_count")
        + counter("unavailable_sample_count")
        + counter("malformed_sample_count")
        > sample_count
        or event_bearing > accepted
        or counter("overlap_sample_count") > sample_count
    ):
        flag("sample_accounting")
    if events is not None:
        if events and event_bearing == 0:
            flag("sample_accounting")
        if not events and event_bearing > 0:
            flag("sample_accounting")

    # Counter <-> reason coherence: render can never disclose a rejected,
    # unavailable, or malformed sample — or source eviction under a lossless
    # verdict — without the matching taint, so a forged aggregate cannot wear
    # green around disclosed evidence loss.
    if counter("conflict_count") > 0 and "event_content_conflict" not in reasons:
        flag("conflict_reason_incoherent")
    if (
        counter("unavailable_sample_count") > 0
        and "inspect_unavailable" not in reasons
        and counter("declared_restart_count") == 0
    ):
        # Unavailable samples with no taint are legitimate ONLY as the outage a
        # restart scenario declared and caused. Absent a declared restart this is
        # still swallowed evidence loss, and still fails.
        flag("unavailable_reason_incoherent")
    if counter("malformed_sample_count") > 0 and "malformed_snapshot" not in reasons:
        flag("malformed_reason_incoherent")
    if lossless and source_max > 0 and counter("overlap_sample_count") == 0:
        flag("overlap_continuity_incoherent")
    return violations


def _live_thrash_finding_is_current(
    events: list[dict[str, Any]], failing_result: dict[str, Any]
) -> bool:
    """Whether a failing oracle result still describes the run's CURRENT state.

    k6g F2.4: historical lifetime evidence alone must never kill a resumed
    productive run. A finding is current only when NO trusted progress-epoch
    boundary (receipt-backed mutation, approved plan transition, user turn,
    typed blocking obligation) landed after the finding's latest contributing
    event. A finding that references no event seqs (e.g. hidden-repair counts
    from inspect spans) stays current — there is no progress evidence to
    supersede it, and failing open would un-bound genuine spend.
    """

    facts = failing_result.get("facts")
    if not isinstance(facts, dict):
        return True
    referenced: list[int] = []
    for key in ("action_seqs", "seqs", "cleanup_seqs"):
        values = facts.get(key)
        if isinstance(values, list):
            referenced.extend(value for value in values if type(value) is int)
    markers = facts.get("terminal_markers")
    if isinstance(markers, list):
        referenced.extend(
            marker["seq"]
            for marker in markers
            if isinstance(marker, dict) and type(marker.get("seq")) is int
        )
    if not referenced:
        return True
    latest_referenced = max(referenced)
    known_action_ids = frozenset(
        str(action_id_of(event))
        for event in events
        if kind_of(event) == KIND_ACTION and action_id_of(event)
    )
    for event in events:
        seq = event.get("seq")
        if (
            type(seq) is int
            and seq > latest_referenced
            and progress_epoch_boundary(event, action_ids=known_action_ids)
        ):
            return False
    return True


# ---- the live client --------------------------------------------------------


class DiscoApiClient:
    def __init__(
        self,
        transport: Transport,
        *,
        db_path: str,
        poll_interval_s: float = 1.0,
        projects_root: str | None = None,
        snapshot_wait_s: float = 0.0,
        require_workspace_commit: bool = False,
    ) -> None:
        self._t = transport
        self._db_path = db_path
        self._poll = poll_interval_s
        # ProjectStore root for the authoritative workspace SNAPSHOT read (Bug 9 fix).
        # None ⇒ authoritative snapshot read DISABLED and workspace collection fails
        # closed. The live CLI passes "" so it mirrors the agent-server's OWN default
        # root resolution (same env); deterministic tests inject a temporary ProjectStore.
        self._projects_root = projects_root
        # How long to wait for the snapshot to flush after the run reaches a terminal
        # state — the build appends FINISHED INSIDE loop.run(), then `_maybe_snapshot`
        # writes the workspace to the ProjectStore; a fast collect can read between the
        # two. Re-read the snapshot until every declared file is present (or this budget
        # elapses) so a genuinely-present file is never reported missing.
        self._snapshot_wait_s = snapshot_wait_s
        # Live evidence freezes only a fully published ProjectStore version.  The
        # explicit option exists solely for legacy deterministic adapter fixtures
        # that model file readiness without running the product persistence layer.
        self._require_workspace_commit = require_workspace_commit
        self._collected_workspace_dirs: dict[str, Path] = {}
        self._verified_workspace_cache = tempfile.TemporaryDirectory(prefix="disco-soak-verified-")
        # The conversation_id this client most recently CREATED (set in
        # create_build_conversation). Lets the runner reach the cid for teardown even when
        # drive_scenario raised before returning a CollectedRun (so an abandoned RUNNING
        # build can still be killed from run_once's finally). None until a create succeeds.
        self.last_conversation_id: str | None = None
        # Scenario-owned product evidence captured through real public surfaces
        # (import, pause/resume, restart, rollback, export). Reset for every run by
        # run_once; unknown keys are intentionally forward-compatible in the
        # product-evidence dossier.
        self.scenario_evidence: dict[str, Any] = {}
        # Live soak observability. The terminal classifier remains authoritative,
        # but this sampler proves the model/tool trace was inspected while the
        # run was active and records the first persistently bad threshold crossing.
        self._live_thrash_scenario: dict[str, Any] | None = None
        self._live_thrash_samples = 0
        self._live_thrash_findings: list[dict[str, Any]] = []
        self._live_thrash_candidate = ""
        self._live_thrash_candidate_count = 0
        self._live_thrash_recorded: set[str] = set()
        self._live_thrash_last_sample = time.monotonic()
        self._inspect_aggregations: dict[str, _InspectTraceAggregation] = {}
        self._inspect_poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._inspect_stop_events: dict[str, asyncio.Event] = {}
        self._inspect_sample_locks: dict[str, asyncio.Lock] = {}
        self._inspect_finish_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._efficiency_progress: LiveEfficiencyProgress | None = None
        self._efficiency_sample_interval_s = 5.0
        self._efficiency_last_sample = 0.0
        self._efficiency_started_at = 0.0

    def enable_efficiency_progress(self, progress: LiveEfficiencyProgress) -> None:
        """Attach a bounded live work-cost readout to the terminal wait.

        Observability only: it reads the evidence the poll loop already writes
        (durable events + the in-memory inspect trace) and makes ZERO provider
        calls. Sampling is rate-limited here and emission is rate-limited again
        inside the progress object, so a sub-second poll cannot flood the log.
        """
        self._efficiency_progress = progress
        self._efficiency_last_sample = 0.0
        self._efficiency_started_at = time.monotonic()

    async def _emit_efficiency_progress(self, conversation_id: str, *, state: str) -> None:
        progress = self._efficiency_progress
        if progress is None:
            return
        now = time.monotonic()
        if self._efficiency_last_sample and (
            now - self._efficiency_last_sample < self._efficiency_sample_interval_s
        ):
            return
        started = self._efficiency_started_at or now
        self._efficiency_last_sample = now
        try:
            events = self.collect_events(conversation_id)
            trace = await self.collect_inspect_trace(conversation_id)
        except Exception:  # noqa: BLE001 — a readout must never break the wait
            return
        record = efficiency_record(
            scenario_id="",
            conversation_id=conversation_id,
            events=events,
            trace=trace if isinstance(trace, dict) else None,
        )
        calls = record.get("provider_calls") or {}
        progress.update(
            now=now,
            elapsed_s=now - started,
            actions=record.get("actions"),
            planning_turns=record.get("planning_turns"),
            execution_turns=record.get("execution_turns"),
            provider_calls=calls.get("conversation_bound") if isinstance(calls, dict) else None,
            compactions=record.get("compactions"),
            repairs=record.get("model_repairs"),
            state=state,
        )

    def enable_live_thrash_monitor(self, scenario: dict[str, Any]) -> None:
        self._live_thrash_scenario = scenario
        self._live_thrash_samples = 0
        self._live_thrash_findings = []
        self._live_thrash_candidate = ""
        self._live_thrash_candidate_count = 0
        self._live_thrash_recorded = set()
        self._live_thrash_last_sample = time.monotonic()

    async def _fetch_inspect_snapshot(self, conversation_id: str) -> tuple[str, object | None]:
        try:
            status, data = await self._t.get_json(f"/api/debug/trace/{conversation_id}")
        except Exception:  # noqa: BLE001 — the aggregate records unavailability
            return "unavailable", None
        if (
            status == 404
            and isinstance(data, dict)
            and set(data) == {"error", "conversation_id"}
            and data.get("error") == "no trace"
            and data.get("conversation_id") == conversation_id
        ):
            return "pretrace", None
        if status >= 400:
            return "unavailable", None
        return "snapshot", data

    async def _sample_inspect_trace(self, conversation_id: str) -> None:
        aggregation = self._inspect_aggregations.get(conversation_id)
        lock = self._inspect_sample_locks.get(conversation_id)
        if aggregation is None or lock is None or aggregation.finalized:
            return
        async with lock:
            if aggregation.finalized:
                return
            disposition, snapshot = await self._fetch_inspect_snapshot(conversation_id)
            if disposition == "pretrace":
                if aggregation.accepted_sample_count == 0:
                    aggregation.note_expected_pretrace_absence()
                else:
                    # The exact 404/no-trace response is provisional only before
                    # the first source snapshot. Once a trace existed, its
                    # disappearance is observable restart/loss, not pretrace.
                    aggregation.note_trace_disappeared()
            elif disposition == "unavailable":
                aggregation.note_unavailable()
            else:
                aggregation.add_snapshot(snapshot)

    async def _poll_inspect_trace(self, conversation_id: str) -> None:
        aggregation = self._inspect_aggregations[conversation_id]
        stop = self._inspect_stop_events[conversation_id]
        interval = min(max(self._poll, 0.01), 0.25)
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    await self._sample_inspect_trace(conversation_id)
        except asyncio.CancelledError:
            aggregation.note_poller_cancelled()
            raise
        except Exception:  # noqa: BLE001 — retain evidence and fail continuity closed
            aggregation.note_unavailable()

    async def start_inspect_collection(self, conversation_id: str) -> None:
        """Idempotently begin polling before the user message kicks the model."""

        if conversation_id in self._inspect_aggregations:
            return
        self._inspect_aggregations[conversation_id] = _InspectTraceAggregation(conversation_id)
        self._inspect_stop_events[conversation_id] = asyncio.Event()
        self._inspect_sample_locks[conversation_id] = asyncio.Lock()
        # Take the earliest possible sample synchronously. A not-yet-created trace
        # is provisional: the first real dropped=0 snapshot proves nothing was lost.
        await self._sample_inspect_trace(conversation_id)
        self._inspect_poll_tasks[conversation_id] = asyncio.create_task(
            self._poll_inspect_trace(conversation_id),
            name=f"inspect-aggregate:{conversation_id}",
        )

    async def _finish_inspect_collection(self, conversation_id: str) -> dict[str, Any]:
        aggregation = self._inspect_aggregations[conversation_id]
        if aggregation.finalized:
            return aggregation.render()
        self._inspect_stop_events[conversation_id].set()
        poller = self._inspect_poll_tasks.get(conversation_id)
        if poller is not None:
            try:
                await poller
            except asyncio.CancelledError:
                # An independently cancelled poller taints continuity but never
                # deletes the canonical prefix already retained.
                aggregation.note_poller_cancelled()
        await self._sample_inspect_trace(conversation_id)
        aggregation.finish()
        return aggregation.render()

    async def finish_inspect_collection(self, conversation_id: str) -> dict[str, Any] | None:
        """Idempotently stop, take a final sample, and freeze the aggregate.

        The owned finish task is shielded so caller cancellation cannot replace an
        already-collected prefix with ``None``. A later cleanup caller awaits the
        same task before releasing the conversation.
        """

        if conversation_id not in self._inspect_aggregations:
            return None
        task = self._inspect_finish_tasks.get(conversation_id)
        if task is None:
            task = asyncio.create_task(
                self._finish_inspect_collection(conversation_id),
                name=f"inspect-finalize:{conversation_id}",
            )
            self._inspect_finish_tasks[conversation_id] = task
        return await asyncio.shield(task)

    def note_inspect_stop_unconfirmed(self, conversation_id: str) -> None:
        """Taint continuity when an ACTIVE conversation's stop was not confirmed.

        A stop that cannot be proven quiescent may leave the model emitting
        trace events after the final sample, so the aggregate must not claim
        losslessness.  The retained canonical prefix is preserved.  This is a
        no-op after finalization: a frozen aggregate was completed through the
        stop-first path and its verdict already stands.
        """

        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is not None and not aggregation.finalized:
            aggregation.reasons.add("active_stop_unconfirmed")

    def note_expected_stack_restart(self, conversation_id: str) -> None:
        """Declare the stack restart this run is about to perform.

        The drive knows it is killing the agent-server; the poller only sees the
        endpoint stop answering. Without the declaration the aggregate scores a
        harness-caused outage as evidence loss. Narrow by construction: see
        `_InspectTraceAggregation.note_expected_stack_restart`. A no-op after
        finalization, so a late call can never reopen a closed verdict.
        """

        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is not None and not aggregation.finalized:
            aggregation.note_expected_stack_restart()

    @property
    def live_thrash_monitor(self) -> dict[str, Any]:
        return {
            "enabled": self._live_thrash_scenario is not None,
            "sample_count": self._live_thrash_samples,
            "minimum_confirmation_samples": 2,
            "findings": list(self._live_thrash_findings),
        }

    def observe_live_thrash_snapshot(
        self,
        events: list[dict[str, Any]],
        inspect_trace: dict[str, Any] | None,
        *,
        terminal_status: str = "",
    ) -> bool:
        """Evaluate one in-run snapshot; require two matching live samples.

        A just-committed ActionEvent may not have its ObservationEvent yet. Two
        matching samples prevent that transient prefix from being reported as a
        tool-error finding. At a terminal state the event prefix is stable, so a
        single sample is conclusive.
        """

        scenario = self._live_thrash_scenario
        if scenario is None:
            return False
        self._live_thrash_samples += 1
        try:
            normalized_events = normalize_events(events)
        except NormalizationError as exc:
            # The frozen classifier owns INVALID_RUN adjudication for corrupt
            # evidence. A live sampler must never reinterpret a row-shaped or
            # malformed prefix as a stream of failed actions.
            _LOG.error("live thrash sampler could not normalize events: %s", exc)
            return False
        failed = [
            result.to_dict()
            for result in ThrashOracle().check(
                normalized_events,
                scenario=scenario,
                inspect_trace=inspect_trace,
            )
            if result.failed
        ]
        # k6g F2: a live KILL needs persistent CURRENT no-progress evidence. A
        # finding whose last contributing event precedes trusted progress (a
        # receipt-backed mutation, an approved plan transition, an answered
        # user turn, a typed blocking obligation) is historical: the terminal
        # classifier still adjudicates it on the frozen events, but the monitor
        # must not kill a resumed, progressing run over it.
        failed = [
            result
            for result in failed
            if _live_thrash_finding_is_current(normalized_events, result)
        ]
        if not failed:
            self._live_thrash_candidate = ""
            self._live_thrash_candidate_count = 0
            return False
        fingerprint = json.dumps(failed, sort_keys=True, separators=(",", ":"))
        if fingerprint == self._live_thrash_candidate:
            self._live_thrash_candidate_count += 1
        else:
            self._live_thrash_candidate = fingerprint
            self._live_thrash_candidate_count = 1
        terminal = terminal_status in TERMINAL_STATES or terminal_status == PAUSED_STATE
        if not terminal and self._live_thrash_candidate_count < 2:
            return False
        if fingerprint in self._live_thrash_recorded:
            return False
        self._live_thrash_recorded.add(fingerprint)
        finding = {
            "detected_at_epoch": time.time(),
            "terminal_status": terminal_status or None,
            "event_count": len(normalized_events),
            "max_event_seq": max(
                (int(event.get("seq", -1)) for event in normalized_events), default=-1
            ),
            "confirmation_samples": self._live_thrash_candidate_count,
            "oracle_results": failed,
        }
        self._live_thrash_findings.append(finding)
        _LOG.error(
            "live thrash threshold crossed conversation=%s findings=%s",
            self.last_conversation_id,
            failed,
        )
        return True

    async def _sample_live_thrash(self, conversation_id: str, *, terminal_status: str = "") -> bool:
        if self._live_thrash_scenario is None:
            return False
        now = time.monotonic()
        terminal = terminal_status in TERMINAL_STATES or terminal_status == PAUSED_STATE
        if not terminal and now - self._live_thrash_last_sample < 1.0:
            return False
        self._live_thrash_last_sample = now
        try:
            events = self.collect_events(conversation_id)
            trace = await self.collect_inspect_trace(conversation_id)
        except Exception:  # noqa: BLE001 — final evidence gate catches missing trace
            return False
        return self.observe_live_thrash_snapshot(
            events,
            trace,
            terminal_status=terminal_status,
        )

    # -- pre-create infra probe (§9; the ONLY infra source) -------------------

    async def pre_create_probe(self, model: str | None) -> None:
        """Probe the agent-server (and, transitively, the model provider) BEFORE
        creating a conversation. Raise InfraProbeError on a matched signature; the
        caller maps that to INFRA_FAILURE with bounded retry (per infra_signatures).

        We probe the server health endpoint — a connection error / 5xx here is a
        pre-create, runner-side infra condition. (A provider 429/5xx that only shows
        up mid-loop is POST-create and is therefore a PRODUCT outcome, never infra.)
        """
        try:
            status, _body = await self._t.health()
        except _PRECREATE_INFRA_ERRORS as exc:
            raise InfraProbeError(
                "agent_server_unreachable_before_conversation",
                {
                    "component": "agent_server",
                    "error_kind": _classify_conn_error(exc),
                    "error": str(exc),
                },
            ) from exc
        if status in (500, 502, 503, 504):
            raise InfraProbeError(
                "provider_5xx_before_conversation",
                {"component": "agent_server", "http_status": status},
            )
        if status == 429:
            raise InfraProbeError(
                "provider_429_before_conversation",
                {"component": "agent_server", "http_status": status},
            )
        # model is advisory here — the agent-server's own /health is the gate; a
        # bad model surfaces post-create as StatusEvent(ERROR) (a product outcome).

    # -- create + drive -------------------------------------------------------

    async def create_build_conversation(
        self,
        prompt: str,
        *,
        model: str | None = None,
        autonomous: bool = False,
        appkit: bool = False,
        surface: str = "build",
        import_fixture: dict[str, Any] | None = None,
        verification_requirements: dict[str, Any] | None = None,
    ) -> str:
        """POST /conversations then POST the user message (which the route appends
        AND kicks). Returns the conversation_id."""
        if surface not in {"build", "agent"}:
            raise ValueError(f"unsupported soak conversation surface: {surface!r}")
        if import_fixture is not None:
            if surface != "build" or appkit:
                raise ValueError("import fixtures are supported only for Freeform Build")
            if autonomous:
                raise ValueError("import fixtures must use the interactive approval path")
            cid = await self._create_imported_conversation(import_fixture)
            self.last_conversation_id = cid
            await self.start_inspect_collection(cid)
            mstatus, _ = await self._t.post_json(
                f"/conversations/{cid}/messages",
                {
                    "content": prompt,
                    **(
                        {"verification_requirements": verification_requirements}
                        if verification_requirements is not None
                        else {}
                    ),
                },
            )
            if mstatus >= 400:
                raise RuntimeError(f"post_message failed: HTTP {mstatus}")
            return cid

        body: dict[str, Any] = {"surface": surface, "autonomous": autonomous}
        if appkit:
            # EPIC F strict AppKit mode — the phase-based allowlist build. The
            # appkit soak lane (deadlock regression cbfec1fd) sets this per
            # scenario; the route flips runtime.set_appkit_mode at create time.
            body["appkit_mode"] = True
        if model:
            body["model_override"] = model
        status, data = await self._t.post_json("/conversations", body)
        if status >= 400 or "conversation_id" not in data:
            raise RuntimeError(f"create_conversation failed: HTTP {status} {data}")
        cid = str(data["conversation_id"])
        # Record the created cid IMMEDIATELY (before the kick) so the runner can tear it
        # down even if the subsequent message POST / drive raises — a created-but-abandoned
        # conversation must never leak as a RUNNING build on the shared server.
        self.last_conversation_id = cid
        # BF2: inspect collection starts at the first instant the harness owns
        # the conversation id, before POST /messages can kick a model request.
        await self.start_inspect_collection(cid)
        # POST the user task — appends the USER message AND kicks the loop. AppKit
        # lanes mirror the production UI's initial frame: `build_brief` present makes
        # the route classify a Build Brief from the prompt (codex finding #11 — without
        # it the contract-activation path never fires for the soak).
        mbody: dict[str, Any] = {"content": prompt}
        if appkit:
            mbody["build_brief"] = {}
        if verification_requirements is not None:
            mbody["verification_requirements"] = verification_requirements
        mstatus, _ = await self._t.post_json(f"/conversations/{cid}/messages", mbody)
        if mstatus >= 400:
            raise RuntimeError(f"post_message failed: HTTP {mstatus}")
        return cid

    async def _create_imported_conversation(self, fixture: dict[str, Any]) -> str:
        """Seed a real imported project through ``/api/projects/import``.

        The fixture is deliberately small and data-only: a safe archive filename
        plus a mapping of relative POSIX paths to UTF-8 text. The product endpoint
        remains responsible for its own independent archive validation and project
        provenance; the runner merely constructs the user-supplied zip bytes.
        """
        filename = fixture.get("filename")
        files = fixture.get("files")
        if not isinstance(filename, str) or not filename.endswith(".zip"):
            raise ValueError("import_fixture.filename must be a .zip filename")
        if not isinstance(files, dict) or not files:
            raise ValueError("import_fixture.files must be a non-empty mapping")
        archive = io.BytesIO()
        total_bytes = 0
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for raw_path, text_spec in sorted(files.items()):
                if not isinstance(raw_path, str) or not raw_path:
                    raise ValueError("import_fixture file paths must be non-empty strings")
                normalized = posixpath.normpath(raw_path.replace("\\", "/"))
                if (
                    normalized in {"", ".", ".."}
                    or normalized.startswith("../")
                    or posixpath.isabs(normalized)
                    or normalized != raw_path.replace("\\", "/")
                ):
                    raise ValueError(f"unsafe import_fixture path: {raw_path!r}")
                if isinstance(text_spec, str):
                    text = text_spec
                elif (
                    isinstance(text_spec, dict)
                    and set(text_spec) == {"line_template", "count"}
                    and isinstance(text_spec.get("line_template"), str)
                    and type(text_spec.get("count")) is int
                    and 1 <= int(text_spec["count"]) <= 100_000
                ):
                    template = str(text_spec["line_template"])
                    text = "".join(
                        template.replace("{{row}}", str(row))
                        for row in range(1, int(text_spec["count"]) + 1)
                    )
                else:
                    raise ValueError(
                        "import_fixture file contents must be UTF-8 text or a bounded "
                        "line_template/count generator"
                    )
                encoded = text.encode("utf-8")
                total_bytes += len(encoded)
                bundle.writestr(normalized, encoded)
        status, data = await self._t.post_file(
            "/api/projects/import",
            field="file",
            filename=filename,
            content=archive.getvalue(),
            content_type="application/zip",
        )
        if status >= 400 or "conversation_id" not in data:
            raise RuntimeError(f"import_project failed: HTTP {status} {data}")
        cid = str(data["conversation_id"])
        accepted = data.get("files") == len(files) and data.get("bytes") == total_bytes
        self.scenario_evidence["import"] = {
            "accepted": accepted,
            "file_count": data.get("files"),
            "bytes": data.get("bytes"),
            "filename": filename,
        }
        return cid

    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan via the REAL WS plan gate (routes/ws.py:95)."""
        await self._t.ws_control(conversation_id, {"type": "approve_plan"})

    async def confirm(self, conversation_id: str) -> None:
        """Confirm a pending RISKY ACTION via the REAL WS confirmation gate
        (routes/ws.py:89 -> runtime.confirm -> ControlOps.confirm -> loop.confirm() + kick).
        This is the confirmation ANALOGUE of approve_plan, NOT a free-text message: a plain
        user `send_message` does NOT clear a WAITING_FOR_CONFIRMATION gate — only the dedicated
        `confirm` control frame executes the pending action and resumes the loop past the gate."""
        await self._t.ws_control(conversation_id, {"type": "confirm"})

    async def resolve_decision(
        self, conversation_id: str, *, preferred_option_id: str | None = None
    ) -> dict[str, Any] | None:
        """Auto-resolve an AWAITING_USER_DECISION gate by picking one of the agent's proposed
        alternatives — the runner ACTS AS THE USER for a non-interactive soak. Routed through
        the REAL decision mechanism: the WS `pick_alternative` frame (routes/ws.py:108 →
        runtime.pick_alternative), NOT approve_plan (a plan gate) — the loop synthesizes the
        picked option's ToolCall as the next action and resumes.

        STATE-BIND before selecting (never auto-resolve the wrong / a stale branch): re-read
        the CURRENT conversation state and require it is STILL AWAITING_USER_DECISION with a
        LIVE `pending_alternatives_id` (the state machine recomputes this from the full event
        log every read, so it is always the live pending gate). Bind the options to THAT id by
        reading the matching AlternativesEvent from the durable log; require a non-empty option
        list with valid ids. Selection: the caller's `preferred_option_id` (scenario override)
        if it names a valid option, else a recommended option (recommendation flag), else the
        first valid option.

        Returns {alternatives_id, option_id} on a successful pick, or None when the gate is no
        longer live / the payload is invalid (no pending id, no valid options) — the caller
        treats None as a HARD signal (never a clean pass)."""
        state = await self.get_state(conversation_id)
        if self._status_of(state) != AWAITING_USER_DECISION:
            return None
        alt_id = state.get("pending_alternatives_id")
        if not alt_id:
            return None
        options = self._alternatives_options(conversation_id, str(alt_id))
        chosen = _choose_alternative(options, preferred_option_id)
        if chosen is None:
            return None
        await self._t.ws_control(conversation_id, {"type": "pick_alternative", "option_id": chosen})
        return {"alternatives_id": str(alt_id), "option_id": chosen}

    def _alternatives_options(
        self, conversation_id: str, alternatives_id: str
    ) -> list[dict[str, Any]]:
        """The option list of the AlternativesEvent whose id == `alternatives_id` (the live
        pending gate), read from the durable event log. Empty when absent / malformed."""
        try:
            events = self._read_events(conversation_id)
        except sqlite3.Error:
            return []
        for e in events:
            if e.get("kind") != "alternatives":
                continue
            p = _payload(e)
            if str(e.get("id") or p.get("id") or "") != str(alternatives_id):
                continue
            opts = p.get("options")
            if isinstance(opts, list):
                return [o for o in opts if isinstance(o, dict)]
        return []

    async def resume(self, conversation_id: str) -> dict[str, Any]:
        """Resume a PAUSED (cooperative / actionless) run — the runner ACTS AS THE
        USER who hits Resume. POST /conversations/{cid}/resume (conversations.py:284,
        the same mode-agnostic path the WS `resume` frame uses). A 409 (not resumable)
        is returned as-is so the caller can stop retrying.

        The HTTP code is returned under `http_status` (NOT `status`): the resume body
        itself carries a ``status`` field (e.g. ``{"ok": true, "status": "RUNNING"}``), so
        merging it under the same key would clobber the HTTP int with the body's STATE
        STRING — the caller's ``int(resp["status"])`` then crashed the whole drive with
        ``ValueError: invalid literal for int() ... 'RUNNING'`` the moment a build paused."""
        status, data = await self._t.post_json(f"/conversations/{conversation_id}/resume", {})
        return {"http_status": status, **(data if isinstance(data, dict) else {})}

    async def pause(self, conversation_id: str) -> None:
        """Request the real cooperative pause control over the product WebSocket."""
        await self._t.ws_control(conversation_id, {"type": "pause"})

    async def restore_workspace_version(
        self, conversation_id: str, *, selector: str = "oldest"
    ) -> dict[str, Any]:
        """Restore a durable project version through the public history routes.

        The product publishes its `workspace_version` events as part of the
        FINISHED finalization pipeline, shortly AFTER the terminal status flips
        (observed ~1.3s on a loaded host; the UI consumes the event stream, so a
        human cannot lose this race). Keying the drive on the terminal status
        alone therefore needs the same bounded flush wait the workspace snapshot
        read already applies (`snapshot_wait_s`): poll the public versions route
        to the deadline, then adjudicate availability truthfully. A build whose
        versions never publish still fails as `versions_unavailable`.
        """
        deadline = time.monotonic() + self._snapshot_wait_s
        while True:
            status, data = await self._t.get_json(f"/conversations/{conversation_id}/versions")
            versions = data.get("versions") if isinstance(data, dict) else None
            if status < 400 and isinstance(versions, list) and versions:
                break
            if time.monotonic() >= deadline:
                return {"ok": False, "http_status": status, "reason": "versions_unavailable"}
            await asyncio.sleep(0.5)
        valid = [row for row in versions if isinstance(row, dict) and type(row.get("seq")) is int]
        if not valid:
            return {"ok": False, "http_status": status, "reason": "versions_malformed"}
        if selector == "previous":
            ordered = sorted(valid, key=lambda row: int(row["seq"]), reverse=True)
            selected = ordered[1] if len(ordered) > 1 else ordered[0]
        else:
            selected = min(valid, key=lambda row: int(row["seq"]))
        selected_seq = int(selected["seq"])
        restore_status, result = await self._t.post_json(
            f"/conversations/{conversation_id}/versions/{selected_seq}/restore", {}
        )
        return {
            "ok": 200 <= restore_status < 300 and result.get("restored") == selected_seq,
            "http_status": restore_status,
            "selected_seq": selected_seq,
            "restored": result.get("restored"),
            "new_version": result.get("new_version"),
            "tree_digest": result.get("tree_digest"),
        }

    async def download_project(self, conversation_id: str) -> tuple[int, bytes]:
        status, content, _headers = await self._t.get_bytes(
            f"/api/projects/{conversation_id}/download"
        )
        return status, content

    async def kill(self, conversation_id: str) -> dict[str, Any]:
        """KILL (force-terminate) a conversation the runner is DONE with — the hygiene
        teardown so an abandoned RUNNING / PAUSED / AWAITING build the runner stopped
        watching (inconclusive cutoff, error path, or post-evidence release) does not
        leak and load the shared server. POST /conversations/{cid}/kill
        (conversations.py:275 — halts the agent, tears down its sandbox, revokes its
        capabilities). IDEMPOTENT: a kill on an already-terminal conversation is harmless.

        The HTTP code is returned under `http_status` (mirrors :meth:`resume`); the route
        body is ``{"killed": true, "state": {...}}``. Returns ``{"http_status": int, ...}``."""
        status, data = await self._t.post_json(f"/conversations/{conversation_id}/kill", {})
        return {"http_status": status, **(data if isinstance(data, dict) else {})}

    async def send_followup(
        self, conversation_id: str, text: str, *, kind: str = "message"
    ) -> None:
        """Send a follow-up over the REAL WS path. `kind`:
        * "message"      -> {"type":"send_message"}  (after-terminal user follow-up:
                            appended + kick; the PRODUCT decides to re-plan)
        * "steer"        -> {"type":"steer"}         (mid-run redirect of a RUNNING
                            agent — the §15.4 after-first-file-write injection)
        * "request_plan" -> {"type":"request_plan"}  (explicit re-plan gate)
        """
        if kind == "steer":
            frame = {"type": "steer", "steer_text": text}
        elif kind == "request_plan":
            frame = {"type": "request_plan", "content": text}
        else:
            frame = {"type": "send_message", "content": text}
        await self._t.ws_control(conversation_id, frame)

    # -- polling --------------------------------------------------------------

    async def get_state(self, conversation_id: str) -> dict[str, Any]:
        status, data = await self._t.get_json(f"/conversations/{conversation_id}/state")
        if status >= 400:
            raise RuntimeError(f"get_state failed: HTTP {status}")
        return data

    @staticmethod
    def _status_of(state: dict[str, Any]) -> str:
        return str(state.get("execution_status") or state.get("status") or "")

    def _progress_marker(self, conversation_id: str) -> tuple[int, int]:
        """A cheap PROGRESS fingerprint for the conversation: (event_count, max_seq) from
        the durable event log. Either advancing means the build is STILL PRODUCING new
        events — the signal the progress-aware wait resets its inactivity timer on (Bug 15).
        A missing/locked DB reads as no-progress (-1) rather than crashing the poll."""
        uri = f"file:{self._db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return (0, -1)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(seq), -1) FROM events WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        except sqlite3.Error:
            return (0, -1)
        finally:
            conn.close()
        return (int(row[0]), int(row[1])) if row else (0, -1)

    def _durable_action_state(
        self, conversation_id: str
    ) -> tuple[tuple[int, int], tuple[str, str] | None] | None:
        """Read one durable progress/action snapshot, or fail closed on bad evidence.

        The returned marker and action pairing come from the SAME SQLite read. This
        prevents a response persisted between separate marker/pairing reads from being
        mistaken for inactivity. Only the most recent action can still be executing:
        an older missing response followed by a newer completed action is an evidence
        defect, not a reason to grant another tool deadline.
        """
        try:
            raw_events = self._read_events(conversation_id)
            events = normalize_events(raw_events)
        except (sqlite3.Error, NormalizationError, TypeError, ValueError):
            return None

        # Payload fields normally mirror these indexed row columns. A mismatch is
        # corrupt evidence: fail closed instead of letting payload-wins normalization
        # oscillate against the SQL progress marker or invent an in-flight action.
        row_identity = [
            (event.get("seq"), event.get("id"), event.get("kind"), event.get("source"))
            for event in raw_events
        ]
        payload_identity = [
            (event.get("seq"), event.get("id"), event.get("kind"), event.get("source"))
            for event in events
        ]
        if row_identity != payload_identity:
            return None

        marker = (
            len(raw_events),
            max((int(event["seq"]) for event in raw_events), default=-1),
        )

        for event in events:
            kind = event.get("kind")
            if kind not in {KIND_ACTION, KIND_OBSERVATION, KIND_AGENT_ERROR}:
                continue
            if seq_of(event) < 0:
                return None
            if kind == KIND_ACTION and (event.get("id") is None or tool_name_of(event) is None):
                return None

        latest_action = next(
            (event for event in reversed(events) if event.get("kind") == KIND_ACTION),
            None,
        )
        if latest_action is None:
            return marker, None

        action_id = str(latest_action["id"])
        action_seq = seq_of(latest_action)
        tool_name = tool_name_of(latest_action)
        if tool_name is None:  # guarded above; keep the trust boundary explicit
            return None
        paired = any(
            event.get("kind") in {KIND_OBSERVATION, KIND_AGENT_ERROR}
            and event.get("action_id") is not None
            and str(event["action_id"]) == action_id
            and seq_of(event) > action_seq
            for event in events
        )
        dangling = None if paired else (action_id, tool_name)
        return marker, dangling

    def _latest_dangling_action(self, conversation_id: str) -> tuple[str, str] | None:
        """Return the most recent durable action iff it lacks a later typed response."""
        state = self._durable_action_state(conversation_id)
        return None if state is None else state[1]

    def _active_agent_step_request_id(self, conversation_id: str) -> str | None:
        """Return a current same-conversation model request from trusted inspect state."""

        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is None or aggregation.conversation_id != conversation_id:
            return None
        return aggregation.active_agent_step_request_id()

    @staticmethod
    def _action_timeout_s(tool_name: str) -> float:
        return _TOOL_TIMEOUT_OVERRIDES_S.get(tool_name, _DEFAULT_TOOL_TIMEOUT_S)

    async def _poll_progress_aware(
        self,
        conversation_id: str,
        *,
        stop_on_gate: bool,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str:
        """The shared PROGRESS-AWARE terminal wait (Bug 15). Poll GET /state until EITHER:
          (a) a genuine terminal / gate / pause status is reached → return it; OR
          (b) the build goes GENUINELY INACTIVE — no NEW events/status change for
              `inactivity_s`, and no durable action remains legally in flight through its
              product timeout + persistence grace → return INACTIVE_TIMEOUT; OR
          (c) the generous `hard_cap_s` ceiling is hit. If the build was STILL progressing
              within the last inactivity window when the cap hit → PROGRESSING_TIMEOUT
              (inconclusive, NOT a product fail); otherwise → INACTIVE_TIMEOUT.

        A still-actively-progressing build is NEVER cut off by a mere wall-clock elapsing:
        the inactivity timer RESETS whenever the event count / max seq advances OR the status
        transitions. A durable unpaired action suppresses only the ordinary inactivity window;
        its own bounded deadline and the unchanged hard ceiling still end the wait.

        IDLE RACE (live-surfaced): POST /messages KICKS the loop ASYNCHRONOUSLY, so a freshly
        -created conversation reads IDLE for a beat before the kick stamps RUNNING. IDLE is
        terminal for adjudication but ambiguous as a DRIVE-STOP — pre-kick "not started yet"
        vs parked "nothing left". We only stop on IDLE once the run has gone active at least
        once (`seen_active`); a pre-kick IDLE is ignored so we don't bail before it plans."""
        start = time.monotonic()
        last_progress = start
        marker = self._progress_marker(conversation_id)
        last_status: str | None = None
        seen_active = False
        dangling_action_id: str | None = None
        dangling_action_deadline = 0.0
        while True:
            state = await self.get_state(conversation_id)
            last = self._status_of(state)
            await self._emit_efficiency_progress(conversation_id, state=last)
            thrash_crossed = await self._sample_live_thrash(conversation_id, terminal_status=last)
            if thrash_crossed and last not in TERMINAL_STATES:
                # A confirmed oracle failure is a spend/reliability stop, not a
                # log message. Kill the campaign-owned conversation immediately;
                # the frozen event stream still classifies the concrete failure.
                try:
                    killed = await self.kill(conversation_id)
                    _LOG.error(
                        "stopped conversation=%s after live thrash threshold (http=%s)",
                        conversation_id,
                        killed.get("http_status"),
                    )
                except Exception as exc:  # noqa: BLE001 - retain the hard stop
                    _LOG.error(
                        "failed to kill conversation=%s after live thrash threshold: %s",
                        conversation_id,
                        type(exc).__name__,
                    )
                return LIVE_THRASH_STOP
            if stop_on_gate and last in GATE_STATES:
                return last
            # A WORK terminal only ENDS the wait when it is the follow-up's OWN new terminal
            # (`min_terminal_seq` guard): with a baseline set, a terminal whose durable event
            # seq <= the baseline is the STALE pre-follow-up terminal — do NOT return on it,
            # keep waiting for the follow-up's own new terminal (seq > baseline). min=None
            # (the default, non-follow-up drive) ⇒ any terminal counts, unchanged behavior.
            if last in _WORK_TERMINALS and self._terminal_is_new(conversation_id, min_terminal_seq):
                return last
            if last == PAUSED_STATE:
                # a cooperative / actionless PAUSE — the driver decides (resume or stop).
                return last
            if last == "IDLE":
                if (
                    seen_active or self._idle_parked_durably(conversation_id, state)
                ) and self._terminal_is_new(conversation_id, min_terminal_seq):
                    return last
                # pre-kick / settling (or a stale pre-follow-up terminal) — keep waiting.
            else:
                seen_active = True  # RUNNING or any active transitional status

            now = time.monotonic()
            # Progress = new events appeared OR the status transitioned since last poll.
            cur_marker = self._progress_marker(conversation_id)
            if cur_marker != marker or last != last_status:
                marker = cur_marker
                last_status = last
                last_progress = now

            if last == "RUNNING":
                action_state = self._durable_action_state(conversation_id)
                if action_state is not None:
                    action_marker, dangling = action_state
                    if action_marker != marker:
                        marker = action_marker
                        last_progress = now
                    if dangling is None:
                        dangling_action_id = None
                        dangling_action_deadline = 0.0
                    else:
                        action_id, tool_name = dangling
                        if action_id != dangling_action_id:
                            dangling_action_id = action_id
                            dangling_action_deadline = (
                                now
                                + self._action_timeout_s(tool_name)
                                + _ACTION_RESULT_PERSISTENCE_GRACE_S
                            )
                # An unreadable/malformed snapshot never starts a deadline. If a
                # prior valid snapshot already proved one, retain its ORIGINAL bound:
                # clearing it here would let intermittent read failures restart the
                # same action's full timeout indefinitely on the next valid poll.

                # A provider request begins before the model can emit its next
                # ActionEvent.  The lossless inspect collector observes that exact,
                # host-owned boundary.  While its latest driver span is a single
                # unmatched start, the run is active rather than silent.  Refreshing
                # last_progress here does not remove the hard cap: a provider/router
                # wedge still ends as PROGRESSING_TIMEOUT (inconclusive), while a
                # completed, stale, foreign, or malformed span grants no extension.
                if self._active_agent_step_request_id(conversation_id) is not None:
                    last_progress = now
            else:
                dangling_action_id = None
                dangling_action_deadline = 0.0

            inactive_for = now - last_progress
            if now - start >= hard_cap_s:
                # Safety ceiling. Was it still progressing recently? Then this is an
                # inconclusive model-speed cutoff, NOT a wedge → PROGRESSING_TIMEOUT.
                return PROGRESSING_TIMEOUT if inactive_for < inactivity_s else INACTIVE_TIMEOUT
            if dangling_action_id is not None:
                if now >= dangling_action_deadline:
                    return INACTIVE_TIMEOUT
            elif inactive_for >= inactivity_s:
                return INACTIVE_TIMEOUT
            await asyncio.sleep(self._poll)

    async def poll_until_terminal_or_gate(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str:
        """Progress-aware wait that ALSO stops on a GATE the runner must act on (e.g.
        AWAITING_PLAN_APPROVAL). Returns the terminal/gate/pause status, or a Bug-15
        timeout sentinel (INACTIVE_TIMEOUT / PROGRESSING_TIMEOUT). `min_terminal_seq`
        (H1/V2): when set, a terminal whose durable event seq <= it is the STALE
        pre-follow-up terminal and does NOT end the wait — only the follow-up's OWN new
        terminal (seq > min_terminal_seq) does."""
        return await self._poll_progress_aware(
            conversation_id,
            stop_on_gate=True,
            inactivity_s=inactivity_s,
            hard_cap_s=hard_cap_s,
            min_terminal_seq=min_terminal_seq,
        )

    async def poll_until_terminal(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
        min_terminal_seq: int | None = None,
    ) -> str:
        """Progress-aware wait to a strictly TERMINAL state (no gate stop). Used after a
        plan is approved / for autonomous runs with no interactive gate. `min_terminal_seq`
        (H1/V2): only a terminal whose durable event seq > it ends the wait (the follow-up's
        OWN new terminal), never the stale pre-follow-up one."""
        return await self._poll_progress_aware(
            conversation_id,
            stop_on_gate=False,
            inactivity_s=inactivity_s,
            hard_cap_s=hard_cap_s,
            min_terminal_seq=min_terminal_seq,
        )

    async def wait_until_status_leaves(
        self, conversation_id: str, status: str, *, timeout_s: float
    ) -> str:
        """After acting on a gate (e.g. approving a plan), wait for the status to move
        OFF that gate before driving again — so a not-yet-processed approval isn't
        re-read as the same gate and double-approved. Returns the new status (or the
        unchanged one on timeout)."""
        deadline = time.monotonic() + timeout_s
        last = status
        while time.monotonic() < deadline:
            last = self._status_of(await self.get_state(conversation_id))
            if last != status:
                return last
            await asyncio.sleep(self._poll)
        return last

    def _latest_plan_revision(self, conversation_id: str) -> int:
        """Highest plan-event revision in the durable log (0 when no plan exists yet).
        Mirrors events.latest_plan_revision but reads the DB directly so the adapter
        stays oracle-import-free. A re-plan appends a NEW PlanEvent whose `revision`
        increments, so a bump here is hard evidence a follow-up was PICKED UP + re-planned."""
        best = 0
        for e in self._read_events(conversation_id):
            if e.get("kind") != "plan":
                continue
            rev = _payload(e).get("revision", 1)
            try:
                best = max(best, int(rev))
            except (TypeError, ValueError):
                continue
        return best

    def _planning_reentry_since(self, conversation_id: str, after_seq: int) -> bool:
        """True when a RE-PLAN re-entry status (detail == `planning`) appears at seq >
        after_seq. enter_planning stamps this on a re-plan but NOT on the first build turn
        (a plain RUNNING with no detail) — so it is a genuine pickup-of-follow-up signal,
        not the initial plan. `after_seq` is the baseline max seq captured BEFORE the send."""
        for e in self._read_events(conversation_id):
            if e.get("kind") != "status" or int(e.get("seq", -1)) <= after_seq:
                continue
            if (_payload(e).get("detail") or None) == _PLANNING_DETAIL:
                return True
        return False

    def _progress_event_since(self, conversation_id: str, after_seq: int) -> bool:
        """True when the FIRST genuine NON-USER PROGRESS event appears at seq > after_seq —
        the EVENT-SEQUENCED pickup signal (V2). The engine actually processing the follow-up
        produces a new durable event: an action, an observation, a plan, an agent_error, an
        AGENT/assistant message, or a RUNNING / gate / `planning` status. Detectable EVEN WHEN
        the conversation STATUS stays at the prior terminal (FINISHED) — a follow-up appended
        during run finalization causes NO status change, the exact dead-window V1 missed.

        A bare USER-MESSAGE append (kind=message, source=user) is explicitly NOT progress: the
        append itself bumps the event seq but is not processing — that is precisely the
        stale-terminal red herring this guards against, so it is skipped, never a pickup."""
        for e in self._read_events(conversation_id):
            if int(e.get("seq", -1)) <= after_seq:
                continue
            kind = e.get("kind")
            if kind == "message":
                if (e.get("source") or "") == "user":
                    continue  # the user-message APPEND itself is NOT pickup (the red herring)
                return True  # an agent/assistant message = the engine produced output
            if kind in ("action", "observation", "plan", "agent_error"):
                return True
            if kind == "status":
                p = _payload(e)
                st = p.get("status")
                if (
                    st == "RUNNING"
                    or st in GATE_STATES
                    or (p.get("detail") or None) == _PLANNING_DETAIL
                ):
                    return True
        return False

    def _latest_terminal_seq(self, conversation_id: str) -> int:
        """The seq of the MOST RECENT terminal status event in the durable log (-1 when none).
        Used to require a follow-up's OWN NEW terminal (a terminal EVENT with seq > the
        baseline) before the drive returns — so the STALE pre-follow-up terminal can never be
        mistaken for the follow-up's completion (the piece V1 missed)."""
        best = -1
        for e in self._read_events(conversation_id):
            if e.get("kind") != "status":
                continue
            if _payload(e).get("status") in TERMINAL_STATES:
                best = max(best, int(e.get("seq", -1)))
        return best

    def _terminal_is_new(self, conversation_id: str, min_terminal_seq: int | None) -> bool:
        """Whether a terminal status reached NOW is the follow-up's OWN new terminal — its
        durable terminal event has seq > `min_terminal_seq` — rather than the STALE
        pre-follow-up terminal. None ⇒ no baseline guard (any terminal counts, the default)."""
        if min_terminal_seq is None:
            return True
        return self._latest_terminal_seq(conversation_id) > min_terminal_seq

    def _idle_parked_durably(self, conversation_id: str, state: dict[str, Any]) -> bool:
        """Whether an observed IDLE is a durably PARKED terminal rather than the
        pre-kick beat. `seen_active` is invocation-local, so a poll invocation that
        STARTS only after a kill parked the run (live-surfaced at pilot seed 405414:
        a delayed approve_plan WS ack held the driver past the scenario kill) would
        otherwise wait out the whole inactivity window against a stable IDLE and
        never send the scenario's recovery follow-up. The durable log is the
        authority: a terminal status event within the live state's own seq horizon
        proves this IDLE is parked, not pre-kick. Bounding by the state's `last_seq`
        keeps a fixture DB seeded ahead of its scripted states from short-circuiting
        the genuine pre-kick wait."""
        try:
            last_seq = int(state.get("last_seq") or 0)
        except (TypeError, ValueError):
            return False
        if last_seq <= 0:
            return False
        for e in self._read_events(conversation_id):
            if e.get("kind") != "status" or int(e.get("seq", -1)) > last_seq:
                continue
            if _payload(e).get("status") in TERMINAL_STATES:
                return True
        return False

    async def capture_followup_baseline(self, conversation_id: str) -> dict[str, Any]:
        """Snapshot the state needed to detect REAL pickup of the NEXT follow-up (H1),
        captured BEFORE send_followup. `max_seq` is the PRIMARY anchor (V2): pickup and the
        follow-up's own new terminal are both measured as events at seq > max_seq.
          * `max_seq` — the latest durable event seq; the baseline for BOTH the event-sequenced
            pickup detector (a non-user progress event past it) AND the min_terminal_seq drive
            guard (the follow-up's OWN new terminal must have seq > it, not the stale one);
          * `plan_revision` — the latest plan revision so a re-plan bump is detectable;
          * `status` — the build's current (prior-terminal) status, e.g. FINISHED (a
            belt-and-suspenders signal only; a follow-up appended during finalization leaves
            the status UNCHANGED, so the seq — not the status — is what V2 keys on).
        Deliberately does NOT use a bare event-count bump as pickup — the user-message append
        alone bumps the seq, and the append is NOT processing (that red herring is the bug)."""
        status = self._status_of(await self.get_state(conversation_id))
        return {
            "status": status,
            "plan_revision": self._latest_plan_revision(conversation_id),
            "max_seq": self._progress_marker(conversation_id)[1],
        }

    async def wait_for_followup_pickup(
        self,
        conversation_id: str,
        baseline: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> str:
        """SERIALIZE after-terminal follow-ups (H1): after send_followup, BLOCK until the
        engine actually PICKS UP this follow-up — measured against `baseline` (captured
        BEFORE the send) — so the NEXT follow-up is not piled on top of an unprocessed one
        and collapsed into a single plan revision.

        EVENT-SEQUENCED (V2 — V1 was status-only and timed out): pickup is detected from the
        durable EVENT LOG relative to `baseline.max_seq`, NOT from a status change. A follow-up
        appended while the prior run is FINALIZING causes NO status change (status stays
        FINISHED) — V1 watched the status, saw nothing, timed out, and PROCEEDED, letting
        follow-up 2 collapse into follow-up 1. V2 returns as soon as there is REAL pickup
        evidence relative to the baseline:
          * FOLLOWUP_REPLANNED — a re-plan: the latest plan revision bumped above baseline, OR
            a `planning` re-entry status appeared past the baseline seq; OR
          * FOLLOWUP_PICKED_UP — the FIRST non-user PROGRESS event (action / observation / plan
            / agent message / RUNNING-or-gate status) appeared at seq > the baseline seq — the
            engine began processing, EVEN IF the status is still FINISHED. (Belt-and-suspenders:
            a genuine status change off the prior terminal also counts.)

        A new event seq caused MERELY by the user-message append is NOT treated as pickup —
        the append itself is not processing (that is precisely the stale-terminal red herring
        this guards against), so the raw event marker is never a pickup signal.

        BOUNDED by `timeout_s` (default = the policy-driven `_followup_pickup_timeout_s()`):
        on timeout it RETURNS FOLLOWUP_PICKUP_TIMEOUT (never hangs). The CALLER must treat that
        as a SEQUENCING FAILURE — HARD-FAIL the run (do NOT re-send, do NOT drive on the stale
        terminal): re-sending duplicates the user turn (no legal kick-without-append on a
        FINISHED run) and driving on the stale terminal collapses the next follow-up (V1's bug)."""
        if timeout_s is None:
            timeout_s = _followup_pickup_timeout_s()
        base_status = str(baseline.get("status") or "")
        base_rev = int(baseline.get("plan_revision") or 0)
        base_seq = int(baseline.get("max_seq", -1))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._status_of(await self.get_state(conversation_id))
            # (1) a re-plan signal: a revision bump OR a `planning` re-entry past the baseline.
            if self._latest_plan_revision(conversation_id) > base_rev:
                return FOLLOWUP_REPLANNED
            if self._planning_reentry_since(conversation_id, base_seq):
                return FOLLOWUP_REPLANNED
            # (2) EVENT-SEQUENCED pickup: a non-user progress event past the baseline seq means
            #     the engine started processing — detectable even when the status stays FINISHED.
            if self._progress_event_since(conversation_id, base_seq):
                return FOLLOWUP_PICKED_UP
            # (3) belt-and-suspenders: the build left its prior terminal/resting status.
            if base_status and status and status != base_status:
                return FOLLOWUP_PICKED_UP
            await asyncio.sleep(self._poll)
        return FOLLOWUP_PICKUP_TIMEOUT

    async def wait_for_first_file_write(
        self, conversation_id: str, *, timeout_s: float
    ) -> int | None:
        """Poll the DB for the first executed file-mutation action (the §15.4 trigger).
        Returns its seq, or None on timeout / terminal-without-write.

        A trigger is valid only after a write-family action has a SUCCESSFUL observation.
        Preview/verify/shell actions and failed or still-pending file writes are not a
        disconnect/cancel boundary.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            events = self._read_events(conversation_id)
            success_call_ids, success_action_ids = _successful_observation_refs(events)
            for e in events:
                if e.get("kind") != "action":
                    continue
                tc = _payload(e).get("tool_call") or {}
                name = tc.get("tool_name")
                if name not in _FILE_MUTATION_TOOLS:
                    continue
                call_id = tc.get("call_id")
                action_id = e.get("id") or _payload(e).get("id")
                if (call_id is not None and str(call_id) in success_call_ids) or (
                    action_id is not None and str(action_id) in success_action_ids
                ):
                    return int(e.get("seq", -1))
            # bail early if the run already finished without a write
            if events and _last_terminal(events) is not None:
                return None
            await asyncio.sleep(self._poll)
        return None

    def latest_user_message_seq(self, conversation_id: str) -> int:
        """The highest seq of a USER message in the durable log, or -1 when none yet. Used to
        snapshot the user-turn watermark BEFORE the harness sends a turn (declared follow-up or
        injected auto-answer) so the NEW user-message seq it produces can be attributed to the
        harness (revision-anchor metadata)."""
        best = -1
        try:
            for e in self._read_events(conversation_id):
                if e.get("kind") == "message" and e.get("source") == "user":
                    best = max(best, int(e.get("seq", -1)))
        except sqlite3.Error:
            return -1
        return best

    async def wait_for_new_user_message_seq(
        self, conversation_id: str, *, after_seq: int, timeout_s: float
    ) -> int | None:
        """After the harness sends a user turn, poll the durable log until a USER message with
        seq > `after_seq` appears; return ITS seq (the turn the harness just sent), or None on
        timeout / no new user message (e.g. a `confirm` or `pick_alternative` control frame
        appends NO user message — nothing to attribute). Bounded — never hangs."""
        deadline = time.monotonic() + timeout_s
        while True:
            seq = self.latest_user_message_seq(conversation_id)
            if seq > after_seq:
                return seq
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll)

    # -- collection (post-terminal, race-free) --------------------------------

    def collect_events(self, conversation_id: str) -> list[dict[str, Any]]:
        """Read the FULL event log from disco.db AFTER the run reached terminal —
        the durable, race-free source (the trace_conversation.py pattern). Returns
        DB-row dicts {seq,kind,source,id,created_at,payload(JSON string)} which the
        classifier's normalizer flattens identically to events.jsonl."""
        return self._read_events(conversation_id)

    async def collect_state(self, conversation_id: str) -> dict[str, Any]:
        return await self.get_state(conversation_id)

    async def collect_inspect_trace(self, conversation_id: str) -> dict[str, Any] | None:
        """Fetch the redacted per-conversation routing/model span trace.

        Inspect is intentionally optional at the adapter layer so deterministic
        fake-transport tests and non-campaign callers remain usable. The live CLI
        campaign enforces its presence before counting a run.
        """
        aggregation = self._inspect_aggregations.get(conversation_id)
        if aggregation is not None:
            if not aggregation.finalized:
                await self._sample_inspect_trace(conversation_id)
            return aggregation.render()

        # Compatibility for adapter-only callers that did not create the
        # conversation through this client. Governed runs always use the
        # aggregate path above and the repository gate requires its receipt.
        disposition, data = await self._fetch_inspect_snapshot(conversation_id)
        if disposition != "snapshot":
            return None
        if not isinstance(data, dict):
            return None
        if data.get("conversation_id") != conversation_id or not isinstance(
            data.get("events"), list
        ):
            return None
        return data

    async def collect_workspace(
        self, conversation_id: str, file_paths: list[str]
    ) -> dict[str, Any]:
        """Collect the conversation's workspace files AUTHORITATIVELY (Bug 9 fix).

        ROOT CAUSE this replaces: the old path fetched each declared file via the
        single-origin preview proxy (``GET …/preview-app/<path>``). That proxies the
        agent's DEV SERVER, so it returns the file ONLY when the served preview is up,
        registered, and serving that exact route — fragile. A build that genuinely
        SUCCEEDED (index.html written, served, FINISHED) produced an EMPTY manifest
        because the proxy 404'd → the OutputTruthOracle false-FAILed it
        (FALSE_FINISH_NO_OUTPUT). Verified live: ``…/preview-app/index.html``,
        ``…/workspace/index.html`` (image-only allowlist), and ``…/artifacts/index.html``
        (declared-"files" only; an app deliverable is artifact_kind="app") ALL 404 for a
        finished static build — there is no HTTP route that serves arbitrary workspace
        source. The durable, dev-server-independent truth is the host ProjectStore
        SNAPSHOT (``<projects_root>/<cid>/workspace/…``), the SAME source the product's
        own preview-edit / artifact-download routes fall back to once a run is terminal.

        Returns a manifest keyed by workspace-relative path:
            {path: {"present": True, "size": int, "sha256": hex, "content": str}}
        for EVERY file in the snapshot (a faithful workspace reflection, not just the
        declared paths), plus each declared path under its EXACT scenario-declared key
        so the OutputTruthOracle's ``path in files`` presence check matches. A declared
        file that is genuinely absent is OMITTED (never a present:false key — that would
        mask FALSE_FINISH_NO_OUTPUT into ARTIFACT_TRUTH_MISMATCH), so a real
        missing-deliverable build STILL FAILs correctly. ``content`` is the decoded UTF-8
        text (so must_contain substring checks run on the real file); a binary / oversized
        file keeps present+size+sha256 with empty content.

        There is deliberately NO generated-preview fallback. The capability-isolated
        preview serves an app, not an authoritative arbitrary-source filesystem, and the
        authenticated legacy preview-app route is security-forbidden. With no ProjectStore
        snapshot, the honest manifest is empty and the evidence contract fails closed.
        """
        declared = list(file_paths)
        manifest: dict[str, Any] = {}
        snapshot_dir: Path | None = None

        if self._projects_root is not None:
            manifest, snapshot_dir = await self._await_ready_snapshot(conversation_id, declared)

        # The snapshot is AUTHORITATIVE: return it as-is. A declared file absent from the
        # snapshot stays OMITTED — never proxy-substituted (hole #1).
        if snapshot_dir is not None:
            return manifest

        # No authoritative snapshot means no workspace truth. Never substitute
        # generated/served bytes for source evidence or cross the preview auth boundary.
        return manifest

    async def workspace_snapshot_digest(
        self, conversation_id: str, file_paths: list[str]
    ) -> str | None:
        """Return a deterministic digest of the authoritative committed snapshot.

        Used by restart scenarios to prove that a full App/Agent process restart
        reconstructed the same project authority before any new user turn.
        """
        manifest = await self.collect_workspace(conversation_id, file_paths)
        entries: list[tuple[str, str, int]] = []
        for path, item in sorted(manifest.items()):
            if not isinstance(item, dict) or item.get("present") is not True:
                continue
            sha = item.get("sha256")
            size = item.get("size")
            if not isinstance(sha, str) or type(size) is not int:
                return None
            entries.append((str(path), sha, size))
        if not entries:
            return None
        raw = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def collect_browser_evidence(
        self,
        conversation_id: str,
        events: list[dict[str, Any]],
        workspace_manifest: dict[str, Any],
        *,
        require_verified_host_screenshot: bool = False,
        horizon_seq: int | None = None,
    ) -> dict[str, bytes]:
        """Capture referenced screenshots from the authoritative ProjectStore snapshot.

        Only paths carried by successful ``browser`` / structured verifier observations
        are eligible.  Each path is jailed beneath this conversation's workspace, read in
        full, and cross-checked against the already-collected workspace manifest size and
        SHA256.  Missing, changing, escaping, or over-limit bytes raise
        :class:`BrowserEvidenceCollectionError`; silently dropping visual evidence would
        make a frozen replay look stronger than the live run.

        ``horizon_seq`` binds collection to an accepted event horizon: only
        observations at or before that seq are eligible.  A workspace frozen at a
        ``WorkspaceVersionEvent`` cannot contain a screenshot that was referenced
        *after* that event, so certifying one would assert evidence the frozen bytes
        do not carry.  Events without a usable ``seq`` are dropped rather than
        assumed in-horizon: an unplaceable observation cannot be proven to precede
        the freeze.
        """
        if horizon_seq is not None:
            eligible: list[dict[str, Any]] = []
            for event in events:
                try:
                    seq = int(event.get("seq", -1))
                except (TypeError, ValueError):
                    continue
                if 0 <= seq <= horizon_seq:
                    eligible.append(event)
            events = eligible
        references = _referenced_screenshot_paths(
            events,
            require_verified_host_screenshot=require_verified_host_screenshot,
        )
        if not references:
            return {}
        if len(references) > _BROWSER_EVIDENCE_MAX_FILES:
            raise BrowserEvidenceCollectionError(
                "referenced browser screenshot count exceeds the durable evidence bound",
                {
                    "conversation_id": conversation_id,
                    "referenced_count": len(references),
                    "max_files": _BROWSER_EVIDENCE_MAX_FILES,
                },
            )

        ws = self._collected_workspace_dirs.get(conversation_id) or self._snapshot_workspace_dir(
            conversation_id
        )
        if ws is None:
            raise BrowserEvidenceCollectionError(
                "referenced browser screenshot has no authoritative workspace snapshot",
                {"conversation_id": conversation_id, "paths": references},
            )

        captured: dict[str, bytes] = {}
        total = 0
        for rel in references:
            source = _jailed_browser_evidence_path(ws, rel, conversation_id=conversation_id)
            entry = workspace_manifest.get(rel)
            if not isinstance(entry, dict) or entry.get("present") is not True:
                raise BrowserEvidenceCollectionError(
                    "referenced browser screenshot is absent from the workspace manifest",
                    {"conversation_id": conversation_id, "path": rel},
                )
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise BrowserEvidenceCollectionError(
                    "referenced browser screenshot could not be read",
                    {
                        "conversation_id": conversation_id,
                        "path": rel,
                        "error": type(exc).__name__,
                    },
                ) from exc
            if len(data) > _BROWSER_EVIDENCE_MAX_FILE_BYTES:
                raise BrowserEvidenceCollectionError(
                    "referenced browser screenshot exceeds the per-file evidence bound",
                    {
                        "conversation_id": conversation_id,
                        "path": rel,
                        "size": len(data),
                        "max_file_bytes": _BROWSER_EVIDENCE_MAX_FILE_BYTES,
                    },
                )
            total += len(data)
            if total > _BROWSER_EVIDENCE_MAX_TOTAL_BYTES:
                raise BrowserEvidenceCollectionError(
                    "referenced browser screenshots exceed the total evidence bound",
                    {
                        "conversation_id": conversation_id,
                        "path": rel,
                        "total_bytes": total,
                        "max_total_bytes": _BROWSER_EVIDENCE_MAX_TOTAL_BYTES,
                    },
                )

            actual_sha = hashlib.sha256(data).hexdigest()
            expected_size = entry.get("size")
            expected_sha = entry.get("sha256")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size != len(data)
                or not isinstance(expected_sha, str)
                or not _RAW_SHA256_RE.fullmatch(expected_sha)
                or expected_sha.lower() != actual_sha
            ):
                raise BrowserEvidenceCollectionError(
                    "referenced browser screenshot does not match the workspace manifest",
                    {
                        "conversation_id": conversation_id,
                        "path": rel,
                        "expected_size": expected_size,
                        "actual_size": len(data),
                        "expected_sha256": expected_sha,
                        "actual_sha256": actual_sha,
                    },
                )
            captured[rel] = data
        return captured

    async def _await_ready_snapshot(
        self, conversation_id: str, declared: list[str]
    ) -> tuple[dict[str, Any], Path | None]:
        """Poll the host ProjectStore snapshot until it has SETTLED on the build's
        AGENT-FINAL state, then return ``(manifest, snapshot_dir)``.

        WHY (stale-read race): ``_maybe_snapshot`` mirrors the sandbox workspace to the
        ProjectStore AFTER the build appends a terminal event. A multi-revision build
        reaches a terminal per revision, so revision-1's files are already on disk
        (presence-complete) BEFORE revision-2's bytes flush — the old presence-only gate
        settled on STALE revision-1 content → a false ARTIFACT_TRUTH_MISMATCH. A timing
        window can't deterministically close that gap, so we gate on a DETERMINISTIC
        readiness signal derived from the build's own durable event log (NOT the scenario's
        expected assertion — gating on the assertion target would mask real failures).

        Per declared file the agent's FINAL state (from ``_agent_declared_expected``) is one of:
          * sha — last op is ``file_write`` (full bytes in the payload): on-disk sha256 MATCH.
          * rendered — a partial edit was the last mutation but a FULL ``file_read`` readback
            after it carries the true post-edit bytes: the on-disk file RENDERED by file_read's
            own numberer must equal the readback (closes the partial-edit-last stale gap).
          * absent — the agent ``rm``'d it via shell: accept only when NOT on disk.
          * present_unproven — a partial edit with no provable post-state: extended stability.
          * unknown — no event-log signal at all: extended stability (present or absent).

        Timeout = FAIL-FAST only for a CONTENT-PRECISE signal (sha / rendered / absent) still
        unsatisfied at the ``snapshot_wait_s`` deadline → raise ``SnapshotNotReadyError`` (→
        INVALID_RUN WORKSPACE_SNAPSHOT_NOT_READY). present_unproven / unknown files NEVER
        fail-fast (no content proof exists, and a legit edit-without-readback build must not
        become INVALID_RUN) — they settle on EXTENDED content-stability (+a logged warning) and
        are accepted best-effort at the deadline, preserving the genuinely-missing → OMITTED →
        FALSE_FINISH path. Already-consistent snapshots are accepted PROMPTLY, no needless wait."""
        try:
            events = self.collect_events(conversation_id)
        except sqlite3.Error:
            # No durable event log available (e.g. deterministic tests that don't seed the
            # DB) → no event signals; every declared file settles on content-stability.
            events = []
        expected = _agent_declared_expected(events, declared)

        deadline = time.monotonic() + self._snapshot_wait_s
        prev_ident: dict[str, Any] = {}
        stable_n: dict[str, int] = {}
        manifest: dict[str, Any] = {}
        snapshot_dir: Path | None = None
        terminal_seq: int | None = None
        latest_effect_seq: int | None = None
        commit_seq: int | None = None
        commit_version_seq: int | None = None
        commit_digest: str | None = None
        observed_digest: str | None = None
        observed_file_count: int | None = None
        observed_total_bytes: int | None = None
        final_seal_error: str | None = None
        event_evidence_valid = False
        # The LIVE status is what separates "this run genuinely never finished"
        # from "it finished but its terminal event is missing". Only the first is
        # exempt from the snapshot demand; the second is evidence loss and must
        # still fail closed. Unreadable live state counts as finished, so an
        # unknown falls to the strict path.
        try:
            _finished_live = self._status_of(await self.get_state(conversation_id)) in {
                "FINISHED",
                "VERIFIED",
            }
        except Exception:  # noqa: BLE001 — unknown live status → strict path
            _finished_live = True
        while True:
            try:
                current_events = normalize_events(self.collect_events(conversation_id))
                event_evidence_valid = True
            except (sqlite3.Error, NormalizationError):
                current_events = []

            def _exact_seq(event: dict[str, Any]) -> int | None:
                raw = event.get("seq")
                return raw if type(raw) is int and raw > 0 else None

            def _sort_seq(event: dict[str, Any]) -> int:
                seq = _exact_seq(event)
                return seq if seq is not None else -1

            def _valid_status(event: dict[str, Any]) -> bool:
                return (
                    event.get("source") == "system"
                    and _exact_seq(event) is not None
                    and isinstance(event.get("status"), str)
                )

            def _valid_workspace_version(event: dict[str, Any]) -> bool:
                version_seq = event.get("version_seq")
                digest = event.get("tree_digest")
                return (
                    event.get("source") == "system"
                    and _exact_seq(event) is not None
                    and type(version_seq) is int
                    and version_seq > 0
                    and isinstance(digest, str)
                    and _RAW_SHA256_RE.fullmatch(digest) is not None
                    and isinstance(event.get("trigger"), str)
                )

            commit_required = self._require_workspace_commit
            seal_file_count: int | None = None
            seal_total_bytes: int | None = None
            if commit_required:
                seal_evidence = _strict_final_workspace_seal(current_events, conversation_id)
                event_evidence_valid = seal_evidence.valid
                terminal_seq = seal_evidence.terminal_seq
                latest_effect_seq = seal_evidence.latest_effect_seq
                commit_seq = seal_evidence.event_seq
                commit_version_seq = seal_evidence.version_seq
                commit_digest = seal_evidence.tree_digest
                seal_file_count = seal_evidence.file_count
                seal_total_bytes = seal_evidence.total_bytes
                final_seal_error = seal_evidence.error
            else:
                for event in current_events:
                    kind = event.get("kind")
                    if kind == "status" and not _valid_status(event):
                        event_evidence_valid = False
                    elif kind == "workspace_version" and not _valid_workspace_version(event):
                        event_evidence_valid = False
                    elif (
                        kind in {"action", "observation", "agent_error"}
                        and _exact_seq(event) is None
                    ):
                        event_evidence_valid = False

                statuses = [
                    event
                    for event in current_events
                    if event.get("kind") == "status" and _valid_status(event)
                ]
                latest_status = max(statuses, key=_sort_seq, default=None)
                terminal_seq = (
                    _exact_seq(latest_status)
                    if event_evidence_valid
                    and latest_status is not None
                    and (
                        str(latest_status.get("status")) in TERMINAL_STATES
                        or str(latest_status.get("status")) == PAUSED_STATE
                    )
                    else None
                )
                legacy_latest_effect_seq = max(
                    (
                        seq
                        for event in current_events
                        if event.get("kind") in {"action", "observation", "agent_error"}
                        and (seq := _exact_seq(event)) is not None
                    ),
                    default=-1,
                )
                latest_effect_seq = (
                    legacy_latest_effect_seq if legacy_latest_effect_seq > 0 else None
                )
                commits = [
                    event
                    for event in current_events
                    if event.get("kind") == "workspace_version"
                    and _valid_workspace_version(event)
                    and event.get("trigger") == "finish"
                    and event_evidence_valid
                    and terminal_seq is not None
                    and _sort_seq(event) > terminal_seq
                    and _sort_seq(event) > legacy_latest_effect_seq
                ]
                commit = max(commits, key=_sort_seq, default=None)
                commit_seq = _exact_seq(commit) if commit is not None else None
                commit_version_seq = (
                    int(commit["version_seq"])
                    if commit is not None and type(commit.get("version_seq")) is int
                    else None
                )
                commit_digest = str(commit.get("tree_digest") or "") if commit is not None else None
                final_seal_error = None

            observed_digest = None
            observed_file_count = None
            observed_total_bytes = None
            # Strict live evidence never relaxes merely because its ordering proof
            # is missing or corrupt.  The schema-v1 final seal, exact event fence,
            # and freshly verified immutable tree are all mandatory.
            commit_ready = not commit_required
            if commit_required and event_evidence_valid and commit_version_seq is not None:
                verified_version = self._verified_workspace_version(
                    conversation_id, commit_version_seq
                )
                if verified_version is not None:
                    snapshot_dir = verified_version.workspace
                    observed_digest = verified_version.tree_digest
                    observed_file_count = verified_version.file_count
                    observed_total_bytes = verified_version.total_bytes
                    commit_ready = (
                        observed_digest == commit_digest
                        and observed_file_count == seal_file_count
                        and observed_total_bytes == seal_total_bytes
                    )
                    if not commit_ready:
                        final_seal_error = (
                            "final seal tree facts do not match the immutable version"
                        )
                else:
                    snapshot_dir = None
                    final_seal_error = "immutable workspace version could not be freshly verified"
            elif not commit_required:
                snapshot_dir = self._snapshot_workspace_dir(conversation_id)
                if commit_seq is not None and snapshot_dir is not None:
                    try:
                        observed_digest = project_tree_digest(snapshot_dir)
                    except OSError:
                        observed_digest = None
            else:
                snapshot_dir = None
            manifest = (
                self._read_snapshot_manifest(conversation_id, declared, snapshot_dir)
                if snapshot_dir is not None and commit_ready
                else {}
            )
            if commit_required and commit_ready and snapshot_dir is not None:
                post_read_version = self._verified_workspace_version(
                    conversation_id, commit_version_seq
                )
                if post_read_version is None:
                    commit_ready = False
                    manifest = {}
                    final_seal_error = "immutable workspace version failed post-read verification"
                else:
                    if (
                        post_read_version.workspace != snapshot_dir
                        or post_read_version.tree_digest != commit_digest
                        or post_read_version.file_count != seal_file_count
                        or post_read_version.total_bytes != seal_total_bytes
                    ):
                        observed_digest = post_read_version.tree_digest
                        observed_file_count = post_read_version.file_count
                        observed_total_bytes = post_read_version.total_bytes
                        commit_ready = False
                        manifest = {}
                        final_seal_error = "immutable workspace version changed during collection"
            # Content-stability bookkeeping: count consecutive identical (present+size+sha)
            # reads per declared file. A change resets the counter to this fresh observation.
            for p in declared:
                ident = _manifest_ident(manifest, p)
                if p in prev_ident and prev_ident[p] == ident:
                    stable_n[p] = stable_n.get(p, 1) + 1
                else:
                    stable_n[p] = 1
                prev_ident[p] = ident
            ready, blocking, unproven_ready = _evaluate_snapshot_readiness(
                declared, expected, manifest, stable_n
            )
            # PROVEN-ready entries (raw_sha / rendered_readback / absent) are safe to accept
            # the instant they're ready. But an entry accepted PURELY on content-stability
            # (unproven_ready) can still be STALE: a multi-revision build's FINAL edit (e.g.
            # the late "Get Started" CTA change in revise_twice_complex) can flush AFTER the
            # brief stability window, so short-circuiting here captured the pre-edit bytes and
            # mislabeled a DELIVERED build WORKSPACE_SNAPSHOT_UNVERIFIED (the authoritative
            # ProjectStore had the change). So when readiness rests on unproven stability, do
            # NOT break early — keep polling to the full snapshot_wait_s deadline so a late
            # flush re-resets stability and is captured. Proven captures stay fast (no wait).
            if (
                ready
                and commit_ready
                and (commit_required or not unproven_ready or time.monotonic() >= deadline)
            ):
                if not commit_required:
                    for p in unproven_ready:
                        _LOG.warning(
                            "snapshot %s: declared file %r accepted on EXTENDED "
                            "content-stability (%s) — no sha/readback proof of the agent's "
                            "final bytes (expected=%s)",
                            conversation_id,
                            p,
                            expected.get(p, ("unknown",))[0],
                            list(expected.get(p, ("unknown",))),
                        )
                break
            if time.monotonic() >= deadline:
                # A run that never reached a SYSTEM FINISHED terminal HAS no
                # agent-final state, so waiting for the snapshot to converge on one
                # is waiting for something that cannot exist. Raising here made the
                # snapshot the reported cause and buried the real one: a STUCK run
                # released at the clarify cap came back INVALID_RUN /
                # WORKSPACE_SNAPSHOT_NOT_READY three separate times this campaign
                # (seeds 440023, 450000, 440019) while the truth was upstream.
                #
                # Returning what was observed is STRICTER, not laxer: the ordinary
                # oracles then judge the run on its real terminal status and it
                # FAILS as the unfinished build it is, instead of an INVALID_RUN
                # that does not count. A run that DID finish is untouched — its
                # snapshot demand still fails fast.
                if final_seal_error == _NO_SYSTEM_FINISHED_TERMINAL and not _finished_live:
                    break
                if blocking or not commit_ready:
                    raise SnapshotNotReadyError(
                        "workspace snapshot never reached the agent's final state within "
                        f"{self._snapshot_wait_s:g}s",
                        {
                            "conversation_id": conversation_id,
                            "snapshot_wait_s": self._snapshot_wait_s,
                            "snapshot_dir": str(snapshot_dir) if snapshot_dir else None,
                            "terminal_seq": terminal_seq,
                            "event_evidence_valid": event_evidence_valid,
                            "workspace_version_seq": commit_seq,
                            "workspace_version_version_seq": commit_version_seq,
                            "workspace_version_digest": commit_digest,
                            "final_seal_file_count": seal_file_count,
                            "final_seal_total_bytes": seal_total_bytes,
                            "latest_effect_seq": latest_effect_seq,
                            "final_seal_error": final_seal_error,
                            "observed_file_count": observed_file_count,
                            "observed_total_bytes": observed_total_bytes,
                            "observed_tree_digest": observed_digest,
                            "unsatisfied": [
                                {
                                    "path": p,
                                    "expected": list(expected.get(p, ("unknown",))),
                                    "on_disk_sha256": _manifest_sha(manifest, p),
                                    "present": _manifest_present(manifest, p),
                                }
                                for p in blocking
                            ],
                        },
                    )
                # No DEFINITE event signal still pending → no evidence the snapshot is behind
                # the agent. Accept the current read best-effort (genuinely-missing files stay
                # OMITTED → FALSE_FINISH preserved); never FAIL-FAST on an unknown file.
                break
            await asyncio.sleep(_SNAPSHOT_POLL_S)
        # Stamp each PRESENT declared file's acceptance PROOF LEVEL onto its manifest entry.
        # Strict live collection has already verified the complete schema-v1 final seal, opened
        # its exact immutable ProjectStore version, matched digest/count/bytes, read the manifest,
        # and re-verified the same version afterward.  That is shape-agnostic authoritative byte
        # proof for every file in the tree; do not discard it merely because the model's last
        # action was a partial edit or a semantic generator unknown to the legacy action parser.
        # Non-strict collection retains its per-action/readback/stability classification.
        # Absent declared files carry no entry (existence is judged separately).
        final_tree_proven = (
            commit_required
            and commit_ready
            and snapshot_dir is not None
            and final_seal_error is None
        )
        for p in declared:
            entry = _manifest_lookup(manifest, p)
            if entry is not None:
                entry["proof"] = (
                    "final_tree_seal" if final_tree_proven else _proof_level(expected.get(p))
                )
                entry["content_stable"] = stable_n.get(p, 0) >= _SNAPSHOT_UNPROVEN_STABLE_POLLS
        if snapshot_dir is not None:
            self._collected_workspace_dirs[conversation_id] = snapshot_dir
        return manifest, snapshot_dir

    def _read_snapshot_manifest(
        self, conversation_id: str, declared: list[str], ws: Path | None = None
    ) -> dict[str, Any]:
        """Walk the host ProjectStore snapshot workspace for `conversation_id` and
        build the per-file manifest. Symlink-jailed (resolve + is_relative_to) so a
        planted ``leak.html -> /etc/passwd`` can never escape the workspace. Empty dict
        when no snapshot exists yet (caller retries / falls back to the preview proxy)."""
        if ws is None:
            ws = self._snapshot_workspace_dir(conversation_id)
        if ws is None:
            return {}
        manifest: dict[str, Any] = {}
        count = 0
        for root, _dirs, names in os.walk(ws):  # followlinks=False → no dir-symlink escape
            for name in sorted(names):
                fp = Path(root) / name
                try:
                    resolved = fp.resolve()
                    if not resolved.is_relative_to(ws) or not resolved.is_file():
                        continue
                    rel = resolved.relative_to(ws).as_posix()
                    data = resolved.read_bytes()
                except OSError:
                    continue
                manifest[rel] = _file_entry(data)
                count += 1
                if count >= _WS_MANIFEST_MAX_FILES:
                    break
            if count >= _WS_MANIFEST_MAX_FILES:
                break
        # Guarantee each DECLARED path is keyed by its EXACT scenario string (the oracle
        # checks `spec["path"] in files`); the walk keys by the leading-slash-free relpath.
        for path in declared:
            rel = path.lstrip("/")
            if path not in manifest and rel in manifest:
                manifest[path] = manifest[rel]
        return manifest

    async def freeze_progressing_workspace(
        self,
        conversation_id: str,
        declared: list[str],
        *,
        deadline_s: float = 90.0,
    ) -> dict[str, Any]:
        """Freeze a still-progressing run's workspace BEFORE the destructive kill.

        The hard-cap stop used to kill first and read the durable store afterwards.
        Product kill correctly destroys the executor/sandbox WITHOUT snapshotting, so
        the store still held only the original import snapshot: every edit, REPORT.md
        and every `.pmx/screenshots/*.png` was absent. Counted context seeds
        460004/460005 surfaced that as MISSING_REQUIRED_EVIDENCE naming
        `0001-navigate.png`, which was merely the FIRST referenced missing path.

        No product change is needed. A Build run that ends PAUSED is snapshotted by
        `_run_with_persistence`'s end-gate under the product's own `workspace_lock`,
        emitting a durable WorkspaceVersionEvent and an immutable ProjectStore version.
        So: pause -> await a NEW durable PAUSED -> await the NEW version event that
        FOLLOWS it -> verify and read that exact immutable version. The mutable store
        head is never read and the live sandbox is never read.

        Returns a disclosure dict; `status="frozen"` only when every step held.
        Any bounded failure returns `FREEZE_TIMEOUT` with an EMPTY manifest — the
        caller must then kill unchanged and must never claim evidence was preserved.
        """

        result: dict[str, Any] = {
            "status": "FREEZE_TIMEOUT",
            "manifest": {},
            "horizon_seq": None,
            "version_seq": None,
            "paused_seq": None,
            "reason": None,
        }
        # 1. pre-pause watermarks and run authority
        #
        # NORMALIZE. `collect_events` returns raw SQLite ROWS --
        # {seq, kind, source, id, created_at, payload} -- where `payload` is an
        # unparsed JSON STRING. Only seq/kind/source are real top-level columns, so
        # reading `status`, `trigger`, `version_seq`, `run_intent_id`, or
        # `agent_view_id` straight off a row yields None every time and this freeze
        # would report FREEZE_TIMEOUT on every real run. `normalize_events` is the
        # canonical flattener the rest of this adapter already uses.
        try:
            before = normalize_events(self.collect_events(conversation_id))
        except Exception as exc:  # noqa: BLE001 — disclosed, never fabricated
            result["reason"] = f"pre-pause event read failed: {type(exc).__name__}"
            return result
        pre_seq = max((int(e.get("seq", -1)) for e in before), default=-1)
        pre_intent = _latest_run_intent_id(before)
        pre_view = _latest_agent_view_id(before)

        # 2. the existing cooperative WS pause
        try:
            await self.pause(conversation_id)
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"pause control failed: {type(exc).__name__}"
            return result

        # 3/4. bounded wait for a NEW durable PAUSED, then the version event AFTER it
        deadline = time.monotonic() + deadline_s
        paused_seq: int | None = None
        version_event: dict[str, Any] | None = None
        events: list[dict[str, Any]] = before
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                events = normalize_events(self.collect_events(conversation_id))
            except Exception:  # noqa: BLE001 — retry to the deadline
                continue
            if paused_seq is None:
                paused_seq = next(
                    (
                        int(e.get("seq", -1))
                        for e in events
                        if int(e.get("seq", -1)) > pre_seq and _event_status_value(e) == "PAUSED"
                    ),
                    None,
                )
            if paused_seq is not None:
                version_event = next(
                    (
                        e
                        for e in events
                        if e.get("kind") == "workspace_version"
                        and int(e.get("seq", -1)) > paused_seq
                        and str(e.get("trigger") or "") == "PAUSED"
                    ),
                    None,
                )
                if version_event is not None:
                    break
        if paused_seq is None:
            result["reason"] = "no durable PAUSED status within the freeze deadline"
            return result
        result["paused_seq"] = paused_seq
        if version_event is None:
            result["reason"] = "no PAUSED WorkspaceVersionEvent within the freeze deadline"
            return result

        horizon_seq = int(version_event.get("seq", -1))
        # 5. a numerically NEW version_seq is deliberately NOT required: an unchanged
        # tree may deduplicate onto an existing immutable version while still emitting
        # a new event. The EVENT is the freeze proof, not the version number.
        version_seq = version_event.get("version_seq")

        # 8. no user turn, resume, newer run intent, or superseded agent view may
        # cross the horizon. One choke point; see _freeze_horizon_violation.
        violation = _freeze_horizon_violation(
            events,
            pre_seq=pre_seq,
            paused_seq=paused_seq,
            horizon_seq=horizon_seq,
            pre_intent=pre_intent,
            pre_view=pre_view,
        )
        if violation is not None:
            result["reason"] = violation
            return result

        # 6. verify and read THAT immutable version — never the mutable head.
        verified = self._verified_workspace_version(conversation_id, version_seq)
        if verified is None:
            result["reason"] = "the PAUSED immutable version could not be freshly verified"
            return result
        # 7. Register THIS immutable version as the conversation's authoritative
        # workspace directory.
        #
        # `collect_browser_evidence` resolves its source as
        # `_collected_workspace_dirs.get(cid) or _snapshot_workspace_dir(cid)`.  The
        # freeze path deliberately bypasses `collect_workspace`, so without this
        # registration that dict is empty and the `or` silently falls back to the
        # MUTABLE ProjectStore head -- which, after the kill, holds only the pre-run
        # import snapshot.  Registering the verified immutable path makes the
        # mutable-head fallback unreachable here rather than merely unlikely.
        self._collected_workspace_dirs[conversation_id] = verified.workspace
        result.update(
            {
                "status": "frozen",
                "manifest": self._read_snapshot_manifest(
                    conversation_id, declared, verified.workspace
                ),
                "horizon_seq": horizon_seq,
                "version_seq": version_seq,
                "workspace_dir": str(verified.workspace),
                "tree_digest": verified.tree_digest,
                "file_count": verified.file_count,
                "total_bytes": verified.total_bytes,
            }
        )
        return result

    def _snapshot_workspace_dir(self, conversation_id: str) -> Path | None:
        """The host ProjectStore ``workspace/`` directory for this conversation, or None
        when no projects_root is configured/valid or the snapshot isn't on disk yet.
        Uses the product's OWN ProjectStore so the runner resolves the SAME root the
        agent-server does (DISCO_DATA_DIR / XDG_DATA_HOME / ~/.local/share/disco/projects)."""
        if self._projects_root is None:
            return None
        try:
            from disco.tools.projects.store import ProjectStore, StorageStatus

            store = ProjectStore(self._projects_root)
            if store.status() != StorageStatus.OK:
                return None
            raw_ws = store.path_for(conversation_id)
            root = store.root.resolve() if store.root is not None else None
            if raw_ws.is_symlink() or root is None:
                return None
            ws = raw_ws.resolve()
            if not ws.is_relative_to(root):
                return None
        except Exception:  # noqa: BLE001 — any resolution failure ⇒ no snapshot available
            return None
        return ws if ws.is_dir() else None

    def _verified_workspace_version(
        self, conversation_id: str, version_seq: int | None
    ) -> _VerifiedWorkspaceVersion | None:
        """Freshly verify and resolve one immutable ProjectStore version.

        ``verify_version`` rescans every regular file without following symlinks,
        then checks those facts against both the version index and sidecar.  The
        strict soak consumer compares the returned, scan-proven facts to the seal
        and reads only this immutable path — never the mutable live mirror.
        """

        if self._projects_root is None or version_seq is None:
            return None
        try:
            from disco.tools.projects.store import ProjectStore, StorageStatus

            store = ProjectStore(self._projects_root)
            if store.status() != StorageStatus.OK:
                return None
            cache_key = hashlib.sha256(f"{conversation_id}:{version_seq}".encode()).hexdigest()
            ws = Path(self._verified_workspace_cache.name) / cache_key
            with store.open_verified_version(conversation_id, version_seq) as verified:
                record = verified.record
                if not ws.exists():
                    ws.mkdir(parents=False)
                    for entry, data in verified.iter_bytes():
                        target = ws / entry.path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                cached_paths = {
                    path.relative_to(ws).as_posix()
                    for path in ws.rglob("*")
                    if path.is_file() and not path.is_symlink()
                }
                expected_paths = {entry.path for entry in verified.files}
                if cached_paths != expected_paths:
                    return None
                for entry in verified.files:
                    data = (ws / entry.path).read_bytes()
                    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                        return None
        except Exception:  # noqa: BLE001 — missing/corrupt version evidence is not ready
            return None
        if not ws.is_dir():
            return None
        return _VerifiedWorkspaceVersion(
            workspace=ws,
            file_count=record.file_count,
            total_bytes=record.total_bytes,
            tree_digest=record.tree_digest,
        )

    async def collect_preview(self, conversation_id: str) -> dict[str, Any]:
        """Capture post-finish preview truth through the public isolated boundary.

        The application session may mint a one-time path capability, but generated
        content is fetched only after body-only redemption in a clean cookie jar.
        ``fetch_isolated_preview`` validates the server-minted p3s origin and never
        sends the app session to it.  Its final HTTP status/body are authoritative:
        a mint, redemption, or product preview failure is retained as a failure and
        is never hidden by serving the host snapshot inside the harness.
        """
        avail_status, avail = await self._t.get_json(f"/conversations/{conversation_id}/preview")
        status, text, _hdrs = await self._t.fetch_isolated_preview(conversation_id)
        if status == 409 and text.strip() == "preview generation changed":
            # The canonical authority rotates BY DESIGN when the first post-finish
            # request replays the sealed runtime from immutable bytes; the real
            # client re-bootstraps on this exact 409 (counted seed 440041, where
            # the availability projection was simultaneously and truthfully 200).
            # Mirror that protocol ONCE with a fresh mint+redeem+fetch; a second
            # rotation in a row is retained as the truthful failure and any other
            # 409 is never retried.
            status, text, _hdrs = await self._t.fetch_isolated_preview(conversation_id)
        return {
            "health": {"status": status},
            "content": text,
            "available": status < 400,
            "runtime_available": (bool(avail.get("available")) if avail_status < 400 else False),
            "runtime_availability_status": avail_status,
            "source": "isolated_path_capability",
        }

    # -- internal: race-free DB read ------------------------------------------

    def _read_events(self, conversation_id: str) -> list[dict[str, Any]]:
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT seq, kind, source, id, created_at, payload "
                "FROM events WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "source": r["source"],
                "id": r["id"],
                "created_at": r["created_at"],
                "payload": r["payload"],  # JSON string — normalizer parses it
            }
            for r in rows
        ]


# ---- helpers ----------------------------------------------------------------


def _file_entry(data: bytes) -> dict[str, Any]:
    """A single workspace-manifest entry: existence + identity (size, sha256) plus the
    decoded text content. The OutputTruthOracle reads ``content`` for must_contain
    substring checks; a binary / oversized file keeps present+size+sha256 with empty
    content (binary deliverables carry no text assertions)."""
    entry: dict[str, Any] = {
        "present": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) <= _WS_MANIFEST_MAX_BYTES:
        try:
            entry["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            entry["content"] = ""
            entry["binary"] = True
    else:
        entry["content"] = ""
        entry["truncated"] = True
    return entry


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except (ValueError, TypeError):
            return {}
    return p if isinstance(p, dict) else {}


def _referenced_screenshot_paths(
    events: list[dict[str, Any]], *, require_verified_host_screenshot: bool = False
) -> list[str]:
    """Return sorted unique screenshot paths explicitly claimed by successful probes.

    ``verify_appkit_app`` can carry interaction screenshots in nested dictionaries,
    so keys named ``screenshot_path`` or ending in ``_screenshot_path`` are followed
    recursively. Other strings (including human-readable ``content`` and base64
    fields) are deliberately ignored.  A host ``verifier_verdict`` is a separate,
    host-owned proof path: only an exact verified/pass verdict is eligible, and such
    a verdict MUST name one admissible screenshot or collection fails closed.
    """
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "screenshot_path" or key.endswith("_screenshot_path"):
                    if child in (None, ""):
                        continue
                    if not isinstance(child, str):
                        raise BrowserEvidenceCollectionError(
                            "successful browser observation has a malformed screenshot path",
                            {"field": key, "value_type": type(child).__name__},
                        )
                    found.add(validate_browser_evidence_relpath(child))
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    try:
        normalized = normalize_events(events)
    except NormalizationError as exc:
        raise BrowserEvidenceCollectionError(
            "browser screenshot references could not be read from the durable event log",
            {"error": str(exc)},
        ) from exc
    # A version restore starts a new workspace-evidence generation. The immutable
    # event log deliberately retains older browser receipts, but their `.pmx`
    # screenshot paths name bytes in the superseded workspace tree. Resolve only
    # references from the current generation against the current ProjectStore
    # snapshot. Using the restore *mutation* as the fence (rather than the later
    # success marker) also prevents pre-restore proof from surviving a partial or
    # failed restore.
    restore_fence = max(
        (
            event["seq"]
            for event in normalized
            if (
                (
                    event.get("kind") == "workspace_mutation"
                    and event.get("operation") == "version.restore"
                )
                or event.get("kind") == "workspace_restored"
            )
            and type(event.get("seq")) is int
        ),
        default=0,
    )
    if restore_fence:
        normalized = [
            event
            for event in normalized
            if type(event.get("seq")) is int and event["seq"] > restore_fence
        ]
    for event in normalized:
        if event.get("kind") == "verifier_verdict":
            if event.get("verified") is not True or event.get("verdict") != "pass":
                continue
            screenshot_path = event.get("screenshot_path")
            if not isinstance(screenshot_path, str) or not screenshot_path:
                if not require_verified_host_screenshot:
                    continue
                raise BrowserEvidenceCollectionError(
                    "passing host verifier verdict has no admissible screenshot path",
                    {
                        "field": "screenshot_path",
                        "value_type": type(screenshot_path).__name__,
                    },
                )
            try:
                found.add(validate_browser_evidence_relpath(screenshot_path))
            except BrowserEvidenceCollectionError:
                if require_verified_host_screenshot:
                    raise
            continue
        if event.get("kind") != "observation":
            continue
        result = event.get("tool_result")
        if not isinstance(result, dict):
            continue
        if result.get("tool_name") not in _BROWSER_EVIDENCE_TOOLS:
            continue
        if result.get("success") is not True:
            continue
        structured = result.get("structured")
        if isinstance(structured, dict):
            visit(structured)
    return sorted(found)


def validate_browser_evidence_relpath(path: str) -> str:
    """Validate the product-owned screenshot namespace and exact POSIX spelling.

    Browser evidence retention must not become an arbitrary workspace-file copier:
    the product daemon owns only direct PNG children of ``.pmx/screenshots``.
    """
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) != 3
        or parts[:2] != [".pmx", "screenshots"]
        or not parts[2].endswith(".png")
    ):
        raise BrowserEvidenceCollectionError(
            "successful browser observation references a path outside the product "
            "screenshot namespace",
            {"path": path},
        )
    return path


def _jailed_browser_evidence_path(workspace: Path, rel: str, *, conversation_id: str) -> Path:
    """Resolve one validated screenshot path without permitting symlink escape."""
    validate_browser_evidence_relpath(rel)
    try:
        resolved = (workspace / rel).resolve(strict=True)
    except OSError as exc:
        raise BrowserEvidenceCollectionError(
            "referenced browser screenshot is missing from the workspace snapshot",
            {
                "conversation_id": conversation_id,
                "path": rel,
                "error": type(exc).__name__,
            },
        ) from exc
    if not resolved.is_relative_to(workspace) or not resolved.is_file():
        raise BrowserEvidenceCollectionError(
            "referenced browser screenshot escapes the workspace snapshot",
            {"conversation_id": conversation_id, "path": rel},
        )
    return resolved


def _successful_observation_refs(events: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Successful observation identifiers keyed both ways the event stream exposes them."""
    call_ids: set[str] = set()
    action_ids: set[str] = set()
    for e in events:
        if e.get("kind") != "observation":
            continue
        p = _payload(e)
        tr = p.get("tool_result") or {}
        if not isinstance(tr, dict) or not bool(tr.get("success", True)):
            continue
        call_id = tr.get("call_id")
        if call_id is not None:
            call_ids.add(str(call_id))
        action_id = p.get("action_id") or e.get("action_id")
        if action_id is not None:
            action_ids.add(str(action_id))
    return call_ids, action_ids


def _last_terminal(rows: list[dict[str, Any]]) -> str | None:
    result: str | None = None
    for r in rows:
        if r.get("kind") != "status":
            continue
        st = str(_payload(r).get("status"))
        if st in TERMINAL_STATES:
            result = st
        elif st == "RUNNING":
            result = None
    return result


def _norm_rel(path: str) -> str:
    """Canonical workspace-relative key using the product's workspace-prefix contract.

    The agent is explicitly taught that the guest root is ``/workspace``, so actions often
    spell ``/workspace/index.html`` while successful tool observations report the product-
    resolved ``index.html``.  These are the same jailed path.  Reuse the product's prefix
    rule, then retain the adapter's narrow legacy normalization.  Deliberately do NOT
    collapse dot segments here: a traversal-shaped action and a clean observation must
    disagree and fail closed instead of being joined by the evidence collector.
    """
    s = strip_redundant_workspace_prefix(str(path))
    if s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _needs_resolved_write_proof(path: str) -> bool:
    """Whether ``path`` relies on the guest-only workspace alias.

    A relative action path is already in the snapshot namespace, so historical event logs
    without structured mutator output can retain the action-content SHA fallback.  A
    ``workspace/``-prefixed path is ambiguous outside the product resolver; require the
    successful observation's canonical path and digest before calling it content-proven.
    """
    s = str(path)
    return s in {"workspace", "/workspace"} or s.startswith(("workspace/", "/workspace/"))


def _choose_alternative(
    options: list[dict[str, Any]], preferred_option_id: str | None
) -> str | None:
    """Pick an AlternativeOption id deterministically: the caller's `preferred_option_id`
    (scenario override) when it names a valid option, else a recommended option (a
    `recommended`/`recommendation` truthy flag, defensively — the option may not carry one),
    else the FIRST valid option. None when there is no option with a non-empty id."""
    valid = [o for o in options if isinstance(o, dict) and o.get("id")]
    if not valid:
        return None
    if preferred_option_id is not None:
        for o in valid:
            if str(o["id"]) == str(preferred_option_id):
                return str(o["id"])
    for o in valid:
        if o.get("recommended") or o.get("recommendation"):
            return str(o["id"])
    return str(valid[0]["id"])


# Snapshot acceptance proof levels — PROVEN/authoritative vs NON-authoritative — recorded
# per declared file so the OutputTruthOracle can proof-gate a content mismatch (a mismatch on
# a non-authoritative capture is unreliable and must NOT be a definitive product failure).
_PROOF_BY_EXPECTED_KIND = {
    "sha": "raw_sha",
    "rendered": "rendered_readback",
    "absent": "absent",
    "present_unproven": "unproven_extended_stability",
    "unknown": "unknown",
}


def _proof_level(expected: tuple[Any, ...] | None) -> str:
    """Map a declared file's `_agent_declared_expected` class to its acceptance proof level.
    A file with no event signal at all (`expected` None) is `unknown` (non-authoritative)."""
    kind = expected[0] if expected else "unknown"
    return _PROOF_BY_EXPECTED_KIND.get(kind, "unknown")


def _manifest_lookup(manifest: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The manifest entry for a declared path under either its exact key or its
    normalized relpath (the snapshot walk keys by the leading-slash-free relpath)."""
    entry = manifest.get(path)
    if entry is None:
        entry = manifest.get(_norm_rel(path))
    return entry if isinstance(entry, dict) else None


def _manifest_present(manifest: dict[str, Any], path: str) -> bool:
    return _manifest_lookup(manifest, path) is not None


def _manifest_sha(manifest: dict[str, Any], path: str) -> str | None:
    entry = _manifest_lookup(manifest, path)
    return entry.get("sha256") if entry else None


def _manifest_ident(manifest: dict[str, Any], path: str) -> tuple[Any, Any] | None:
    """A change-detection identity for content-stability: (size, sha256), or None when the
    file is absent (absence is itself a stable-able state)."""
    entry = _manifest_lookup(manifest, path)
    if entry is None:
        return None
    return (entry.get("size"), entry.get("sha256"))


_SHELL_META_TOKENS = frozenset({"&&", "||", ";", "|", "|&", "<", ">", ">>", "<<"})
_SHELL_META_SUBSTRINGS = ("&&", "||", ";", "|", "<", ">")


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _shell_pipeline_tokens(command: str) -> list[str] | None:
    """Tokenize a small shell pipeline while retaining control operators.

    This is used only to *prove* a command read-only.  Any syntax we do not understand returns
    None and therefore keeps the existing fail-safe opaque-mutation downgrade.
    """
    if not command or any(marker in command for marker in ("\n", "\r", "`", "$(", "${")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _curl_segment_is_read_only(tokens: list[str]) -> bool:
    """True for the bounded curl GET/probe surface used by build verification.

    curl has several local-file output switches, so this is an allowlist rather than a blacklist.
    ``-o /dev/null`` is the only permitted output target; response bodies otherwise go to stdout.
    """
    no_value = {
        "-s",
        "--silent",
        "-S",
        "--show-error",
        "-f",
        "--fail",
        "-L",
        "--location",
        "-I",
        "--head",
    }
    with_value = {"-w", "--write-out"}
    saw_url = False
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in no_value:
            i += 1
            continue
        if token in with_value:
            # curl 8.3+ supports `%output{filename}` inside --write-out; accepting an
            # arbitrary format would let a command classified "read-only" overwrite a
            # declared file.  The live readiness probe needs only this stdout-only token.
            if i + 1 >= len(tokens) or tokens[i + 1] != "%{http_code}":
                return False
            i += 2
            continue
        if token in {"-o", "--output"}:
            if i + 1 >= len(tokens) or tokens[i + 1] != "/dev/null":
                return False
            i += 2
            continue
        if token.startswith("--output="):
            if token != "--output=/dev/null":
                return False
            i += 1
            continue
        if token == "--":
            return i + 1 < len(tokens) and all(
                part.startswith(("http://", "https://")) for part in tokens[i + 1 :]
            )
        if token.startswith(("http://", "https://")):
            saw_url = True
            i += 1
            continue
        return False
    return saw_url


def _http_server_segment_is_read_only(tokens: list[str]) -> bool:
    """Recognize only Python's standard static HTTP server invocation."""
    if len(tokens) not in {4, 6} or posixpath.basename(tokens[0]) not in {"python", "python3"}:
        return False
    if tokens[1:3] != ["-m", "http.server"] or not tokens[3].isdigit():
        return False
    if len(tokens) == 4:
        return True
    return tokens[4] in {"-b", "--bind", "-d", "--directory"} and bool(tokens[5])


def _shell_is_proven_read_only(command: str, declared: set[str]) -> bool:
    """Conservatively prove that a shell action cannot alter declared workspace files.

    Unknown programs, redirects, substitutions, backgrounding, and malformed syntax remain
    opaque.  The one Python ``-c`` exception is compared to the product's own exact generated
    static-verifier command, avoiding a second permissive parser for arbitrary Python.
    """
    if any(marker in command for marker in ("\n", "\r", "`", "$(", "${")):
        return False
    try:
        from disco.core.loop.finish.common import _static_verify_command

        if any(command == _static_verify_command(path) for path in declared):
            return True
    except (ImportError, TypeError, ValueError):
        pass

    tokens = _shell_pipeline_tokens(command)
    if not tokens:
        return False
    operators = {"&&", "||", "|", "|&", ";"}
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {"<", ">", ">>", "<<", "&"}:
            return False
        if token in operators:
            if not segments[-1]:
                return False
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1]:
        return False

    for segment in segments:
        program = posixpath.basename(segment[0])
        if program in {"test", "[", "echo", "head", "grep"}:
            if program == "[" and segment[-1] != "]":
                return False
            continue
        if program == "cd" and len(segment) == 2:
            continue
        if program == "curl" and _curl_segment_is_read_only(segment):
            continue
        if _http_server_segment_is_read_only(segment):
            continue
        return False
    return True


def _has_shell_meta(tokens: list[str]) -> bool:
    return any(
        tok in _SHELL_META_TOKENS or any(marker in tok for marker in _SHELL_META_SUBSTRINGS)
        for tok in tokens
    )


def _shell_norm_rel(path: str) -> str:
    norm = posixpath.normpath(_norm_rel(path))
    return "." if norm == "" else norm


def _split_flags_and_paths(tokens: list[str]) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    paths: list[str] = []
    after_separator = False
    for tok in tokens:
        if not after_separator and tok == "--":
            after_separator = True
            continue
        if not after_separator and tok.startswith("-") and tok != "-":
            flags.append(tok)
            continue
        paths.append(tok)
    return flags, paths


def _rm_has_recursive_flag(flags: list[str]) -> bool:
    for flag in flags:
        if flag in {"-r", "-R", "--recursive"}:
            return True
        if flag.startswith("--"):
            continue
        if flag.startswith("-") and any(ch in flag[1:] for ch in ("r", "R")):
            return True
    return False


def _contains_relpath(parent: str, child: str) -> bool:
    if parent == child:
        return True
    if parent == ".":
        return child not in {"", "."}
    return child.startswith(f"{parent}/")


def _rm_removes_declared(tokens: list[str], norm_path: str) -> bool:
    flags, raw_paths = _split_flags_and_paths(tokens)
    declared = _shell_norm_rel(norm_path)
    targets = [_shell_norm_rel(p) for p in raw_paths]
    if declared in targets:
        return True
    if not _rm_has_recursive_flag(flags):
        return False
    return any(_contains_relpath(target, declared) for target in targets)


def _mv_removes_declared(tokens: list[str], norm_path: str) -> bool:
    _flags, raw_paths = _split_flags_and_paths(tokens)
    if len(raw_paths) != 2:
        return False
    declared = _shell_norm_rel(norm_path)
    source = _shell_norm_rel(raw_paths[0])
    dest = _shell_norm_rel(raw_paths[1])
    return source == declared and dest != declared


def _shell_removes(command: str, norm_path: str) -> bool:
    """True iff `command` is a PURE delete/rename that makes `norm_path` absent.

    Marking a declared file ABSENT is fail-FAST (unsatisfied → hard INVALID), so this must
    be strict, not fuzzy: the old basename fallback marked the ROOT deliverable absent when
    an export flow rm'd a COPY (export/index.html) — REL-6 EXPORT class false-INVALID
    (conv_bc52c276). Now: tokens are parsed shell-style, every token must belong to a pure
    rm/mv invocation (no zip/cp/&&/; compound ops — those go to the fail-safe OPAQUE
    downgrade instead), and the declared path must match by normalized relpath. Recursive rm
    of a parent directory marks declared children absent; a pure two-path mv marks the source
    absent. Anything less certain degrades to present_unproven, which only ever relaxes."""
    toks = _shell_tokens(command)
    if not toks or _has_shell_meta(toks):
        return False
    if toks[0] == "rm":
        return _rm_removes_declared(toks[1:], norm_path)
    if toks[0] == "mv":
        return _mv_removes_declared(toks[1:], norm_path)
    return False


# OPAQUE mutation tools. A shell command (either shell tool name) or a project-script can rewrite a
# declared path in a way we cannot reconstruct from the action alone, so after one of these the path
# degrades to content-unprovable (present_unproven) rather than leaving an earlier modeled write's
# sha standing as the "final" expected — which false-INVALIDs a snapshot that correctly holds the
# post-mutation bytes. Fail-safe by design: only ever relaxes a sha to present, never a false
# INVALID.
_SHELL_TOOLS = frozenset({"shell", "shell_exec"})
_SCRIPT_MUTATE_TOOLS = frozenset({"run_project_script"})


def _full_readback_body(args: dict[str, Any], content: Any) -> str | None:
    """Return the RENDERED body of a FULL, GENUINELY-RENDERED ``file_read`` observation, or
    None when the read is paged / truncated / synthetic (and so cannot define the agent's
    final bytes). The body is the readback MINUS its ``[lines 1-N of N]`` header line — i.e.
    the ``<line-no>\\t<line>`` block, ready to compare against ``_render_numbered`` of the
    on-disk file.

    Rejections (→ None): an explicit ``offset``/``limit`` (a targeted, partial read); any
    header that is not the exact full-read shape (a budget-truncated ``; read more…`` or a
    pressure ``— HEAD-ONLY…`` read has unequal/extra header text); the F9 dedup pointer or
    any other synthetic content (no ``[lines 1-N of N]`` header at all)."""
    if args.get("offset") is not None or args.get("limit") is not None:
        return None
    if not isinstance(content, str):
        return None
    head, sep, body = content.partition("\n")
    m = _FILE_READ_FULL_HEADER_RE.match(head)
    if m is None or m.group(1) != m.group(2):
        return None  # not a full read (or a synthetic [F9 dedup: …] / non-rendered payload)
    # `sep` empty ⇒ a header-only (empty-file) read: body is "" (matches _render_numbered("")).
    return body if sep else ""


def _render_numbered(text: str) -> str:
    """Render `text` EXACTLY as ``file_read`` renders a full read — 1-based, right-aligned
    line numbers + a tab, no trailing newline — by importing the PRODUCT's own pure helper
    (``disco.tools.builtin.files._number_lines``) so the format can never drift from the
    readback we compare against. Imported lazily to keep the deterministic test path free of
    the live tool deps until a readback signal actually needs rendering."""
    from disco.tools.builtin.files import _number_lines

    return _number_lines(text, 1)


def _agent_declared_expected(
    events: list[dict[str, Any]], declared: list[str]
) -> dict[str, tuple[Any, ...]]:
    """Reconstruct each DECLARED file's AGENT-FINAL state from the durable event log, keyed by
    the original declared-path string. A mutation / read counts only when its action has a
    SUCCESSFUL observation and no agent_error (a failed/aborted op is NOT the final state).

    Priority per path (seq-ordered — the LAST mutation, plus any FULL readback after it):
      a. last mutation is ``file_write`` (full, non-elided content) → ``("sha", <hexdigest>)``
         over the RAW written bytes (strongest signal).
      b. else a partial mutator (edit/append/replace/insert/str_replace, or an elided
         file_write) is the last mutation AND a FULL, genuinely-rendered ``file_read``
         readback exists with seq AFTER it → ``("rendered", <readback body>)`` (compared in
         file_read's rendered space — closes the partial-edit-last stale-snapshot gap).
      c. else last op is a shell ``rm`` of the path → ``("absent",)``.
      d. else a partial mutator with NO qualifying post-mutation readback →
         ``("present_unproven",)`` (content can't be proven; extended-stability + warn).
      e. else no signal → omitted (caller treats as ``unknown``)."""
    want = {_norm_rel(p): p for p in declared}
    if not want:
        return {}

    obs_success: dict[str, bool] = {}
    obs_content: dict[str, Any] = {}
    obs_structured: dict[str, Any] = {}
    err_call_ids: set[str] = set()
    for e in events:
        kind = e.get("kind")
        if kind == "observation":
            tr = _payload(e).get("tool_result") or {}
            cid = tr.get("call_id")
            if cid is not None:
                obs_success[str(cid)] = bool(tr.get("success", True))
                obs_content[str(cid)] = tr.get("content")
                obs_structured[str(cid)] = tr.get("structured")
        elif kind == "agent_error":
            tcid = _payload(e).get("tool_call_id")
            if tcid is not None:
                err_call_ids.add(str(tcid))

    # Per path: the LAST mutation (seq, kind, sha?) and the LAST qualifying full readback
    # (seq, rendered body). Combined after the walk so ordering decides which wins.
    last_mut: dict[str, tuple[int, str, str | None]] = {}  # np -> (seq, kind, write_sha)
    last_read: dict[str, tuple[int, str]] = {}  # np -> (seq, body)
    for e in events:  # seq order (the DB read is ORDER BY seq)
        if e.get("kind") != "action":
            continue
        seq = int(e.get("seq", -1))
        tc = _payload(e).get("tool_call") or {}
        call_id = tc.get("call_id")
        name = tc.get("tool_name")
        args = tc.get("arguments") or {}
        if call_id is None:
            continue
        cid = str(call_id)
        if cid in err_call_ids or not obs_success.get(cid, False):
            continue  # only a SUCCESSFUL, un-errored op is the agent's real state
        if name in _FILE_WRITE_FULL_TOOLS:
            raw_path = str(args.get("path", ""))
            np = _norm_rel(raw_path)
            content = args.get("content")
            structured = obs_structured.get(cid)
            observed_path: str | None = None
            observed_sha: str | None = None
            if isinstance(structured, dict):
                spath = structured.get("path")
                ssha = structured.get("sha256")
                if isinstance(spath, str) and spath:
                    observed_path = _norm_rel(spath)
                if isinstance(ssha, str) and _RAW_SHA256_RE.fullmatch(ssha):
                    observed_sha = ssha.lower()

            # A structured result is the product's post-resolution write receipt.  It may
            # legitimately canonicalize `/workspace/x` to `x`, but it must never redirect
            # the action to another declared path or contradict the bytes in the action.
            # Any contradiction records BOTH implicated declared paths as mutated-but-
            # unproven so an older precise SHA cannot survive the bad receipt.
            if isinstance(structured, dict):
                implicated = {p for p in (np, observed_path) if p in want}
                action_sha = (
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if isinstance(content, str) and not _ELISION_MARKER_RE.search(content)
                    else None
                )
                receipt_valid = (
                    observed_path is not None
                    and observed_path == np
                    and observed_sha is not None
                    and action_sha is not None
                    and action_sha == observed_sha
                )
                if receipt_valid and np in want:
                    last_mut[np] = (seq, "sha", observed_sha)
                elif implicated:
                    for implicated_path in implicated:
                        last_mut[implicated_path] = (seq, "present", None)
                continue

            if np not in want:
                continue
            if (
                not _needs_resolved_write_proof(raw_path)
                and isinstance(content, str)
                and not _ELISION_MARKER_RE.search(content)
            ):
                # Backward compatibility for older relative-path events recorded before
                # successful mutators emitted structured path/SHA receipts.
                sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                last_mut[np] = (seq, "sha", sha)
            else:  # missing receipt for an alias, elided bytes, or unknown bytes
                last_mut[np] = (seq, "present", None)
        elif name in _FILE_WRITE_PARTIAL_TOOLS:
            np = _norm_rel(str(args.get("path", "")))
            if np not in want:
                continue
            last_mut[np] = (seq, "present", None)
        elif name in _FILE_READ_TOOLS:
            np = _norm_rel(str(args.get("path", "")))
            if np not in want:
                continue
            body = _full_readback_body(args, obs_content.get(cid))
            if body is not None:
                last_read[np] = (seq, body)
        elif name in _SHELL_TOOLS:
            command = str(args.get("command") or args.get("cmd") or "")
            if _shell_is_proven_read_only(command, set(want)):
                continue
            for np in want:
                if _shell_removes(command, np):
                    last_mut[np] = (seq, "absent", None)
                else:
                    # OPAQUE mutation. A shell command can rewrite a declared path (sed -i /
                    # redirect / heredoc / an interpreter one-liner) in a way we cannot reconstruct
                    # — and we can't reliably tell a write from a read from the command string.
                    # Enumerating "which shell forms write" lost twice (missed `shell_exec`, missed
                    # heredocs), so
                    # fail SAFE: any non-rm shell op downgrades every declared path to
                    # content-unprovable. Seq-ordered, so this only overrides an EARLIER modeled
                    # write's sha (a later real file_write re-establishes a precise sha). Cost is
                    # precision (present_unproven vs sha) — never a false INVALID, which is the
                    # point.
                    last_mut[np] = (seq, "present", None)
        elif name in _SCRIPT_MUTATE_TOOLS:
            # run_project_script mutates via its `operations` list; a save/replace_text op on a
            # declared path is a content-unprovable mutation (multi-op splice, not reconstructable).
            ops = args.get("operations")
            if isinstance(ops, list):
                for op in ops:
                    if not isinstance(op, dict) or op.get("op") not in ("save", "replace_text"):
                        continue
                    npx = _norm_rel(str(op.get("path", "")))
                    if npx in want:
                        last_mut[npx] = (seq, "present", None)

    by_norm: dict[str, tuple[Any, ...]] = {}
    for np in want:
        mut = last_mut.get(np)
        rd = last_read.get(np)
        if mut is None:
            continue  # (e) no mutation signal → unknown (omitted)
        mseq, mkind, msha = mut
        if mkind == "sha":
            by_norm[np] = ("sha", msha)  # (a)
        elif mkind == "absent":
            by_norm[np] = ("absent",)  # (c)
        elif rd is not None and rd[0] > mseq:
            by_norm[np] = ("rendered", rd[1])  # (b) full readback AFTER the last partial edit
        else:
            by_norm[np] = ("present_unproven",)  # (d) partial edit, no provable post-state
    return {want[np]: st for np, st in by_norm.items()}


def _manifest_content(manifest: dict[str, Any], path: str) -> str | None:
    """The on-disk file's decoded UTF-8 text from the manifest entry (empty for a binary /
    oversized file), or None when the file is absent."""
    entry = _manifest_lookup(manifest, path)
    if entry is None:
        return None
    c = entry.get("content")
    return c if isinstance(c, str) else ""


def _evaluate_snapshot_readiness(
    declared: list[str],
    expected: dict[str, tuple[Any, ...]],
    manifest: dict[str, Any],
    stable_n: dict[str, int],
) -> tuple[bool, list[str], list[str]]:
    """Decide whether the snapshot has settled on the agent's final state.

    Returns ``(ready, blocking, unproven_ready)``:
      * ``ready`` — EVERY declared file's expected state is met.
      * ``blocking`` — UNSATISFIED files carrying a DEFINITE, content-precise signal
        (``sha`` / ``rendered`` / ``absent``). On the wait-budget deadline a non-empty
        ``blocking`` is the FAIL-FAST trigger (the snapshot is provably behind the agent).
      * ``unproven_ready`` — files accepted ONLY via extended content-stability
        (``present_unproven`` / ``unknown``), i.e. with no content proof; the caller logs a
        warning. These NEVER fail-fast (a legit edit-without-readback build must not become
        INVALID_RUN); an unsatisfied unproven file simply keeps `ready` False and is accepted
        best-effort at the deadline.

    Content-precise gates:
      * ``sha`` — on-disk raw sha256 == the written bytes' sha.
      * ``rendered`` — the on-disk file RENDERED by file_read's own numberer == the readback.
      * ``absent`` — the file is NOT on disk.
    Stability gates (count of consecutive identical reads in ``stable_n``):
      * ``present_unproven`` — present AND ``>= _SNAPSHOT_UNPROVEN_STABLE_POLLS`` (+warn).
      * ``unknown`` — present-or-absent stable ``>= _SNAPSHOT_UNPROVEN_STABLE_POLLS`` (+warn)."""
    ready = True
    blocking: list[str] = []
    unproven_ready: list[str] = []
    for p in declared:
        kind = expected.get(p, ("unknown",))[0]
        present = _manifest_present(manifest, p)
        n = stable_n.get(p, 0)
        definite = False
        if kind == "sha":
            ok = present and _manifest_sha(manifest, p) == expected[p][1]
            definite = True
        elif kind == "rendered":
            on_disk_rendered = _render_numbered(_manifest_content(manifest, p) or "")
            ok = present and on_disk_rendered == expected[p][1]
            definite = True
        elif kind == "absent":
            ok = not present
            definite = True
        elif kind == "present_unproven":
            ok = present and n >= _SNAPSHOT_UNPROVEN_STABLE_POLLS
            if ok:
                unproven_ready.append(p)
        else:  # unknown — require PRESENT + extended stability (NOT stable-absent).
            # Settling on a STABLE-ABSENT declared file accepted a PARTIAL snapshot (only the
            # .pmx/ live-browser scaffolding had flushed) as final, before the real output
            # (e.g. index.html) appeared → a FALSE_FINISH_NO_OUTPUT false-negative on a build
            # that actually succeeded. Requiring `present` makes a not-yet-flushed file keep
            # the snapshot un-ready; a file that is GENUINELY never created waits the full
            # window and the run's own timeout flags it (correct), instead of premature ready.
            ok = present and n >= _SNAPSHOT_UNPROVEN_STABLE_POLLS
            if ok:
                unproven_ready.append(p)
        if not ok:
            ready = False
            if definite:
                blocking.append(p)
    return ready, blocking, unproven_ready


def _classify_conn_error(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "name" in text or "resolve" in text or "dns" in text:
        return "dns"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "connection_error"


# ---- real HTTP/WS transport (used by the live runner; not in CI) ------------


class HttpTransport:
    """httpx + websockets transport against a running agent-server. Imported lazily
    so the deterministic test path never needs the live deps loaded.

    AUTH (2026-07-09): the agent-server now requires a paired session on every
    non-public route (the fresh-install pairing work), so the transport bootstraps
    one lazily via POST /api/auth/mint. Against the loopback-bound dev posture the
    localhost auto-pair admits a tokenless mint (Origin == base_url == localhost);
    an exposed/remote target can supply DISCO_SOAK_PAIRING_TOKEN instead. The
    session cookie + CSRF header then ride every request (and the WS connect)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        timeout_s: float = 120.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._transport = _transport
        self._cookie: str | None = None  # "disco_session=<value>"
        self._csrf: str | None = None

    async def _ensure_session(self) -> None:
        if self._cookie is not None:
            return
        import httpx

        body: dict[str, Any] = {}
        token = os.environ.get("DISCO_SOAK_PAIRING_TOKEN")
        if token:
            body["pairing_token"] = token
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(
                f"{self.base_url}/api/auth/mint",
                json=body,
                headers={"Origin": self.base_url},
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"soak transport could not pair with the agent-server: HTTP {r.status_code} "
                f"{r.text[:200]} — on a non-loopback target set DISCO_SOAK_PAIRING_TOKEN"
            )
        data = _safe_json(r)
        self._csrf = str(data.get("csrf_token") or "")
        cookie = r.cookies.get("disco_session")
        if not cookie:
            raise RuntimeError("mint succeeded but no disco_session cookie was set")
        self._cookie = f"disco_session={cookie}"

    def _headers(self, *, unsafe: bool) -> dict[str, str]:
        h = {"Origin": self.base_url, "Cookie": self._cookie or ""}
        if unsafe and self._csrf:
            h["X-Disco-CSRF"] = self._csrf
        return h

    def _ws_url(self, conversation_id: str) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}/ws/conversations/{conversation_id}"

    async def post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(
                f"{self.base_url}{path}", json=body, headers=self._headers(unsafe=True)
            )
            return r.status_code, _safe_json(r)

    async def get_json(self, path: str) -> tuple[int, dict[str, Any]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, _safe_json(r)

    async def get_text(self, path: str) -> tuple[int, str, dict[str, str]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, r.text, dict(r.headers)

    async def post_file(
        self,
        path: str,
        *,
        field: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> tuple[int, dict[str, Any]]:
        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(
                f"{self.base_url}{path}",
                files={field: (filename, content, content_type)},
                headers=self._headers(unsafe=True),
            )
            return r.status_code, _safe_json(r)

    async def get_bytes(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        await self._ensure_session()
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, r.content, dict(r.headers)

    async def fetch_isolated_preview(self, conversation_id: str) -> tuple[int, str, dict[str, str]]:
        """Mint/redeem the generation-bound canonical Preview without the app session.

        Capability response fields and the isolated origin are validated before
        the one-time bearer is redeemed.  Redemption and generated-content fetch
        use a fresh cookie jar, redirects and environment proxies are disabled,
        and an application session cookie in that jar fails closed.
        """

        await self._ensure_session()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as app_client:
                minted = await app_client.post(
                    f"{self.base_url}/conversations/{conversation_id}/preview/capability",
                    json={
                        "target_path": "/",
                        "transport": "canonical",
                    },
                    headers=self._headers(unsafe=True),
                )
            if minted.status_code != 200:
                return minted.status_code, minted.text, dict(minted.headers)
            body = _safe_json(minted)
            selected_port = body.get("port")
            if (
                body.get("transport") != "canonical"
                or body.get("target_path") != "/"
                or not isinstance(body.get("preview_authority"), str)
                or not body.get("preview_authority")
                or not isinstance(selected_port, int)
                or isinstance(selected_port, bool)
                or (
                    selected_port not in USER_PORTS
                    and not is_managed_host_preview_port(selected_port)
                )
                or selected_port == NOVNC_PORT
            ):
                return 502, "invalid preview capability response", {}
            bootstrap_url = str(body.get("bootstrap_url") or "")
            intent = str(body.get("bootstrap_intent") or "")
            isolated_url = validated_canonical_preview_url(
                self.base_url,
                conversation_id,
                selected_port,
                bootstrap_url,
            )
            if not intent or isolated_url is None:
                return 502, "invalid preview capability response", {}

            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as preview_client:
                redeemed = await preview_client.post(
                    bootstrap_url,
                    data={"intent": intent},
                    headers={"Origin": self.base_url},
                )
                if redeemed.status_code != 200:
                    # A malformed/hostile bootstrap must not reflect the one-use
                    # bearer into retained preview evidence.
                    return redeemed.status_code, "preview capability redemption failed", {}
                if "cookies" in redeemed.headers.get("clear-site-data", "").lower():
                    if tuple(preview_client.cookies.jar):
                        return 502, "preview storage reset installed a cookie too early", {}
                    handoff = _preview_storage_handoff(redeemed.text)
                    if handoff is None:
                        return 502, "invalid preview storage reset response", {}
                    completed = await preview_client.post(
                        bootstrap_url,
                        data={"handoff": handoff},
                        headers={"Origin": self.base_url},
                    )
                    if completed.status_code != 200:
                        return completed.status_code, "preview storage handoff failed", {}
                    completion = _safe_json(completed)
                    if completion.get("target") != body["target_path"]:
                        return 502, "invalid preview storage handoff response", {}
                if any(cookie.name == SESSION_COOKIE for cookie in preview_client.cookies.jar):
                    return 502, "preview bootstrap crossed application session", {}
                preview = await preview_client.get(isolated_url)
                return preview.status_code, preview.text, dict(preview.headers)
        except (httpx.HTTPError, OSError, ValueError):
            # Post-create boundary failure: retain a non-success status for the
            # output oracle, never a local snapshot PASS and never the bearer.
            return 599, "isolated preview request failed", {}

    async def ws_control(self, conversation_id: str, frame: dict[str, Any]) -> None:
        import websockets

        await self._ensure_session()
        url = self._ws_url(conversation_id)
        # websockets>=12 renamed extra_headers → additional_headers.
        headers = [("Origin", self.base_url), ("Cookie", self._cookie or "")]
        async with websockets.connect(
            url, open_timeout=self._timeout, additional_headers=headers
        ) as ws:
            # Drain the initial state frame the server sends on connect, then send
            # the control frame. We do NOT wait for an ack — truth is the event log.
            try:
                await asyncio.wait_for(ws.recv(), timeout=self._timeout)
            except TimeoutError:
                pass
            await ws.send(json.dumps(frame))
            # Give the server a beat to process before the socket closes.
            await asyncio.sleep(0.2)

    async def health(self) -> tuple[int, dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.get(f"{self.base_url}/health")
            return r.status_code, _safe_json(r)


def _preview_storage_handoff(document: str) -> str | None:
    """Extract the body-only handoff from the product's locked reset document."""

    match = _PREVIEW_RESET_BODY_RE.search(document)
    if match is None:
        return None
    try:
        encoded = json.loads(match.group(1))
        parsed = urllib.parse.parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except (TypeError, ValueError):
        return None
    if set(parsed) != {"handoff"} or len(parsed["handoff"]) != 1:
        return None
    handoff = parsed["handoff"][0]
    return handoff if 0 < len(handoff) <= _MAX_PREVIEW_HANDOFF_CHARS else None


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return {}
    return data if isinstance(data, dict) else {"_": data}
