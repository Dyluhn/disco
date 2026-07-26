"""View-rendering free functions for the agent loop.

Extracted from engine.py (the AgentLoop god-class). These are render-time
projections — the live workspace snapshot, the F8 arg-shrink transform, and the
router overflow signal. They carry no loop state: the snapshot takes the sandbox
explicitly, the others are pure over their inputs. Bodies are byte-identical to
the former AgentLoop methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import TYPE_CHECKING

from ..context import (
    ArtifactMemoryStore,
    CompactionPolicy,
    ContextLedger,
    VerifierFailureRef,
    context_compact_if_needed,
)
from ..effects import ObservationReceipt
from ..events import (
    WORKSPACE_SNAPSHOT_SENTINEL,
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    obs_snip_override,
    retarget_elided_arg_markers,
)
from ..inspect import inspect_enabled
from ..llm import Difficulty, OperatingMode, OverflowSignal
from ..obs import log_event
from ..view import View, microcompact
from . import signals
from .context_budget import ContextCaps, derive_context_caps
from .context_builder import build_context_pack, render_context_pack
from .context_live import context_pack_enabled, protected_context_compaction_seqs
from .dedup import (
    _F8_PREFIX_CHARS,
    _F8_TRUNCATION_MARKER_TEMPLATE,
    _f8_confirmed_file_writes,
    collapse_superseded_reads,
)
from .file_state import FileStateTracker, file_state_notice
from .messages import _workspace_paths_from_events
from .observe import _ground_read
from .resource_context import (
    current_prompt_coverage_lines,
    snapshot_receipt,
    snapshot_window_receipt,
)

if TYPE_CHECKING:
    from .boundaries import Sandbox
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

_PLAN_EXECUTION_RECEIPT_MARKER = "PLAN PHASE RECEIPT"
_PLAN_PLANNING_RECEIPT_MARKER = "PLANNING PHASE RECEIPT"
_INVALID_PLAN_FEEDBACK = "invalid_plan_done_conditions"
_PLAN_VERIFIER_FAILED = "plan_verifier_failed"
_PLAN_VERIFIER_REPLAN_REQUIRED = "plan_verifier_replan_required"
_RETIRED_PLAN_FEEDBACK_PREFIX = "[[DISCO-RETIRED-PLAN-FEEDBACK:"


def _plan_planning_receipt(events: list[Event], *, planning_active: bool) -> LLMMessage | None:
    """Project current revision-planning truth after stale historical context."""

    if not planning_active:
        return None
    repair = signals.verifier_repair_planning_active(events)
    approved = signals.latest_approved_plan(events)
    prior_revision = approved.revision if approved is not None else 0
    next_revision = signals.next_plan_revision(events)
    if repair:
        purpose = (
            "The current plan-owned verifier failed twice. The delivered workspace "
            "may already be correct; external acceptance is unchanged. Submit a "
            "corrected plan/verifier for approval and do not redo product edits while "
            "this repair is still in PLANNING."
        )
    elif approved is None:
        instruction = (signals.latest_user_text(events) or "the user's request").strip()
        if len(instruction) > 320:
            instruction = instruction[:317].rstrip() + "..."
        purpose = (
            f"No plan is approved yet. Revision {next_revision} is the initial plan for "
            f"the request {instruction!r}. Read only what you need, then call "
            "`submit_plan` for approval."
        )
    else:
        instruction = (
            signals.current_revision_instruction(events) or "the latest user change"
        ).strip()
        if len(instruction) > 320:
            instruction = instruction[:317].rstrip() + "..."
        purpose = (
            f"A new user instruction is awaiting revision {next_revision}: {instruction!r}. "
            "Read only what you need, then call `submit_plan` for that revision."
        )
    return LLMMessage(
        role="user",
        content=(
            "<system-reminder>\n"
            f"{_PLAN_PLANNING_RECEIPT_MARKER} (authoritative; derived from durable "
            "phase events):\n"
            f"CURRENT PHASE: PLANNING for revision {next_revision}. Revision "
            f"{prior_revision} is only the previously approved baseline; it is NOT "
            "approval for the pending revision. Workspace mutation and execution "
            f"tools remain unavailable until the new plan is approved. {purpose}\n"
            "</system-reminder>"
        ),
    )


def _plan_execution_receipt(events: list[Event]) -> LLMMessage | None:
    """Project the durable approval edge into the first execution view.

    ``plan_approved`` is intentionally a non-LLM StatusEvent.  Without this
    projection, the model's last visible instruction can remain "your plan was
    NOT accepted" even after the user approved its corrected replacement.  The
    mode/tool router knows execution started, while the model still believes it
    is planning.  Deriving the receipt from the persisted approval transition
    keeps those two views of phase truth identical across restart and
    condensation without adding a second best-effort event.

    The receipt remains present until the first execution ActionEvent.  After
    that, the action/result pair itself proves that the phase handoff was
    consumed.  An out-of-phase ``submit_plan`` gets the same guidance from the
    dispatch guard, so that recovery does not depend on this projection
    lingering forever.
    """

    plan = signals.latest_approved_plan(events)
    if plan is None or signals.in_planning_for_revision(events):
        return None
    approval = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        None,
    )
    if approval is None:
        return None
    approval_seq = approval.seq or 0
    if any(
        isinstance(event, StatusEvent)
        and event.detail == "plan_revision_idempotent"
        and (event.seq or 0) > approval_seq
        for event in events
    ):
        # The paired idempotent-redirect MessageEvent is already the current
        # execution handoff. Do not project a second copy from the older approval.
        return None
    # A newer proposal is awaiting its own decision; do not describe the older
    # approval as the active handoff if a caller accidentally materializes a
    # view while that gate is parked.
    if any(isinstance(event, PlanEvent) and (event.seq or 0) > approval_seq for event in events):
        return None
    verifier_repair = signals.verifier_repair_execution_active(events)
    if not verifier_repair and any(
        isinstance(event, ActionEvent) and (event.seq or 0) > approval_seq for event in events
    ):
        return None

    next_step = plan.steps[0].title.strip() if plan.steps else "the approved work"
    if verifier_repair:
        guidance = (
            "This approval is a VERIFIER-ONLY correction after successful workspace "
            "work. The prior delivered bytes remain valid; do not redo product edits "
            "unless fresh evidence shows they are wrong. Call `finish` now so Disco "
            "freshly re-runs external acceptance, the corrected plan predicates, and "
            "the normal host/browser gates. Read-only inspection is allowed if needed."
        )
    else:
        guidance = (
            "Any earlier invalid-plan or correct-and-resubmit instruction for this "
            "revision is superseded. Do not call `submit_plan` or submit this approved "
            "plan again. Continue with execution tools now; the next step is: "
            f"{next_step}. Use `propose_plan_update` only if new user input or "
            "execution evidence shows that the approved plan itself must change."
        )
    return LLMMessage(
        role="user",
        content=(
            "<system-reminder>\n"
            f"{_PLAN_EXECUTION_RECEIPT_MARKER} (authoritative; derived from the "
            "durable approval record):\n"
            f"Plan revision {plan.revision} is APPROVED. You are now in EXECUTION "
            f"mode. {guidance}\n</system-reminder>"
        ),
    )


def _retire_superseded_invalid_plan_feedback(
    events: list[Event],
) -> tuple[list[Event], frozenset[str]]:
    """Retire the exact tagged rejection events a later approval resolved.

    This is render-only.  Audit history stays byte-for-byte intact, but obsolete
    blocking instructions cannot compete with the approved plan in a restarted
    or re-condensed model view.  Each exact tagged event is replaced by a unique
    render sentinel before ``View.of``; an ordinary user message with identical
    prose is therefore never removed by content coincidence.  The replacement
    retains the original id/seq/meta so condensation placement and view-authority
    projection remain unchanged.
    """

    if signals.latest_approved_plan(events) is None:
        return events, frozenset()
    superseded_failures = signals.superseded_plan_owned_failure(events)
    approval_seq = max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent) and event.detail == "plan_approved"
        ),
        default=0,
    )
    projected: list[Event] = []
    sentinels: set[str] = set()
    for event in events:
        blocking = event.meta.get("blocking") if isinstance(event, MessageEvent) else None
        retire_invalid = blocking == _INVALID_PLAN_FEEDBACK and (event.seq or 0) < approval_seq
        retire_plan_failure = (
            blocking in {_PLAN_VERIFIER_FAILED, _PLAN_VERIFIER_REPLAN_REQUIRED}
            and event.id in superseded_failures
        )
        if (
            isinstance(event, MessageEvent)
            and event.source is EventSource.ENVIRONMENT
            and (retire_invalid or retire_plan_failure)
        ):
            sentinel = f"{_RETIRED_PLAN_FEEDBACK_PREFIX}{event.id}]]"
            sentinels.add(sentinel)
            projected.append(
                event.model_copy(
                    update={"message": event.message.model_copy(update={"content": sentinel})}
                )
            )
            continue
        projected.append(event)
    return projected, frozenset(sentinels)


# CW-2 — the assist-ON baseline snapshot caps (today's weak-model calibration).
# These are the byte-identical fallback when no derived caps are threaded in (the
# engine's back-compat delegator + legacy tests). assist-OFF caps scale with the
# live context window via context_budget.derive_context_caps.
_WS_MAX_FILES = 8  # cap the snapshot breadth (most-recently-touched first)
_WS_PER_FILE_CHARS = 6_000  # per-file cap; larger files head/tail-truncate with a marker
_WS_TOTAL_CHARS = 16_000  # total snapshot budget (~4k tokens), bounded vs the condenser
# The assist-ON baseline as a ContextCaps bundle (the default when caps is None).
_BASELINE_CAPS = ContextCaps(
    max_files=_WS_MAX_FILES,
    per_file_chars=_WS_PER_FILE_CHARS,
    total_chars=_WS_TOTAL_CHARS,
    read_char_budget=7_000,
    obs_snip_chars=8_000,
)
_WS_READ_TIMEOUT_S = 2.0  # per-file read cap — the snapshot runs under the conversation
#                           lock, so a hung sandbox read must never freeze control ops


def overflow_signal(events: list[Event]) -> OverflowSignal:
    """Derive the router's overflow inputs from the log: trailing tool errors
    feed router rule 4 (stuck recovery) so a struggling step can auto-escalate
    to overflow *before* STUCK is declared (§6 note)."""
    consecutive = 0
    for e in reversed(events):
        if isinstance(e, AgentErrorEvent):
            consecutive += 1
        elif isinstance(e, ActionEvent | ObservationEvent | MessageEvent):
            break
    return OverflowSignal(difficulty=Difficulty.ROUTINE, consecutive_tool_errors=consecutive)


def _drop_context_pack_owned_history(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Drop raw PlanEvent renders when the ContextPack owns goal/version/todo."""
    return [
        msg
        for msg in messages
        if not (msg.role == "assistant" and msg.content.startswith("Plan (revision "))
    ]


