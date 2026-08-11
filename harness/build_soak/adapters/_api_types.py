"""Bounded contract types, constants, and seal policy.

These definitions sit behind the ``disco_api`` compatibility facade.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx  # the adapter MAY import an http client (oracle path stays disco/http-free)

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
_HOST_VERIFY_DELIVERABLE_TIMEOUT_S = 300.0
_TOOL_TIMEOUT_OVERRIDES_S = {
    "slides_generate": _SLIDES_GENERATE_TIMEOUT_S,
    "host.verify_deliverable": _HOST_VERIFY_DELIVERABLE_TIMEOUT_S,
}
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


class FinishUnsealableContentError(Exception):
    """The run reached FINISHED and the PRODUCT itself disclosed — typed
    (`meta.persistence_failure.kind == "seal_incomplete_content"`), on the durable
    log — that the strict final seal refused on DETERMINISTIC content (symlinks,
    hardlinked/non-regular entries, oversized files). This is a PRODUCT outcome,
    not an evidence gap: no amount of waiting turns the refused version into a
    valid one — only the product repairing and RE-finishing can, which is why
    the adapter still observes the full snapshot-wait window before classifying
    (a later valid finish seal supersedes the refusal) and only raises this at
    the deadline. A §17 re-run would launder a product defect into an invisible
    retry (F-27, counted trial 590005: WORKSPACE_SNAPSHOT_NOT_READY conflated
    seal-REFUSED-on-content with snapshot lag). The runner records
    FAIL / FINISH_UNSEALABLE_CONTENT — never INVALID_RUN. Adjudicated on the
    product-owned typed meta, never by parsing the English (the
    CONDENSATION_SUMMARY_UNUSABLE precedent). `facts` carries the product's own
    blocking list plus the seal evidence for the dossier."""

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}


# The product-owned typed disclosure kind this adapter splits on. Pinned equal
# to disco.agent_server.workspace_persistence.SEAL_INCOMPLETE_CONTENT_KIND by
# a test — a rename on either side must fail loudly, never silently revert
# F-27 adjudication to rerunnable snapshot lag.
SEAL_INCOMPLETE_CONTENT_KIND = "seal_incomplete_content"


def _exact_seq(event: dict[str, Any]) -> int | None:
    """The event's positive int seq, or None (bool/str/absent never count)."""

    raw = event.get("seq")
    return raw if type(raw) is int and raw > 0 else None


def _valid_workspace_version(event: dict[str, Any]) -> bool:
    """Single shape validator for a workspace_version event the harness will
    treat as evidence: system-sourced, exact positive seq, positive int
    version_seq, sha256 tree_digest, string trigger."""

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


def _latest_finish_version_seq(events: list[dict[str, Any]]) -> int:
    latest = -1
    for event in events:
        if (
            event.get("kind") == "workspace_version"
            and _valid_workspace_version(event)
            and event.get("trigger") == "finish"
        ):
            seq = _exact_seq(event)
            if seq is not None:
                latest = max(latest, seq)
    return latest


def _refusal_blocking(event: dict[str, Any]) -> list[str] | None:
    meta = event.get("meta")
    if not isinstance(meta, dict):
        return None
    failure = meta.get("persistence_failure")
    if not (isinstance(failure, dict) and failure.get("kind") == SEAL_INCOMPLETE_CONTENT_KIND):
        return None
    raw = failure.get("blocking")
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _typed_content_seal_refusal(events: list[dict[str, Any]]) -> list[str] | None:
    """The product's typed content-seal-refusal disclosure, if it governs.

    Returns the product-disclosed blocking entries when a
    ``seal_incomplete_content`` persistence disclosure exists AFTER the latest
    FINISH-triggered workspace_version event (a later successful finish seal
    supersedes an older refusal — a run that repaired and re-finished is judged
    on its real seal).  Only the finish seal holds that authority: a
    recovery/pause version cut after the refusal must not launder the typed
    content refusal back into rerunnable snapshot lag. The superseding version
    must pass ``_valid_workspace_version`` — a malformed version event has no
    authority over a typed refusal — and only exact-seq messages participate
    in the scan (the harness never adjudicates on events it cannot order).
    Returns None otherwise.
    """

    latest_version_seq = _latest_finish_version_seq(events)
    for event in reversed(events):
        if event.get("kind") != "message":
            continue
        seq = _exact_seq(event)
        if seq is None:
            continue
        if seq <= latest_version_seq:
            break
        blocking = _refusal_blocking(event)
        if blocking is not None:
            return blocking
    return None


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


