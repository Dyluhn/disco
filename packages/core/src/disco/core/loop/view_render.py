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
    ActionEvent,
    AgentErrorEvent,
    Event,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
)
from ..llm import Difficulty, OverflowSignal
from ..view import View, microcompact
from . import signals
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

_WS_MAX_FILES = 8  # cap the snapshot breadth (most-recently-touched first)
_WS_PER_FILE_CHARS = 6_000  # per-file cap; larger files head/tail-truncate with a marker
_WS_TOTAL_CHARS = 16_000  # total snapshot budget (~4k tokens), bounded vs the condenser
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


async def workspace_snapshot_message(
    sbx: Sandbox | None,
    events: list[Event],
    *,
    tracker: FileStateTracker | None = None,
    stale: frozenset[str] | None = None,
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
        one-line "✓ {path} — current, shown earlier" pointer. The stale set
        is pre-computed by ViewBuilder.build (via FileStateTracker.stale_paths)
        and passed in so the snapshot does not re-read just for staleness.
        After showing a file in full, the tracker is updated.

    Layering-safe: reaches the sandbox via the same duck-typed
    getattr(self.executor, "sandbox", None) seam as _execute_and_observe."""
    if sbx is None:
        return None
    _stale: frozenset[str] = stale if stale is not None else frozenset()
    mutated, read_only = _workspace_paths_from_events(events)
    ordered = mutated + read_only  # mutated first → never evicted by reads (#1)
    if not ordered:
        return None
    preamble = (
        "# CURRENT WORKSPACE — your files on disk RIGHT NOW (authoritative).\n"
        "Below is the live, exact content of the files you are working on, "
        "re-read from disk this turn. It OVERRIDES any earlier or elided copy of "
        "these files shown above; trust THIS over your memory.\n"
        "To change a file, the RELIABLE way is to call `file_write` with the FULL "
        "new content = everything shown below for that file PLUS your change. "
        "Keep the docstring, every constant, and every existing function you are "
        "not deliberately removing — do not drop anything. Adding to a file is "
        "expected and good. AVOID line-number edits (`file_replace_lines` / "
        "`file_insert_lines`) for these changes: line numbers shift as you edit "
        "and a wrong range silently deletes code — a whole-file `file_write` "
        "based on the content below cannot miscount.\n"
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
    budget = _WS_TOTAL_CHARS - len(preamble)
    shown_count = 0
    # E4 (T8) — .disco-spill-* are T10's overflow logs (head/tail markers
    # of an oversize stdout), not deliverables. They pollute the working
    # set with multi-MB noise and dwarf every real file. Skip them
    # outright: the model has no business re-reading its own spill log.
    # Match the leaf filename (basename) so an absolute path the model
    # might use (e.g. /workspace/.disco-spill-abc.log) is caught too.
    spill_basename_prefix = ".disco-spill-"
    for path in ordered:
        if shown_count >= _WS_MAX_FILES or budget <= 0:
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
        try:
            raw = await asyncio.wait_for(
                sbx.read_file(path), timeout=_WS_READ_TIMEOUT_S
            )
        except TimeoutError:
            # A hung read implies a wedged/dead sandbox. STOP — don't hold the
            # conversation lock for timeout×N files (that would block pause/steer/
            # cancel). Degrade to the snapshot collected so far. The sandbox isn't
            # healed here (this is a pure projection step); the model's NEXT real
            # action goes through _execute_and_observe, which detects the dead
            # sandbox and emits the restart notice.
            break
        except FileNotFoundError:
            # E4 (T8) — file was deleted between the action and this read.
            # Surface that as an explicit note (the model can re-decide
            # whether to re-create the file); don't silently lose it.
            omitted_notes.append(f"[file gone: {path}]")
            continue
        except PermissionError:
            # E4 (T8) — the sandbox denied this read. Note it so the
            # model knows the path exists in its working set but the
            # snapshot cannot show it (the model can still try file_read).
            omitted_notes.append(f"[unreadable: permission: {path}]")
            continue
        except (IsADirectoryError, NotADirectoryError, OSError) as e:
            # E4 (T8) — a directory slipped into the working set (e.g. the
            # model `file_write`d a directory path by mistake). Skip with
            # a brief note; do not include its binary directory listing.
            if isinstance(e, IsADirectoryError) or (
                isinstance(e, OSError) and getattr(e, "errno", None) == 21  # EISDIR
            ):
                omitted_notes.append(f"[directory: {path}]")
                continue
            # Any other OSError — keep the prior silent-skip behavior
            # (the existing contract was "file gone/unreadable: skip").
            continue
        except Exception:  # noqa: BLE001 — file gone/unreadable this turn: skip it
            continue
        # E4 (T8) — binary sniff. A NUL byte in the first 1KB is a near-
        # certain signal of binary content (text decoders reject it;
        # editors render garbage; the snapshot's only value is showing
        # the model something it can act on). Bounded sample (1KB, not
        # the whole file) so a huge text file with one stray NUL past
        # the head — e.g. an embedded null in a template — still
        # renders normally via `errors="replace"`.
        if b"\x00" in raw[:1024]:
            omitted_notes.append(f"[binary omitted: {path}]")
            continue
        text = raw.decode("utf-8", "replace")
        # W2 — pointer for unchanged known files (tracker mode only).
        # If the tracker is active, this file has been shown in full before,
        # and its disk SHA has NOT changed since (not in the stale set), emit
        # a one-line pointer instead of re-dumping the whole body. This is the
        # primary context-reduction mechanism: a file shown on turn 1 that
        # hasn't changed costs 1 line on every subsequent turn.
        if (
            tracker is not None
            and tracker.is_known(path)
            and path not in _stale
        ):
            pointer = f"✓ {path} — current, shown earlier"
            budget -= len(pointer)
            blocks.append(pointer)
            shown_count += 1
            continue
        # Full body: file is stale, never-shown-in-tracker, or tracker is None
        # (backward-compat mode). Render head + tail when oversize.
        cap = min(_WS_PER_FILE_CHARS, budget)
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

    async def build(self, events: list[Event]) -> View:
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
        snapshot = await workspace_snapshot_message(
            sbx,
            events,
            tracker=self._file_tracker,
            stale=frozenset(stale),
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
        view = view.model_copy(
            update={"messages": collapse_superseded_reads(view.messages, events)}
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
        # W2 — assemble trailing context: stale notice (if any) + snapshot.
        # Stale notice goes BEFORE the snapshot so the model reads "these
        # files changed" immediately before seeing the authoritative content.
        # Both are appended AFTER all history transforms (F8, collapse) so
        # they are never accidentally shrunk or collapsed.
        extra: list[LLMMessage] = []
        stale_msg = file_state_notice(stale)
        if stale_msg is not None:
            extra.append(stale_msg)
        if snapshot is not None:
            _LOG.info(
                "A8 workspace snapshot injected: %d chars across the working set",
                len(snapshot.content),
            )
            extra.append(snapshot)
        if extra:
            view = view.model_copy(update={"messages": [*view.messages, *extra]})
        return view