class _SnapshotHalt:
    """Typed sentinel: a hung read ⇒ wedged sandbox → stop the snapshot.
    A distinct type (not bare ``object()``) so the caller's isinstance check
    narrows the read result cleanly to ``bytes | None``."""


_SNAPSHOT_READ_HALT = _SnapshotHalt()


async def _read_working_file(
    sbx: Sandbox, path: str, omitted_notes: list[str]
) -> bytes | _SnapshotHalt | None:
    """Read one working-set file's raw bytes for the snapshot. Returns the
    bytes on success; ``None`` to SKIP this file (gone / unreadable / directory
    / binary — an explanatory note is appended to ``omitted_notes``); or
    ``_SNAPSHOT_READ_HALT`` to STOP the snapshot (a hung read implies a wedged/
    dead sandbox — don't hold the conversation lock for timeout×N files, which
    would block pause/steer/cancel). Raises nothing. Extracted from
    workspace_snapshot_message to keep that coordinator under the size cap."""
    try:
        raw = await asyncio.wait_for(sbx.read_file(path), timeout=_WS_READ_TIMEOUT_S)
    except TimeoutError:
        # Degrade to the snapshot collected so far. The sandbox isn't healed
        # here (a pure projection step); the model's NEXT real action goes
        # through _execute_and_observe, which detects the dead sandbox.
        return _SNAPSHOT_READ_HALT
    except FileNotFoundError:
        # E4 (T8) — deleted between the action and this read. Surface it (the
        # model can re-decide whether to re-create); don't silently lose it.
        omitted_notes.append(f"[file gone: {path}]")
        return None
    except PermissionError:
        # E4 (T8) — sandbox denied the read. Note it so the model knows the path
        # is in its working set but the snapshot can't show it.
        omitted_notes.append(f"[unreadable: permission: {path}]")
        return None
    except (IsADirectoryError, NotADirectoryError, OSError) as e:
        # E4 (T8) — a directory slipped into the working set. Note + skip; any
        # other OSError keeps the prior silent-skip contract.
        if isinstance(e, IsADirectoryError) or (
            isinstance(e, OSError) and getattr(e, "errno", None) == 21  # EISDIR
        ):
            omitted_notes.append(f"[directory: {path}]")
        return None
    except Exception:  # noqa: BLE001 — file gone/unreadable this turn: skip it
        return None
    # E4 (T8) — binary sniff. A NUL byte in the first 1KB is a near-certain
    # binary signal; the snapshot's only value is content the model can act on.
    # Bounded sample so a huge text file with one stray NUL past the head still
    # renders via errors="replace".
    if b"\x00" in raw[:1024]:
        omitted_notes.append(f"[binary omitted: {path}]")
        return None
    return raw