@dataclass(frozen=True)
class _FinalSealBoundary:
    terminal_seq: int
    latest_effect_seq: int | None
    event_seq: int
    marker: dict[str, Any]


@dataclass(frozen=True)
class _EffectFence:
    latest_effect_seq: int | None


def _boundary_error(
    error: str,
    *,
    terminal_seq: int | None = None,
    latest_effect_seq: int | None = None,
    event_seq: int | None = None,
    version_seq: int | None = None,
    tree_digest: str | None = None,
) -> _FinalWorkspaceSealEvidence:
    return _FinalWorkspaceSealEvidence(
        terminal_seq=terminal_seq,
        latest_effect_seq=latest_effect_seq,
        event_seq=event_seq,
        version_seq=version_seq,
        tree_digest=tree_digest,
        error=error,
    )


def _semantic_terminal(
    events: list[dict[str, Any]],
) -> int | _FinalWorkspaceSealEvidence:
    semantic = [
        event
        for event in events
        if event.get("kind") in {"status", "workspace_version", *_FINAL_SEAL_EFFECT_KINDS}
    ]
    if any(_exact_seq(event) is None for event in semantic):
        return _boundary_error("semantic event has no canonical sequence")
    statuses = [event for event in events if event.get("kind") == "status"]
    if not statuses:
        return _boundary_error("event log has no status event")
    latest_status = max(statuses, key=lambda event: _exact_seq(event) or -1)
    terminal_seq = _exact_seq(latest_status)
    if (
        terminal_seq is None
        or latest_status.get("source") != "system"
        or latest_status.get("status") != "FINISHED"
    ):
        return _boundary_error(_NO_SYSTEM_FINISHED_TERMINAL)
    return terminal_seq


def _effect_fence(
    events: list[dict[str, Any]], terminal_seq: int
) -> _EffectFence | _FinalWorkspaceSealEvidence:
    effects = [event for event in events if event.get("kind") in _FINAL_SEAL_EFFECT_KINDS]
    latest_raw = max((_exact_seq(event) or -1 for event in effects), default=-1)
    latest_effect_seq = latest_raw if latest_raw > 0 else None
    if latest_effect_seq is not None and latest_effect_seq >= terminal_seq:
        return _boundary_error(
            "effect event exists at or after terminal FINISHED",
            terminal_seq=terminal_seq,
            latest_effect_seq=latest_effect_seq,
        )
    return _EffectFence(latest_effect_seq)


def _workspace_version_boundary(
    events: list[dict[str, Any]],
    terminal_seq: int,
    latest_effect_seq: int | None,
) -> _FinalSealBoundary | _FinalWorkspaceSealEvidence:
    versions = [event for event in events if event.get("kind") == "workspace_version"]
    if not versions:
        return _boundary_error(
            "event log has no workspace version marker",
            terminal_seq=terminal_seq,
            latest_effect_seq=latest_effect_seq,
        )
    marker = max(versions, key=lambda event: _exact_seq(event) or -1)
    event_seq = _exact_seq(marker)
    if event_seq is None or marker.get("source") != "system":
        return _boundary_error(
            "latest workspace version is not a canonical system event",
            terminal_seq=terminal_seq,
            latest_effect_seq=latest_effect_seq,
        )
    if event_seq <= terminal_seq:
        return _boundary_error(
            "workspace version does not follow terminal FINISHED",
            terminal_seq=terminal_seq,
            latest_effect_seq=latest_effect_seq,
            event_seq=event_seq,
        )
    if marker.get("trigger") != "finish":
        return _boundary_error(
            "workspace version is not finish-triggered",
            terminal_seq=terminal_seq,
            latest_effect_seq=latest_effect_seq,
            event_seq=event_seq,
        )
    return _FinalSealBoundary(terminal_seq, latest_effect_seq, event_seq, marker)


def _final_seal_boundary(
    events: list[dict[str, Any]],
) -> _FinalSealBoundary | _FinalWorkspaceSealEvidence:
    terminal = _semantic_terminal(events)
    if isinstance(terminal, _FinalWorkspaceSealEvidence):
        return terminal
    effect = _effect_fence(events, terminal)
    if isinstance(effect, _FinalWorkspaceSealEvidence):
        return effect
    return _workspace_version_boundary(events, terminal, effect.latest_effect_seq)


