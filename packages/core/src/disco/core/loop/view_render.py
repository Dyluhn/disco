"""View-rendering free functions for the agent loop.

Extracted from engine.py (the AgentLoop god-class). These are render-time
projections — the live workspace snapshot, the F8 arg-shrink transform, and the
router overflow signal. They carry no loop state: the snapshot takes the sandbox
explicitly, the others are pure over their inputs. Bodies are byte-identical to
the former AgentLoop methods.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from ..events import (
    WORKSPACE_SNAPSHOT_SENTINEL,
    ActionEvent,
    AgentErrorEvent,
    Event,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    obs_snip_override,
    retarget_elided_arg_markers,
)
from ..llm import Difficulty, OverflowSignal
from ..view import View, microcompact
from . import signals
from .context_budget import ContextCaps, derive_context_caps
from .dedup import (
    _F8_PREFIX_CHARS,
    _F8_TRUNCATION_MARKER_TEMPLATE,
    _f8_confirmed_file_writes,
    collapse_superseded_reads,
)
from .file_state import FileStateTracker, file_state_notice
from .messages import _workspace_paths_from_events

if TYPE_CHECKING:
    from .boundaries import Sandbox
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

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


async def workspace_snapshot_message(
    sbx: Sandbox | None,
    events: list[Event],
    *,
    tracker: FileStateTracker | None = None,
    stale: frozenset[str] | None = None,
    caps: ContextCaps | None = None,
    pin_full: bool = False,
    out_pinned_full: set[str] | None = None,
) -> LLMMessage | None:
    """Re-derive the CURRENT on-disk content of the working-set files from the
    sandbox each turn and render it as an authoritative, always-fresh message.

    WHY (root cause, source-verified + reproduced live): a file_write's body is
    elided at render time to "<N chars elided — use file_read>" (events._snip_args,
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

    W2 — stale-aware rendering (tracker + stale set):
      * tracker is None (default): backward-compat mode — all files shown in full
        every turn (identical to the pre-W2 behavior; used by the loop's own
        _workspace_snapshot_message delegator and legacy tests).
      * tracker provided: emit FULL body only for files in
        (stale ∪ never-shown-in-tracker); collapse every other file to a
        one-line "✓ {path} — current on disk, unchanged since last shown" pointer.
        The stale set
        is pre-computed by ViewBuilder.build (via FileStateTracker.stale_paths)
        and passed in so the snapshot does not re-read just for staleness.
        After showing a file in full, the tracker is updated.

    Layering-safe: reaches the sandbox via the same duck-typed
    getattr(self.executor, "sandbox", None) seam as _execute_and_observe."""
    if sbx is None:
        return None
    # CW-2: assist-OFF derives larger caps from the live window; the default
    # (caps is None) is the byte-identical assist-ON baseline used by the engine's
    # back-compat delegator and legacy tests.
    _caps = caps if caps is not None else _BASELINE_CAPS
    _stale: frozenset[str] = stale if stale is not None else frozenset()
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
    if pin_full:
        preamble = (
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
    else:
        preamble = (
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
    blocks: list[str] = []
    # E4 (T8) — per-file notes appended to the trailing omitted-notice.
    # Each note tells the model exactly WHY a file it touched is NOT
    # rendered as a BEGIN/END block (binary, deleted, permission). The
    # model can then `file_read` it itself when it needs the content.
    omitted_notes: list[str] = []
    budget = _caps.total_chars - len(preamble)
    shown_count = 0
    # E4 (T8) — .disco-spill-* are T10's overflow logs (head/tail markers
    # of an oversize stdout), not deliverables. They pollute the working
    # set with multi-MB noise and dwarf every real file. Skip them
    # outright: the model has no business re-reading its own spill log.
    # Match the leaf filename (basename) so an absolute path the model
    # might use (e.g. /workspace/.disco-spill-abc.log) is caught too.
    spill_basename_prefix = ".disco-spill-"
    for path in ordered:
        if shown_count >= _caps.max_files or budget <= 0:
            break
        # E4 (T8) — skip T10's overflow-log paths up-front
        if os.path.basename(path).startswith(spill_basename_prefix):
            continue
        # C5 — skip the `.pmx/` write-through mirror. It is the on-disk
        # durable copy of the in-View KnowledgeEvent channel (see
        # _write_pmx_memory_fact in this file). The View is the
        # authoritative in-session source; the file is just a recovery
        # aid for hard filesystem resets. Surfacing it in the working-
        # set snapshot would (a) be a divergent second store from the
        # model's POV and (b) double-count the same fact (once in the
        # View, once in the mirror). Exclude it like `.disco-spill-*`.
        if ".pmx" in path.split("/"):
            continue
        raw = await _read_working_file(sbx, path, omitted_notes)
        if isinstance(raw, _SnapshotHalt):
            break  # hung read ⇒ wedged sandbox; degrade to the snapshot so far
        if raw is None:
            continue  # gone / unreadable / directory / binary — note already added
        text = raw.decode("utf-8", "replace")
        # W2 — pointer for unchanged known files (tracker mode only).
        # If the tracker is active, this file has been shown in full before,
        # and its disk SHA has NOT changed since (not in the stale set), emit
        # a one-line pointer instead of re-dumping the whole body. This is the
        # primary context-reduction mechanism: a file shown on turn 1 that
        # hasn't changed costs 1 line on every subsequent turn.
        #
        # CW-3 — `pin_full` SUPPRESSES the pointer collapse. When the block lives
        # in the cacheable PREFIX (capable models), a turn-dependent full→pointer
        # transition would make the block bytes DIFFER turn-over-turn for an
        # UNCHANGED file → never a stable cache prefix. With pin_full the block is
        # a pure function of (on-disk content + working-set order): byte-identical
        # across turns until a file actually changes, so prompt caching rebills it
        # at ~10%. The tracker is STILL updated below (so external-change staleness
        # detection keeps working); only the pointer shortcut is skipped.
        if (
            not pin_full
            and tracker is not None
            and tracker.is_known(path)
            and path not in _stale
        ):
            # CW P1-c — this pointer only renders when NOT pin_full (assist-ON), where
            # the pre-CW-3 wording is byte-identical for the tail-placed block.
            pointer = f"✓ {path} — current, shown earlier"
            budget -= len(pointer)
            blocks.append(pointer)
            shown_count += 1
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
        else:
            shown = text
            # CW-4 (truthful pointers): this file is rendered in FULL (untruncated)
            # in the block this turn → record it so the caller's
            # collapse_superseded_reads can truthfully point a superseded-read stub
            # at the WORKSPACE block for this path. Truncated files (the `if` branch
            # above) and pointer-collapsed / omitted / skipped files are NOT
            # recorded → their stubs use the no-claim notice. Output-only side
            # channel: it never affects the returned message bytes (assist-ON stays
            # byte-identical when the caller ignores it).
            if out_pinned_full is not None:
                out_pinned_full.add(path)
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
    if not blocks and not omitted_notes:
        return None
    omitted = len(ordered) - shown_count
    # Compose the trailing notice: budget-omitted count (existing
    # behavior) PLUS the E4 per-file notes. A single trailing paragraph
    # keeps the snapshot's shape consistent; the notes are short
    # one-liners the model can act on (`file_read`, recreate, etc.).
    notice_parts: list[str] = []
    if omitted > 0:
        notice_parts.append(
            f"[{omitted} more file(s) you have touched are not shown here "
            f"(snapshot budget) — file_read them when you need their content.]"
        )
    notice_parts.extend(omitted_notes)
    notice = ("\n\n" + "\n".join(notice_parts)) if notice_parts else ""
    return LLMMessage(role="user", content=preamble + "\n\n".join(blocks) + notice)


def f8_shrink_file_write_args(
    messages: list[LLMMessage], events: list[Event]
) -> list[LLMMessage]:
    """F8 — GATED render-time transform. For every assistant message
    whose tool_call is a ``file_write`` that was CONFIRMED successful
    (per :func:`_f8_confirmed_file_writes`), replace the long
    ``content`` argument with a short prefix + a recoverable marker.

    Render-time only: the persisted event log is unchanged. The
    transform runs AFTER ``View.of`` (and the existing
    ``_ARG_SNIP_CHARS`` shaper in events.py) so it OVERRIDES the
    generic "<N chars elided — use file_read>" marker with a more
    useful form: a real 200-char prefix + a path-aware hint. The
    on-disk full content is the recovery surface (file_read /
    workspace snapshot).

    Lossless for the wire (the marker names the file's path, so
    ``file_read <path>`` recovers the full content) and lossless
    for the event log (the event's ``tool_call.arguments["content"]``
    is never modified — only the rendered message the provider
    sees is trimmed).

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
            if (
                tc.get("name") == "file_write"
                and isinstance(cid, str)
                and cid in confirmed
            ):
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
                        new_args["content"] = (
                            content[:_F8_PREFIX_CHARS]
                            + _F8_TRUNCATION_MARKER_TEMPLATE.format(path=path)
                        )
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

    async def build(self, events: list[Event]) -> View:
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

    async def _build(self, events: list[Event], caps: ContextCaps) -> View:
        # S3 Microcompact (GAP A): a cheap, no-model pass FIRST — tombstone no-op
        # turns (a failed call an identical later call superseded) so the lossy
        # model-summarization condenser fires on a smaller, denser residue (or not
        # at all). Idempotent: re-running won't re-tombstone an already-dropped span.
        micro = microcompact(events)
        if micro:
            for tomb in micro:
                await self._loop._emit(tomb)
            events = await self._loop._events()
        view = View.of(events)
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
        # CW-3 — capable models (assist OFF) pin the working set in FULL and place
        # the block in the cacheable PREFIX, so suppress the per-turn pointer
        # collapse (pin_full): the block must be byte-identical turn-over-turn for
        # an unchanged file to be a stable cache prefix. assist ON keeps the W2
        # pointer behavior + tail placement (byte-identical to today).
        # CW-4 — collect the set of paths pinned in FULL in the block this turn, so
        # the superseded-read collapse below can point a stub at the WORKSPACE block
        # ONLY for paths whose current content is actually present there in full
        # (assist-OFF). Output-only: it never changes the snapshot bytes.
        pinned_full: set[str] = set()
        snapshot = await workspace_snapshot_message(
            sbx,
            events,
            tracker=self._file_tracker,
            stale=frozenset(stale),
            caps=caps,
            pin_full=not self._loop._assist,
            out_pinned_full=pinned_full,
        )
        snap_tokens = len(snapshot.content) // 4 if snapshot is not None else 0
        est = signals.estimate_tokens(view) + snap_tokens
        # H3: log the prompt size per step so cost regressions are visible (the 60k
        # bloat was invisible because nothing measured it). DEBUG-level; cheap.
        _LOG.debug("driver view: ~%d input tokens, %d messages", est, len(view.messages))
        req = self._loop.condenser.should_condense(view, token_count=est)
        if req is not None:
            tombstone = await self._loop.condenser.condense(
                events, view, summarizer=self._loop.summarizer
            )
            if tombstone is not None:
                await self._loop._emit(tombstone)
                view = View.of(await self._loop._events())
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
        view = self._loop._gate_recitation(view, events)
        # W2 — collapse superseded reads (ALL tiers, NOT assist-gated).
        # Rewrite every earlier file_read tool-result for a path to a short
        # "[superseded...]" stub; keep only the most-recent result in full.
        # This is a pure render-time compaction — the event log is unchanged.
        # CW-4 — assist-OFF: pass the pinned-in-full set so a stub only claims
        # "current content is in the WORKSPACE block" for paths that are actually
        # there in full this turn; the rest get the no-claim notice. assist-ON: pass
        # None → byte-identical to before CW-4 (every stub points to the block,
        # matching that tier's tail-snapshot contract).
        _pinned_arg = None if self._loop._assist else frozenset(pinned_full)
        view = view.model_copy(
            update={
                "messages": collapse_superseded_reads(
                    view.messages, events, pinned_full_paths=_pinned_arg
                )
            }
        )
        # CW P1-a (round-2) — assist-OFF: retarget every elided tool-call ARGUMENT marker
        # to the NEUTRAL non-dangling marker (re-issue / file_read recovery only). An
        # elided arg is a write/edit body — NOT the file's current content — so it is not
        # in the CURRENT WORKSPACE block even when the path is pinned; the old per-pin
        # "it's in the block" pointer was therefore a dangling claim. `_snip_args` rendered
        # the directional pre-CW-3 marker inside View.of (no tier context there); this pass
        # neutralizes it for the prefix-placed block. assist-ON keeps the directional
        # marker (correct for its tail-placed block + byte-identical to pre-CW-3).
        if not self._loop._assist:
            view = view.model_copy(
                update={"messages": retarget_elided_arg_markers(view.messages)}
            )
        # F8 — GATED mid-turn arg truncation (assist-tier context-window
        # reclaim). When assist is ON, replace the long `content` argument
        # in any past assistant message whose `file_write` tool call was
        # CONFIRMED successful with a short prefix + a path-aware marker.
        # Lossless: the full content lives on disk (and in the snapshot) —
        # `file_read <path>` recovers it. Render-time only: the persisted
        # event log is unchanged. Assist OFF (the capable-model default)
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
                view = view.model_copy(
                    update={"messages": [*prefix, *view.messages, *tail]}
                )
        return view