def _workspace_snapshot_preamble(pin_full: bool) -> str:
    """The CURRENT WORKSPACE block preamble. Per-tier wording (CW-3 / CW P1-c):
    ``pin_full`` (assist OFF) moves the block to the cacheable PREFIX, so it names
    ITSELF ("this prompt") instead of a directional "above/below"; the assist-ON
    tail-placement keeps the original directional wording byte-for-byte."""
    if pin_full:
        return (
            f"{WORKSPACE_SNAPSHOT_SENTINEL} — your files on disk RIGHT NOW (authoritative).\n"
            "This block contains the live, exact content of the files you are working "
            "on, re-read from disk this turn. It OVERRIDES any other copy of these "
            "files shown elsewhere in this prompt; trust THIS over your memory.\n"
            "To change a file: for a SMALL change, prefer `file_edit` (pass the exact "
            "text you see as `old`) or `file_replace_lines` / `file_insert_lines` (use "
            "the line numbers shown for each file in this block). For a full rewrite, "
            "use `file_write` with the FULL new content — but you MUST call `file_read` "
            "on this file first if you have written to it before, or the write will be "
            "refused. Keep every existing function, constant, and docstring you are not "
            "deliberately removing — do not drop code you did not mean to delete.\n"
            "**SILENT CONTEXT** — use this block without narrating it. Do NOT "
            "acknowledge the snapshot in your reply (no 'I can see the files', "
            "'the workspace shows…', 'good, the content is here', etc.). "
            "Just continue the work.\n\n"
        )
    return (
        f"{WORKSPACE_SNAPSHOT_SENTINEL} — your files on disk RIGHT NOW (authoritative).\n"
        "Below is the live, exact content of the files you are working on, "
        "re-read from disk this turn. It OVERRIDES any earlier or elided copy of "
        "these files shown above; trust THIS over your memory.\n"
        "To change a file: for a SMALL change, prefer `file_edit` (pass the exact "
        "text you see as `old`) or `file_replace_lines` / `file_insert_lines` (use "
        "the line numbers shown below). For a full rewrite, use `file_write` with "
        "the FULL new content — but you MUST call `file_read` on this file first "
        "if you have written to it before, or the write will be refused. Keep "
        "every existing function, constant, and docstring you are not deliberately "
        "removing — do not drop code you did not mean to delete.\n"
        "**SILENT CONTEXT** — use this block without narrating it. Do NOT "
        "acknowledge the snapshot in your reply (no 'I can see the files', "
        "'the workspace shows…', 'good, the content is here', etc.). "
        "Just continue the work.\n\n"
    )