def _seal_effect_matches(seal: dict[str, Any], latest_effect_seq: int | None) -> bool:
    observed = seal.get("latest_effect_seq")
    if "latest_effect_seq" not in seal:
        return False
    if latest_effect_seq is None:
        return observed is None
    return type(observed) is int and observed == latest_effect_seq


def _final_seal_field_error(
    seal: dict[str, Any],
    *,
    terminal_seq: int,
    latest_effect_seq: int | None,
    version_seq: int,
    tree_digest: str,
) -> str | None:
    if type(seal.get("schema_version")) is not int or seal.get("schema_version") != 1:
        return "final seal schema version is not 1"
    if type(seal.get("terminal_seq")) is not int or seal.get("terminal_seq") != terminal_seq:
        return "final seal terminal sequence does not match the event log"
    if not _seal_effect_matches(seal, latest_effect_seq):
        return "final seal latest effect sequence does not match the event log"
    if type(seal.get("version_seq")) is not int or seal.get("version_seq") != version_seq:
        return "final seal version sequence does not match its event"
    if not isinstance(seal.get("tree_digest"), str) or seal.get("tree_digest") != tree_digest:
        return "final seal tree digest does not match its event"
    file_count = seal.get("file_count")
    if type(file_count) is not int or file_count < 0:
        return "final seal file count is invalid"
    total_bytes = seal.get("total_bytes")
    if type(total_bytes) is not int or total_bytes < 0:
        return "final seal total bytes is invalid"
    return None


def _validated_final_seal(
    boundary: _FinalSealBoundary, conversation_id: str
) -> _FinalWorkspaceSealEvidence:
    marker = boundary.marker
    version_seq = marker.get("version_seq")
    tree_digest = marker.get("tree_digest")
    fields = {
        "terminal_seq": boundary.terminal_seq,
        "latest_effect_seq": boundary.latest_effect_seq,
        "event_seq": boundary.event_seq,
    }
    if type(version_seq) is not int or version_seq < 1:
        return _boundary_error("workspace version has an invalid version sequence", **fields)
    if not isinstance(tree_digest, str) or _STRICT_SHA256_RE.fullmatch(tree_digest) is None:
        return _boundary_error(
            "workspace version has an invalid tree digest",
            version_seq=version_seq,
            **fields,
        )
    seal = marker.get("final_seal")
    if not isinstance(seal, dict):
        return _boundary_error(
            "latest workspace version has no final seal",
            version_seq=version_seq,
            tree_digest=tree_digest,
            **fields,
        )
    if set(seal) != _FINAL_SEAL_KEYS:
        return _boundary_error(
            "final seal fields do not match schema v1",
            version_seq=version_seq,
            tree_digest=tree_digest,
            **fields,
        )
    scope = seal.get("scope")
    if not isinstance(scope, dict) or set(scope) != _FINAL_SEAL_SCOPE_KEYS:
        error = "final seal scope is malformed"
    elif scope.get("namespace") != "workspace.tree" or scope.get("identifier") != conversation_id:
        error = "final seal scope does not match the conversation workspace"
    else:
        error = _final_seal_field_error(
            seal,
            terminal_seq=boundary.terminal_seq,
            latest_effect_seq=boundary.latest_effect_seq,
            version_seq=version_seq,
            tree_digest=tree_digest,
        )
    return _FinalWorkspaceSealEvidence(
        **fields,
        version_seq=version_seq,
        tree_digest=tree_digest,
        file_count=seal.get("file_count") if type(seal.get("file_count")) is int else None,
        total_bytes=seal.get("total_bytes") if type(seal.get("total_bytes")) is int else None,
        error=error,
    )


def _strict_final_workspace_seal(
    events: list[dict[str, Any]], conversation_id: str
) -> _FinalWorkspaceSealEvidence:
    """Validate the frozen live lane's non-bypass final-seal contract.

    Historical unsealed workspace-version events may remain in the log for schema
    compatibility, but the *latest* marker must carry a complete schema-v1 seal
    bound to the latest exact SYSTEM/FINISHED status and every effect fence.
    """

    boundary = _final_seal_boundary(events)
    if isinstance(boundary, _FinalWorkspaceSealEvidence):
        return boundary
    return _validated_final_seal(boundary, conversation_id)


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
