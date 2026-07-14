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
                             the served app isn't up). Falls back to the preview proxy.
  collect_preview            GET /conversations/{cid}/preview + preview-app root

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
import json
import logging
import os
import posixpath
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx  # the adapter MAY import an http client (oracle path stays disco/http-free)

from ..events import NormalizationError, normalize_events
from ..oracles.thrash import ThrashOracle

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
# Internal dirs the served-root index detection must skip — mirrors the product's
# lifecycle._find_snapshot_index / SandboxSession._detect_serve_dir skip set exactly, so a
# .pmx/.disco/node_modules index.html is NEVER mistaken for the build's served root.
_SERVE_SKIP_DIRS = frozenset({".pmx", ".disco", "node_modules"})
# Reserved control ports the durable serve+probe must NEVER bind (defense in depth — the
# probe binds port 0 so the OS assigns a free EPHEMERAL high port, never these): the
# agent-server (8000), the app-server (8800), and the conventional vite dev port (5173).
_RESERVED_CONTROL_PORTS = frozenset({8000, 8800, 5173})

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
    ) -> None:
        self._t = transport
        self._db_path = db_path
        self._poll = poll_interval_s
        # ProjectStore root for the authoritative workspace SNAPSHOT read (Bug 9 fix).
        # None ⇒ snapshot read DISABLED (deterministic fake-transport tests keep the
        # pure preview-proxy path, byte-identical to before). The live CLI passes ""
        # so it mirrors the agent-server's OWN default root resolution (same env), and
        # the snapshot unit test injects a tmp root.
        self._projects_root = projects_root
        # How long to wait for the snapshot to flush after the run reaches a terminal
        # state — the build appends FINISHED INSIDE loop.run(), then `_maybe_snapshot`
        # writes the workspace to the ProjectStore; a fast collect can read between the
        # two. Re-read the snapshot until every declared file is present (or this budget
        # elapses) so a genuinely-present file is never reported missing.
        self._snapshot_wait_s = snapshot_wait_s
        # The conversation_id this client most recently CREATED (set in
        # create_build_conversation). Lets the runner reach the cid for teardown even when
        # drive_scenario raised before returning a CollectedRun (so an abandoned RUNNING
        # build can still be killed from run_once's finally). None until a create succeeds.
        self.last_conversation_id: str | None = None
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

    def enable_live_thrash_monitor(self, scenario: dict[str, Any]) -> None:
        self._live_thrash_scenario = scenario
        self._live_thrash_samples = 0
        self._live_thrash_findings = []
        self._live_thrash_candidate = ""
        self._live_thrash_candidate_count = 0
        self._live_thrash_recorded = set()
        self._live_thrash_last_sample = time.monotonic()

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

    async def _sample_live_thrash(
        self, conversation_id: str, *, terminal_status: str = ""
    ) -> bool:
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
    ) -> str:
        """POST /conversations then POST the user message (which the route appends
        AND kicks). Returns the conversation_id."""
        if surface not in {"build", "agent"}:
            raise ValueError(f"unsupported soak conversation surface: {surface!r}")
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
        # POST the user task — appends the USER message AND kicks the loop. AppKit
        # lanes mirror the production UI's initial frame: `build_brief` present makes
        # the route classify a Build Brief from the prompt (codex finding #11 — without
        # it the contract-activation path never fires for the soak).
        mbody: dict[str, Any] = {"content": prompt}
        if appkit:
            mbody["build_brief"] = {}
        mstatus, _ = await self._t.post_json(f"/conversations/{cid}/messages", mbody)
        if mstatus >= 400:
            raise RuntimeError(f"post_message failed: HTTP {mstatus}")
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
        await self._t.ws_control(
            conversation_id, {"type": "pick_alternative", "option_id": chosen}
        )
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
        status, data = await self._t.post_json(
            f"/conversations/{conversation_id}/resume", {}
        )
        return {"http_status": status, **(data if isinstance(data, dict) else {})}

    async def kill(self, conversation_id: str) -> dict[str, Any]:
        """KILL (force-terminate) a conversation the runner is DONE with — the hygiene
        teardown so an abandoned RUNNING / PAUSED / AWAITING build the runner stopped
        watching (inconclusive cutoff, error path, or post-evidence release) does not
        leak and load the shared server. POST /conversations/{cid}/kill
        (conversations.py:275 — halts the agent, tears down its sandbox, revokes its
        capabilities). IDEMPOTENT: a kill on an already-terminal conversation is harmless.

        The HTTP code is returned under `http_status` (mirrors :meth:`resume`); the route
        body is ``{"killed": true, "state": {...}}``. Returns ``{"http_status": int, ...}``."""
        status, data = await self._t.post_json(
            f"/conversations/{conversation_id}/kill", {}
        )
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
          (b) the build goes GENUINELY INACTIVE — no NEW events and no status change for
              `inactivity_s` → return INACTIVE_TIMEOUT (a real wedge: classify normally); OR
          (c) the generous `hard_cap_s` ceiling is hit. If the build was STILL progressing
              within the last inactivity window when the cap hit → PROGRESSING_TIMEOUT
              (inconclusive, NOT a product fail); otherwise → INACTIVE_TIMEOUT.

        A still-actively-progressing build is NEVER cut off by a mere wall-clock elapsing:
        the inactivity timer RESETS whenever the event count / max seq advances OR the status
        transitions. Only genuine silence or the safety ceiling ends the wait.

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
        while True:
            last = self._status_of(await self.get_state(conversation_id))
            thrash_crossed = await self._sample_live_thrash(
                conversation_id, terminal_status=last
            )
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
            if last in _WORK_TERMINALS and self._terminal_is_new(
                conversation_id, min_terminal_seq
            ):
                return last
            if last == PAUSED_STATE:
                # a cooperative / actionless PAUSE — the driver decides (resume or stop).
                return last
            if last == "IDLE":
                if seen_active and self._terminal_is_new(conversation_id, min_terminal_seq):
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

            inactive_for = now - last_progress
            if now - start >= hard_cap_s:
                # Safety ceiling. Was it still progressing recently? Then this is an
                # inconclusive model-speed cutoff, NOT a wedge → PROGRESSING_TIMEOUT.
                return (
                    PROGRESSING_TIMEOUT if inactive_for < inactivity_s else INACTIVE_TIMEOUT
                )
            if inactive_for >= inactivity_s:
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
                if (
                    call_id is not None and str(call_id) in success_call_ids
                ) or (
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
        try:
            status, data = await self._t.get_json(f"/api/debug/trace/{conversation_id}")
        except Exception:  # noqa: BLE001 — campaign policy adjudicates absence
            return None
        if status >= 400 or not isinstance(data, dict):
            return None
        if str(data.get("conversation_id") or "") != conversation_id:
            return None
        if not isinstance(data.get("events"), list):
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

        Fallback: ONLY when there is NO snapshot at all (no projects_root configured, or
        the conversation's snapshot workspace dir never materialized), each declared path
        is fetched via the preview proxy — so a no-storage deployment is never WORSE than
        before. When the snapshot IS authoritative (its workspace dir exists), the proxy
        is NEVER consulted: a declared file absent from the snapshot is genuinely missing
        and is OMITTED, so the proxy can't mask a missing required deliverable with a
        served/stale copy (anti-false-PASS hole #1).
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

        # No snapshot at all → legacy preview-proxy fallback (no-storage deployments only).
        for path in declared:
            if path in manifest:
                continue
            rel = path.lstrip("/")
            status, text, _hdrs = await self._t.get_text(
                f"/conversations/{conversation_id}/preview-app/{rel}"
            )
            if status < 400:
                entry = _file_entry(text.encode("utf-8"))
                entry["source"] = "preview_proxy"
                manifest[path] = entry
        return manifest

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
        while True:
            snapshot_dir = self._snapshot_workspace_dir(conversation_id)
            manifest = (
                self._read_snapshot_manifest(conversation_id, declared, snapshot_dir)
                if snapshot_dir is not None
                else {}
            )
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
            if ready and (not unproven_ready or time.monotonic() >= deadline):
                for p in unproven_ready:
                    _LOG.warning(
                        "snapshot %s: declared file %r accepted on EXTENDED content-stability "
                        "(%s) — no sha/readback proof of the agent's final bytes (expected=%s)",
                        conversation_id, p, expected.get(p, ("unknown",))[0],
                        list(expected.get(p, ("unknown",))),
                    )
                break
            if time.monotonic() >= deadline:
                if blocking:
                    raise SnapshotNotReadyError(
                        "workspace snapshot never reached the agent's final state within "
                        f"{self._snapshot_wait_s:g}s",
                        {
                            "conversation_id": conversation_id,
                            "snapshot_wait_s": self._snapshot_wait_s,
                            "snapshot_dir": str(snapshot_dir) if snapshot_dir else None,
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
        # Stamp each PRESENT declared file's acceptance PROOF LEVEL onto its manifest entry so
        # the OutputTruthOracle can proof-gate a content mismatch (Part A): a mismatch on a
        # non-authoritative capture (unproven_extended_stability / unknown) is unreliable and
        # must not be a definitive product failure unless the bytes reached the same stability
        # threshold the readiness gate uses; a proven one (raw_sha / rendered_readback) stays
        # hard. Absent declared files carry no entry (existence is judged separately).
        for p in declared:
            entry = _manifest_lookup(manifest, p)
            if entry is not None:
                entry["proof"] = _proof_level(expected.get(p))
                entry["content_stable"] = (
                    stable_n.get(p, 0) >= _SNAPSHOT_UNPROVEN_STABLE_POLLS
                )
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
            ws = store.path_for(conversation_id).resolve()
        except Exception:  # noqa: BLE001 — any resolution failure ⇒ no snapshot available
            return None
        return ws if ws.is_dir() else None

    async def collect_preview(self, conversation_id: str) -> dict[str, Any]:
        """Capture preview truth: availability + the served ROOT html + its HTTP
        health status. Shape consumed by OutputTruthOracle:
        {"health": {"status": <code>}, "content": <html>, "available": <bool>}.

        Bug 10 (same ephemeral-proxy fragility as Bug 9): post-FINISH the live preview
        proxy 404s because the model's served preview (a backgrounded ``python -m
        http.server``) is torn down when the run ends — false `FALSE_FINISH_PREVIEW_BROKEN`.

        TRUTH HIERARCHY (no forged 200 — every preview-OK carries GENUINE POSITIVE evidence
        the deliverable actually serves the required content):
          1. The LIVE proxy serves (status < 400 AND non-empty) → that IS the truth, used as-is.
          2. Proxy down + a STATIC served-root (``index.html``) is durable in the snapshot →
             the runner SERVES that snapshot dir itself on an OS-assigned FREE high port (never
             a reserved control port) and HTTP-PROBES it, then tears the server down. The REAL
             probe (status + body) is the evidence — independent of whether the agent ran an
             in-run verify. A non-serving / unreadable / wrong-content snapshot → the probe
             genuinely fails / lacks the needle → the preview FAILs (NOT masked). Absence of an
             in-run verify is NOT treated as a pass; the serve+probe supplies the positive proof.
          3. The build's OWN in-run web-app verification ENDED in failure (`_in_run_verify_failed`)
             → believe the agent's broken-verdict; do NOT claim OK even if the static shell serves.
          4. No static served-root (dynamic-only app, or no deliverable) → honest 404
             (FALSE_FINISH_PREVIEW_BROKEN). Live dynamic-app preview verification is the
             documented follow-up — never a forged pass.
        """
        avail_status, avail = await self._t.get_json(
            f"/conversations/{conversation_id}/preview"
        )
        status, text, _hdrs = await self._t.get_text(
            f"/conversations/{conversation_id}/preview-app/"
        )
        live = {
            "health": {"status": status},
            "content": text,
            "available": bool(avail.get("available")) if avail_status < 400 else False,
        }
        # (1) The live preview proxy actually served → that IS the truth.
        if status < 400 and text:
            return live
        # (3) The agent's own in-run verify ended in failure → believe it; no durable claim.
        if self._projects_root is None or self._in_run_verify_failed(conversation_id):
            return live
        # (4) No static served-root → honest 404 (dynamic-only / no deliverable).
        served_index = self._snapshot_served_index(conversation_id)
        if served_index is None:
            return live
        # (2) Serve the snapshot static content ourselves + PROBE it for GENUINE evidence.
        probe = self._serve_probe_snapshot(served_index.parent)
        if probe is None:
            return live  # couldn't serve/probe → no positive evidence → honest 404
        probe_status, probe_body = probe
        return {
            "health": {"status": probe_status},
            "content": probe_body,
            "available": probe_status < 400,
            "source": "snapshot_serve_probe",
        }

    def _serve_probe_snapshot(self, served_dir: Path) -> tuple[int, str] | None:
        """Serve `served_dir` on an OS-assigned FREE loopback port (NEVER a reserved control
        port — port 0 lets the OS pick an ephemeral high port, guarded defensively) and
        HTTP-probe ``GET /`` for GENUINE evidence the static deliverable serves + with what
        content. Returns (status, body) or None if it could not be served/probed. The server
        is ALWAYS torn down (finally). Loopback-only bind (127.0.0.1)."""
        import functools
        import http.server
        import threading
        import urllib.error
        import urllib.request

        served_root = served_dir.resolve()

        def static_index_fallback() -> tuple[int, str] | None:
            """Equivalent GET / evidence when loopback clients are forbidden.

            The caller already selected a static served root. Re-jail the file
            here so this fallback can never follow an index symlink outside it.
            """

            try:
                index = (served_root / "index.html").resolve()
                if not index.is_relative_to(served_root) or not index.is_file():
                    return None
                return 200, index.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            # silence per-request stderr noise; signature matches BaseHTTPRequestHandler
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        handler = functools.partial(_QuietHandler, directory=str(served_dir))
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError:
            return None  # probe unavailable is the HONEST answer — never fabricate a 200
        # From here `httpd` owns a bound socket — it must be closed on EVERY path, including
        # a failure to create/start the serving thread (else the socket/server leaks).
        thread: threading.Thread | None = None
        try:
            port = httpd.server_address[1]
            if port in _RESERVED_CONTROL_PORTS:  # defensive — port 0 won't pick these
                return static_index_fallback()
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    return int(resp.status), body
            except urllib.error.HTTPError as exc:  # a real HTTP error status IS evidence
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    body = ""
                return int(exc.code), body
            except (urllib.error.URLError, OSError, ValueError):
                # Some CI/sandbox profiles disallow loopback client connections even though
                # the static root is already selected and jailed. For GET /, reading that
                # index file is equivalent to SimpleHTTPRequestHandler's success response.
                return static_index_fallback()
        finally:
            # shutdown() only makes sense once serve_forever is actually running; server_close()
            # is ALWAYS safe and is what frees the socket if the thread never started.
            if thread is not None and thread.is_alive():
                httpd.shutdown()
            httpd.server_close()
            if thread is not None:
                thread.join(timeout=2)

    def _snapshot_served_index(self, conversation_id: str) -> Path | None:
        """The durable served-root ``index.html`` Path for a STATIC build: at the snapshot
        workspace root (preferred), else the shallowest ``index.html`` in the tree —
        MIRRORING the product's ``lifecycle._find_snapshot_index`` skip set
        (``.pmx`` / ``.disco`` / ``node_modules``) so an internal ``index.html`` is never
        picked as the served root. Symlink-jailed (resolved path must stay inside the
        workspace). None ⇒ no static served-root (so the caller does NOT claim a preview)."""
        ws = self._snapshot_workspace_dir(conversation_id)
        if ws is None:
            return None
        candidates: list[Path] = []
        root_index = ws / "index.html"
        if root_index.is_file():
            candidates = [root_index]
        else:
            try:
                candidates = sorted(
                    (
                        p
                        for p in ws.rglob("index.html")
                        if p.is_file() and not (_SERVE_SKIP_DIRS & set(p.relative_to(ws).parts))
                    ),
                    key=lambda p: (len(p.relative_to(ws).parts), str(p)),
                )
            except OSError:
                return None
        for cand in candidates:
            try:
                resolved = cand.resolve()
                if resolved.is_relative_to(ws) and resolved.is_file():
                    return resolved
            except OSError:
                continue
        return None

    def _snapshot_served_root(self, conversation_id: str) -> str | None:
        """The served-root ``index.html`` CONTENT (decoded) — thin wrapper over
        :meth:`_snapshot_served_index` (which handles root-preference, the
        ``.pmx``/``.disco``/``node_modules`` skip set, and the symlink jail). None ⇒ no
        static served-root."""
        p = self._snapshot_served_index(conversation_id)
        if p is None:
            return None
        try:
            return p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _in_run_verify_failed(self, conversation_id: str) -> bool:
        """True iff the build's LAST in-run ``verify_web_app`` FAILED — where FAILED means
        ANY of: ``structured.passed`` is False; the tool FAILED TO EXECUTE
        (``tool_result.success`` is False, which produces NO verdict — hole #2); or it ran
        but the verdict/error signals failure. Only a verify that genuinely PASSED (ran
        successfully AND ``passed`` truthy) leaves this False, so the durable-preview
        substitution never fires off a verify that errored or failed. No verify at all ⇒
        False (a static deliverable that finished with no serve-check is not a PROVEN
        failure; the served-root presence + content truth still gate the substitution)."""
        last: dict[str, Any] | None = None
        for e in self._read_events(conversation_id):
            if e.get("kind") != "observation":
                continue
            tr = _payload(e).get("tool_result") or {}
            if tr.get("tool_name") == "verify_web_app":
                last = tr
        if last is None:
            return False
        if not last.get("success", True):
            return True  # verifier EXECUTION failure — no verdict produced (hole #2)
        raw_structured = last.get("structured")
        structured: dict[str, Any] = raw_structured if isinstance(raw_structured, dict) else {}
        if structured.get("passed") is False:
            return True
        if str(structured.get("verdict", "")).lower() in {"fail", "failed", "error", "broken"}:
            return True
        return bool(structured.get("error"))

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
    """Canonical workspace-relative key: drop a leading ``./`` then any leading ``/`` so a
    scenario-declared ``/index.html`` / ``./index.html`` and an event-log ``index.html``
    compare equal."""
    s = str(path)
    if s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


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


def _has_shell_meta(tokens: list[str]) -> bool:
    return any(
        tok in _SHELL_META_TOKENS
        or any(marker in tok for marker in _SHELL_META_SUBSTRINGS)
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
    err_call_ids: set[str] = set()
    for e in events:
        kind = e.get("kind")
        if kind == "observation":
            tr = _payload(e).get("tool_result") or {}
            cid = tr.get("call_id")
            if cid is not None:
                obs_success[str(cid)] = bool(tr.get("success", True))
                obs_content[str(cid)] = tr.get("content")
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
            np = _norm_rel(str(args.get("path", "")))
            if np not in want:
                continue
            content = args.get("content")
            if isinstance(content, str) and not _ELISION_MARKER_RE.search(content):
                sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                last_mut[np] = (seq, "sha", sha)
            else:  # elided/unknown bytes → a content-unprovable mutation
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
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
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self.base_url}{path}", json=body, headers=self._headers(unsafe=True)
            )
            return r.status_code, _safe_json(r)

    async def get_json(self, path: str) -> tuple[int, dict[str, Any]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, _safe_json(r)

    async def get_text(self, path: str) -> tuple[int, str, dict[str, str]]:
        import httpx

        await self._ensure_session()
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            r = await client.get(f"{self.base_url}{path}", headers=self._headers(unsafe=False))
            return r.status_code, r.text, dict(r.headers)

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

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(f"{self.base_url}/health")
            return r.status_code, _safe_json(r)


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return {}
    return data if isinstance(data, dict) else {"_": data}