async def workspace_snapshot_message(
    sbx: Sandbox | None,
    events: list[Event],
    *,
    tracker: FileStateTracker | None = None,
    stale: frozenset[str] | None = None,
    caps: ContextCaps | None = None,
    pin_full: bool = False,
    out_pinned_full: set[str] | None = None,
    out_prompt_receipts: list[ObservationReceipt] | None = None,
) -> LLMMessage | None:
    """Re-derive the CURRENT on-disk content of the working-set files from the
    sandbox each turn and render it as an authoritative, always-fresh message.

    WHY (root cause, source-verified + reproduced live): a file_write's body is
    elided at render time to a "[[DISCO-ELIDED: N chars ...]]" sentinel (events._snip_args,
    >1.5k chars), and observation-masking + condensation can later erase a
    file_read's output too. A weak driver that doesn't proactively file_read
    every turn therefore loses sight of what's on disk and regenerates files
    from lossy memory — clobbering prior content (reproduced: a 4,441-char file
    rewritten to 202 chars, all constants + fingerprint gone).

    The OSS-proven fix (Aider's ChatChunks.chat_files re-read via io.read_text
    each turn; OpenHands str_replace operating on current on-disk text) is to
    re-derive file content from the SOURCE OF TRUTH (disk) every turn and keep
    it OUT of the condensable history. This message is rebuilt here, post-
    projection, so View.of's snip/mask/condense never touch it and the
    condenser (which acts on EVENTS) can never erase it. Returns None if there
    is no sandbox or no tracked files — degrading to prior behavior.

    Every provider request is stateless. A file shown on a prior turn is therefore
    never replaced by a "shown earlier" pointer. ``tracker`` and ``stale`` still
    support external-change notices, while every request independently carries the
    current bounded body. Full, untruncated, byte-exact UTF-8 bodies also emit typed
    receipts through ``out_prompt_receipts``; those receipts are the sole authority
    for compacting an equivalent historical read.

    Layering-safe: reaches the sandbox via the same duck-typed
    getattr(self.executor, "sandbox", None) seam as _execute_and_observe."""
    if sbx is None:
        return None
    # CW-2: assist-OFF derives larger caps from the live window; the default
    # (caps is None) is the byte-identical assist-ON baseline used by the engine's
    # back-compat delegator and legacy tests.
    _caps = caps if caps is not None else _BASELINE_CAPS
    # Keep the stale argument as an API-compatible input. Staleness affects the
    # separate warning message, not whether this stateless request receives bytes.
    _ = stale
    mutated, read_only = _workspace_paths_from_events(events)
    ordered = mutated + read_only  # mutated first → never evicted by reads (#1)
    if not ordered:
        return None
    # CW-3 / CW P1-c — PER-TIER preamble wording, gated on `pin_full` (which is set
    # iff assist is OFF). assist-OFF moves the block to the cacheable PREFIX (before
    # the event history), so any directional reference ("below"/"above") to the
    # history is WRONG → the block names ITSELF ("the CURRENT WORKSPACE block in this
    # prompt"). assist-ON keeps the block in the TAIL (after the history), so the
    # ORIGINAL pre-CW-3 directional wording is CORRECT — and restoring it byte-for-byte
    # keeps the assist-ON rendered prompt byte-identical to pre-CW-3 (P1-c).
    preamble = _workspace_snapshot_preamble(pin_full)
    blocks: list[str] = []
    prompt_receipts: list[ObservationReceipt] = []
    # E4 (T8) — per-file notes appended to the trailing omitted-notice.
    # Each note tells the model exactly WHY a file it touched is NOT
    # rendered as a BEGIN/END block (binary, deleted, permission). The
    # model can then `file_read` it itself when it needs the content.
    omitted_notes: list[str] = []
    budget_omitted: list[str] = []
    budget = _caps.total_chars - len(preamble)
    shown_count = 0
    # E4 (T8) — .disco-spill-* are T10's overflow logs (head/tail markers
    # of an oversize stdout), not deliverables. They pollute the working
    # set with multi-MB noise and dwarf every real file. Skip them
    # outright: the model has no business re-reading its own spill log.
    # Match the leaf filename (basename) so an absolute path the model
    # might use (e.g. /workspace/.disco-spill-abc.log) is caught too.
    spill_basename_prefix = ".disco-spill-"
    candidates = [
        path
        for path in ordered
        if not os.path.basename(path).startswith(spill_basename_prefix)
        and ".pmx" not in path.split("/")
    ]
    for index, path in enumerate(candidates):
        if shown_count >= _caps.max_files or budget <= 0:
            budget_omitted.extend(candidates[index:])
            break
        raw = await _read_working_file(sbx, path, omitted_notes)
        if isinstance(raw, _SnapshotHalt):
            budget_omitted.extend(candidates[index:])
            break  # hung read ⇒ wedged sandbox; degrade to the snapshot so far
        if raw is None:
            continue  # gone / unreadable / directory / binary — note already added
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            omitted_notes.append(
                f"[invalid UTF-8 text omitted without lossy replacement: {path}; "
                "the exact bytes remain on disk]"
            )
            continue
        # Full body: file is stale, never-shown-in-tracker, or tracker is None
        # (backward-compat mode). Render head + tail when oversize.
        cap = min(_caps.per_file_chars, budget)
        if len(text) > cap:
            head = cap * 3 // 4
            tail = cap - head
            shown = (
                f"{text[:head]}\n"
                f"… [{len(text) - head - tail:,} more chars — this file is large. "
                "To see a region: file_read(path, offset=L, limit=M). "
                "To change it: file_edit with a content anchor, or "
                "file_replace_lines on a freshly-read range.] …\n"
                f"{text[-tail:]}"
            )
            head_bytes = len(text[:head].encode("utf-8"))
            tail_bytes = len(text[-tail:].encode("utf-8")) if tail else 0
            byte_spans = tuple(
                span
                for span in (
                    (0, head_bytes) if head_bytes else None,
                    (len(raw) - tail_bytes, len(raw)) if tail_bytes else None,
                )
                if span is not None
            )
            prompt_receipts.append(
                snapshot_window_receipt(
                    path=path,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    raw_size_bytes=len(raw),
                    byte_spans=byte_spans,
                    rendered_content=shown,
                )
            )
        else:
            shown = text
            # Only an untruncated, losslessly decoded body proves the disk revision
            # byte-for-byte. Replacement-decoded text remains useful context, but it
            # cannot authorize compaction or satisfy read grounding as "exact".
            if out_pinned_full is not None:
                out_pinned_full.add(path)
            prompt_receipts.append(
                snapshot_receipt(
                    path=path,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    total_lines=len(text.splitlines()),
                    raw_size_bytes=len(raw),
                    rendered_content=shown,
                )
            )
        # Plain text delimiters — NOT markdown ``` fences. A fenced snapshot
        # invites the model to reflexively copy the ``` into its file_write
        # (models are trained to wrap code in ```), polluting the real file and
        # then thrashing to strip it (caught live on gpt-oss-120b). file_read
        # returns raw content with no fences; the snapshot matches that.
        block = (
            f"----- BEGIN FILE {path} ({len(text):,} chars on disk) -----\n"
            f"{shown}\n"
            f"----- END FILE {path} -----"
        )
        budget -= len(block)  # charge the WHOLE block incl header/markers (#6)
        blocks.append(block)
        shown_count += 1
        # W2 — update tracker after showing full body (seq=0: the snapshot
        # doesn't have its own seq; SHA is the ground truth for staleness).
        if tracker is not None:
            tracker.record_agent_io(path, raw, seq=0)
    # E4 (T8) — return None only when there is NOTHING to tell the model.
    # If every file was omitted (binary / gone / permission / spill) we
    # still want to surface the notes so the model knows the working
    # set is not empty on disk — it just couldn't be rendered.
    if not blocks and not omitted_notes and not budget_omitted:
        return None
    # Compose the trailing notice: budget-omitted count (existing
    # behavior) PLUS the E4 per-file notes. A single trailing paragraph
    # keeps the snapshot's shape consistent; the notes are short
    # one-liners the model can act on (`file_read`, recreate, etc.).
    notice_parts: list[str] = []
    if budget_omitted:
        shown_names = budget_omitted[:12]
        remaining = len(budget_omitted) - len(shown_names)
        suffix = f", +{remaining} more" if remaining else ""
        notice_parts.append(
            "[not present in this request because of the snapshot budget: "
            + ", ".join(shown_names)
            + suffix
            + ". Their current bytes remain on disk; file_read only a path/range needed "
            "for the next action.]"
        )
    notice_parts.extend(omitted_notes)
    notice = ("\n\n" + "\n".join(notice_parts)) if notice_parts else ""
    coverage_lines = current_prompt_coverage_lines(prompt_receipts)
    coverage = ""
    if coverage_lines:
        coverage = (
            "\n\n# CURRENT PROMPT FILE COVERAGE\n"
            "Host-observed exact resource ranges physically present in this request:\n"
            + "\n".join(f"- {line}" for line in coverage_lines)
        )
    if out_prompt_receipts is not None:
        out_prompt_receipts.extend(prompt_receipts)
    return LLMMessage(
        role="user",
        content=preamble + "\n\n".join(blocks) + coverage + notice,
    )


def f8_shrink_file_write_args(messages: list[LLMMessage], events: list[Event]) -> list[LLMMessage]:
    """F8 — GATED render-time transform. For every assistant message
    whose tool_call is a ``file_write`` that was CONFIRMED successful
    (per :func:`_f8_confirmed_file_writes`), replace the long
    ``content`` argument with a short prefix + a recoverable marker.

    Render-time only: the persisted event log is unchanged. The
    transform runs AFTER ``View.of`` (and the existing
    ``_ARG_SNIP_CHARS`` shaper in events.py) so it OVERRIDES the
    generic "[[DISCO-ELIDED: N chars ...]]" marker with a more
    useful form: a real 200-char prefix + a path-aware history marker. The
    always-fresh workspace snapshot remains the normal grounding surface.

    The marker does not command a full reread: it identifies the resource and
    asks for only the minimal range if a future action actually needs more
    context. The event log remains lossless because the event's
    ``tool_call.arguments["content"]`` is never modified.

    Assist OFF (the default) → caller does not invoke this method;
    messages are byte-identical to today.

    Returns a new list; the input ``messages`` is not mutated. Each
    modified message is a new LLMMessage (LLMMessage is frozen, so
    ``model_copy`` is required); each modified tool_call dict is a
    new dict.
    """
    confirmed = _f8_confirmed_file_writes(events)
    if not confirmed:
        return messages
    out: list[LLMMessage] = []
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            out.append(msg)
            continue
        new_tcs: list[dict] = []
        mutated = False
        for tc in msg.tool_calls:
            if not isinstance(tc, dict):
                new_tcs.append(tc)
                continue
            cid = tc.get("id")
            if tc.get("name") == "file_write" and isinstance(cid, str) and cid in confirmed:
                path, content = confirmed[cid]
                # Only shrink when the ORIGINAL content is long
                # enough that a prefix is meaningful. Short writes
                # (≤ _F8_PREFIX_CHARS) pass through unchanged —
                # the snip shaper in events.py did not elide them
                # either, and the F8 prefix would be the full
                # content + marker (no reclaim, no value).
                if len(content) > _F8_PREFIX_CHARS:
                    args = tc.get("arguments")
                    if isinstance(args, dict):
                        new_args = dict(args)
                        new_args["content"] = content[
                            :_F8_PREFIX_CHARS
                        ] + _F8_TRUNCATION_MARKER_TEMPLATE.format(path=path)
                        new_tc = dict(tc)
                        new_tc["arguments"] = new_args
                        new_tcs.append(new_tc)
                        mutated = True
                        continue
            new_tcs.append(tc)
        if mutated:
            out.append(msg.model_copy(update={"tool_calls": new_tcs}))
        else:
            out.append(msg)
    return out


class ViewBuilder:
    """Materialize the model-facing View each turn: microcompact, condense if
    triggered (§8), gate the C6 tail-recap, apply the F8 shrink (assist), and
    append the always-fresh workspace snapshot. Back-ref collaborator: body is
    byte-identical to the former AgentLoop._materialize_view with self. →
    self._loop..

    W2 addition: holds a ``FileStateTracker`` instance across turns to power
    the stale-aware snapshot (full body only for stale/never-shown files,
    one-line pointer for unchanged known files) and the stale-change notice."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop
        # W2 — per-ViewBuilder file-state tracker (mutable, survives across
        # turns). Starts empty; populated by workspace_snapshot_message as
        # files are shown in full. No lock needed: build() runs under the
        # loop's conversation lock.
        self._file_tracker: FileStateTracker = FileStateTracker()

    def _resolve_caps(self) -> ContextCaps:
        """CW-2/CW-6 — resolve this turn's context caps from the capability gate
        (`assist`) and the LIVE driver context window. Reuses the SAME cached
        `_driver_context_window()` the condenser budgets against (runtime); does not
        re-probe. Best-effort: a loop without the runtime method (legacy/mock) →
        unknown window → the assist baseline (byte-identical to today)."""
        window: int | None = None
        probe = getattr(self._loop, "_driver_context_window", None)
        if callable(probe):
            try:
                result = probe()
                window = int(result) if isinstance(result, int) else None
            except Exception:  # noqa: BLE001 — never block view build on a probe miss
                window = None
        return derive_context_caps(assist=bool(self._loop._assist), context_window=window)

    def _context_compaction_policy(self) -> CompactionPolicy:
        policy = getattr(self._loop, "_context_compaction_policy", None)
        if isinstance(policy, CompactionPolicy):
            return policy
        return CompactionPolicy.default()

    async def _context_pack_message(self, events: list[Event]) -> LLMMessage:
        """Build the live ContextPack message, with durable store inputs best-effort."""
        base_ledger: ContextLedger | None = None
        todo_text: str | None = None
        design_direction: str | None = None
        failures: tuple[VerifierFailureRef, ...] | None = None
        sbx = getattr(getattr(self._loop, "executor", None), "sandbox", None)
        if sbx is not None:
            store = ArtifactMemoryStore(sbx)
            try:
                workspace_root = getattr(sbx, "workspace_root", None)
                reconstructed = await store.reconstruct(
                    str(getattr(self._loop, "conversation_id", "") or ""),
                    str(workspace_root) if workspace_root is not None else None,
                )
                base_ledger = reconstructed.ledger
                failures = reconstructed.ledger.latest_verifier_failures
            except Exception:  # noqa: BLE001 - context files are best-effort
                _LOG.warning(
                    "CXT live context-pack ledger read failed for %s",
                    getattr(self._loop, "conversation_id", ""),
                    exc_info=True,
                )
            try:
                todo_text = await store.read_todo()
            except Exception:  # noqa: BLE001 - absent/corrupt todo omits the section
                _LOG.warning(
                    "CXT live context-pack todo read failed for %s",
                    getattr(self._loop, "conversation_id", ""),
                    exc_info=True,
                )
            try:
                design_direction = await store.read_design_direction()
            except Exception:  # noqa: BLE001 - absent/corrupt design direction omits it
                _LOG.warning(
                    "CXT live context-pack design direction read failed for %s",
                    getattr(self._loop, "conversation_id", ""),
                    exc_info=True,
                )

        pack = build_context_pack(
            events,
            base_ledger=base_ledger,
            policy=self._context_compaction_policy(),
            todo_text=todo_text,
            design_direction=design_direction,
            failures=failures,
            allowed_next_actions=self._context_allowed_next_actions(events),
        )
        return LLMMessage(role="user", content=render_context_pack(pack))

    def _context_allowed_next_actions(self, events: list[Event]) -> tuple[str, ...]:
        """Expose the phase router's real planning scope in the durable pack."""

        if not self._planning_phase_active(events):
            return ()
        try:
            names = self._loop._driver.planning_allowed_tool_names()
        except Exception:  # noqa: BLE001 - view projection must remain available
            names = getattr(self._loop, "_planning_tools", frozenset())
        return tuple(sorted(str(name) for name in names if str(name)))

    def _planning_phase_active(self, events: list[Event]) -> bool:
        """Whether this plan-first loop is currently accepting planning actions."""

        if not getattr(self._loop, "_planning_tools", frozenset()):
            return False
        if self._loop._effective_mode(events) is not OperatingMode.PLANNING:
            return False
        return not signals.plan_submitted_since_current_planning(events)

    async def _project_view(self, events: list[Event], *, include_context_pack: bool) -> View:
        projected_events, retired = _retire_superseded_invalid_plan_feedback(events)
        view = View.of(projected_events)
        messages = [message for message in view.messages if message.content not in retired]
        if include_context_pack:
            msg = await self._context_pack_message(events)
            messages = _drop_context_pack_owned_history(messages)
            insert_at = 1 if messages else 0
            if inspect_enabled():
                try:
                    log_event(
                        "context_pack",
                        cid=str(getattr(self._loop, "conversation_id", "") or ""),
                        included=True,
                        message_index=insert_at,
                        chars=len(msg.content),
                    )
                except Exception:  # noqa: BLE001 - inspect must never affect prompts
                    pass
            messages = [*messages[:insert_at], msg, *messages[insert_at:]]
        planning_receipt = _plan_planning_receipt(
            events, planning_active=self._planning_phase_active(events)
        )
        execution_receipt = _plan_execution_receipt(events)
        if planning_receipt is not None:
            messages.append(planning_receipt)
        if execution_receipt is not None:
            messages.append(execution_receipt)
        return view.model_copy(update={"messages": messages})

    async def build(self, events: list[Event]) -> View:
        """The View alone, for callers that own their history and do not re-read it.

        Prefer :meth:`build_with_horizon` on any path that will go on to use "the
        events" afterwards -- see that method for why.
        """
        view, _horizon = await self.build_with_horizon(events)
        return view

    async def build_with_horizon(self, events: list[Event]) -> tuple[View, list[Event]]:
        """The View **and the exact event list it was built from**.

        ``_build`` can append durable events mid-build -- microcompact tombstones,
        context-compaction snips, and a condensation tombstone -- re-reading the log
        after each. The View therefore describes a LATER horizon than the list the
        caller passed in. A caller that renders this View but keeps its own
        pre-build list is holding two different horizons at once, and the span the
        condenser just replaced still looks live to it, so the SAME span can be
        condensed a second time.

        Returning both together makes the horizon impossible to lose: one response,
        one horizon.
        """
        # CW-2/CW-6 — derive the per-turn caps once and thread them into the
        # snapshot (pin breadth/size) + the observation snip. assist-ON → the
        # baseline caps + a None snip override → byte-identical to today.
        caps = self._resolve_caps()
        # Pass the derived snip cap unconditionally: events.py only HONORS it when
        # it strictly exceeds the 8k baseline, so the assist-ON / small-window caps
        # (obs_snip_chars == 8k) are a no-op there → byte-identical to today.
        snip_token = obs_snip_override.set(caps.obs_snip_chars)
        try:
            return await self._build(events, caps)
        finally:
            obs_snip_override.reset(snip_token)

    async def _build(self, events: list[Event], caps: ContextCaps) -> tuple[View, list[Event]]:
        # S3 Microcompact (GAP A): a cheap, no-model pass FIRST — tombstone no-op
        # turns (a failed call an identical later call superseded) so the lossy
        # model-summarization condenser fires on a smaller, denser residue (or not
        # at all). Idempotent: re-running won't re-tombstone an already-dropped span.
        micro = microcompact(events)
        if micro:
            for tomb in micro:
                await self._loop._emit(tomb)
            events = await self._loop._events()
        context_pack_active = context_pack_enabled()
        view = await self._project_view(events, include_context_pack=context_pack_active)
        # W2 — stale check before snapshot: identify files whose disk SHA
        # diverged from what the snapshot last showed (externally changed).
        # This runs first so the snapshot can mark those files as full-body
        # and the stale notice names exactly those paths. The check is
        # guarded by sandbox availability and a non-empty working set.
        sbx = getattr(getattr(self._loop, "executor", None), "sandbox", None)
        mutated, read_only = _workspace_paths_from_events(events)
        working_set = mutated + read_only
        stale: list[str] = []
        if sbx is not None and working_set:
            stale = await self._file_tracker.stale_paths(sbx, working_set)
        # A8: build the live workspace snapshot up-front so its size is counted in
        # the condense decision (steelman finding #4 — otherwise the condenser
        # undercounts the true prompt by ~4k tokens every turn, re-opening the same
        # estimation gap the A-S1 fix closed). The snapshot content is disk-derived
        # and independent of condensation, so building it before the condense check
        # and attaching it after is sound.
        # W2: call the module-level function directly (bypasses the loop's
        # _workspace_snapshot_message delegator) so we can pass the tracker +
        # pre-computed stale set. The loop delegator is kept for back-compat tests.
        # Capable models place the byte-stable block in the cacheable prefix;
        # assist mode keeps it in the recent tail. Both are stateless provider
        # requests and therefore receive current bounded bodies every turn.
        # Collect exact full-body paths for write grounding and typed revision/
        # range receipts for safe historical-read compaction.
        pinned_full: set[str] = set()
        snapshot_receipts: list[ObservationReceipt] = []
        snapshot = await workspace_snapshot_message(
            sbx,
            events,
            tracker=self._file_tracker,
            stale=frozenset(stale),
            caps=caps,
            pin_full=not self._loop._assist,
            out_pinned_full=pinned_full,
            out_prompt_receipts=snapshot_receipts,
        )
        # Every path pinned in FULL (untruncated) in the CURRENT WORKSPACE block
        # this turn has its disk-fresh current content in front of the model — a
        # subsequent file_write of it is grounded, NOT a blind rewrite-from-memory.
        # Satisfy the read-before-write gate for those paths so the model is not
        # forced into the read(only-a-pointer)/write(gated) deadlock that drove the
        # live shell-exec fallback. Truncated/omitted files are NOT pinned-in-full
        # (the model lacks their full content) → the gate still fires for them and
        # a real file_read is required, exactly as today.
        for _p in pinned_full:
            _ground_read(self._loop, _p)
        snap_tokens = len(snapshot.content) // 4 if snapshot is not None else 0
        est = signals.estimate_tokens(view) + snap_tokens
        # H3: log the prompt size per step so cost regressions are visible (the 60k
        # bloat was invisible because nothing measured it). DEBUG-level; cheap.
        _LOG.debug("driver view: ~%d input tokens, %d messages", est, len(view.messages))
        req = self._loop.condenser.should_condense(view, token_count=est)
        if req is not None and context_pack_active:
            snips = context_compact_if_needed(
                events,
                self._context_compaction_policy(),
                protected_seqs=protected_context_compaction_seqs(events),
                pressure_chars=est * 4,
            )
            if snips:
                for snip in snips:
                    await self._loop._emit(snip)
                events = await self._loop._events()
                view = await self._project_view(events, include_context_pack=context_pack_active)
                est = signals.estimate_tokens(view) + snap_tokens
                req = self._loop.condenser.should_condense(view, token_count=est)
        if req is not None:
            tombstone = await self._loop.condenser.condense(
                events, view, summarizer=self._loop.summarizer
            )
            if tombstone is not None:
                await self._loop._emit(tombstone)
                events = await self._loop._events()
                view = await self._project_view(events, include_context_pack=context_pack_active)
            # Soft trigger with no tombstone this step: proceed uncondensed and
            # retry next iteration (§8). Non-fatal.
        # Append the snapshot AFTER any condensation (so it is never rebuilt away by
        # a re-projection) and outside View.of (so the render-time snip/mask never
        # touch it). Disco's equivalent of Aider's always-fresh chat_files chunk.
        # C6 — gate the tail-recap on cadence + drift BEFORE the snapshot
        # append so the gate inspects a message list whose LAST element
        # is the recap (or the final condensation-rebuilt tail), not the
        # snapshot. The gate never touches the snapshot; the snapshot
        # is appended AFTER the gate regardless of the gate's verdict
        # (it's authoritative on-disk content the model needs).
        view = self._loop._gate_recitation(view, events, context_pack_active=context_pack_active)
        # Pure render-time compaction: only exact same-revision/range receipts may
        # replace a historical read. Bare paths and prior-turn delivery confer no
        # authority; a later partial read cannot evict an earlier full read.
        _pinned_arg = None if self._loop._assist else frozenset(pinned_full)
        view = view.model_copy(
            update={
                "messages": collapse_superseded_reads(
                    view.messages,
                    events,
                    pinned_full_paths=_pinned_arg,
                    prompt_receipts=tuple(snapshot_receipts),
                )
            }
        )
        # CW P1-a (round-2) — assist-OFF: retarget every elided tool-call ARGUMENT marker
        # to the neutral metadata-only marker. An
        # elided arg is a write/edit body — NOT the file's current content — so it is not
        # in the CURRENT WORKSPACE block even when the path is pinned; the old per-pin
        # "it's in the block" pointer was therefore a dangling claim. `_snip_args` rendered
        # the directional pre-CW-3 marker inside View.of (no tier context there); this pass
        # neutralizes it for the prefix-placed block. assist-ON keeps the directional
        # marker (correct for its tail-placed block + byte-identical to pre-CW-3).
        if not self._loop._assist:
            view = view.model_copy(update={"messages": retarget_elided_arg_markers(view.messages)})
        # F8 — GATED mid-turn arg truncation (assist-tier context-window
        # reclaim). When assist is ON, replace the long `content` argument
        # in any past assistant message whose `file_write` tool call was
        # CONFIRMED successful with a short prefix + a path-aware marker.
        # The current snapshot is the ordinary grounding surface; if more context
        # is genuinely needed, the marker asks only for an exact minimal range.
        # Render-time only: the persisted event log is unchanged. Assist OFF
        # → no-op; the rendered messages are byte-identical to today.
        # Runs AFTER the collapse so the F8 marker is applied to history
        # and after the snapshot append so the snapshot (authoritative
        # current state) is never shrunk.
        if self._loop._assist:
            view = view.model_copy(
                update={
                    "messages": self._loop._f8_shrink_file_write_args(view.messages, events),
                }
            )
        # W2 — the stale notice (if any) is appended AFTER all history transforms
        # (F8, collapse) so it is never accidentally shrunk or collapsed. It is a
        # VOLATILE per-turn alert (present only on a change turn), so it stays in
        # the tail next to the action regardless of tier — keeping it out of the
        # cacheable prefix.
        stale_msg = file_state_notice(stale)
        if snapshot is not None:
            _LOG.info(
                "A8 workspace snapshot injected: %d chars across the working set",
                len(snapshot.content),
            )
        # CW-3 — POSITION the pinned snapshot block.
        #   assist OFF (capable): the block is byte-stable (pin_full) → move it to
        #     the PREFIX (front of view.messages, which lands immediately after the
        #     system prompt once routing prepends it, BEFORE the event history). The
        #     system+tools+workspace prefix then caches as a unit; an unchanged turn
        #     rebills it at ~10% instead of re-billing the whole ~60k block. The
        #     stale notice stays in the tail (volatile).
        #   assist ON (small): UNCHANGED — stale notice then snapshot in the TAIL,
        #     byte-identical to before this change (the W2 pointer block is volatile,
        #     so it belongs in the recent, high-attention window, not a cache prefix).
        if self._loop._assist:
            tail: list[LLMMessage] = []
            if stale_msg is not None:
                tail.append(stale_msg)
            if snapshot is not None:
                tail.append(snapshot)
            if tail:
                view = view.model_copy(update={"messages": [*view.messages, *tail]})
        else:
            prefix = [snapshot] if snapshot is not None else []
            tail = [stale_msg] if stale_msg is not None else []
            if prefix or tail:
                view = view.model_copy(update={"messages": [*prefix, *view.messages, *tail]})
        # `events` has been re-read after every durable append above, so it is the
        # exact horizon this View describes. Returning it with the View is what
        # keeps a caller from carrying a stale list forward.
        return view, events
