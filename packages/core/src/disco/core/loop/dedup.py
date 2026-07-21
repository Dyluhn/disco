"""F8/F9 — GATED context/latency-reclaim helpers (assist-tier).

Extracted from `engine.py` (god-file decomposition, Wave 1). Pure functions
that analyze the event list — no loop state, no `self`, no agent calls:

* F8 (`_f8_confirmed_file_writes`): which file_writes are confirmed-successful,
  so their stored ``content`` arg can be truncated at RENDER time (the full
  content is on disk + in the workspace snapshot).
* F9 (`_f9_*`): read-only sliding-window dedup — detect when a read-only tool
  call exactly repeats a recent one (and nothing has mutated the path since),
  so the engine can short-circuit it with a pointer instead of re-executing.

Both are GATED behind the assist tier by their callers; assist OFF (the
capable-model default) → these are never invoked and the engine is
byte-identical to today.

The ``_WORKSPACE_MUTATING_TOOLS`` / ``_WORKSPACE_READ_TOOLS`` tool-classification
frozensets live here (rather than in engine.py) because F9's invalidation and
read-only checks are their primary consumer; engine.py re-imports them for the
A8 workspace-snapshot machinery. Keeping them here lets this module avoid an
engine import (no cycle).
"""

from __future__ import annotations

from ..effects import ObservationReceipt
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    WorkspaceMutationEvent,
)
from .resource_context import (
    canonical_workspace_identifier,
    latest_read_revision_by_resource,
    read_receipt_records,
    receipt_covered_in_current_prompt,
    rendered_content_matches_receipt,
    select_prompt_read_records,
)

# A8 (file-state survival): single-file tools whose `path` arg names a file the
# agent has touched — the working set re-read from disk each turn by
# _workspace_snapshot_message. file_list is excluded (its path is a directory).
# Files the agent MUTATED — the deliverable; these must never be evicted from the
# snapshot by exploration reads (steelman finding #1).
_WORKSPACE_MUTATING_TOOLS = frozenset(
    {
        "file_write",
        "file_append",
        "file_edit",
        "file_replace_lines",
        "file_insert_lines",
    }
)
_WORKSPACE_READ_TOOLS = frozenset({"file_read"})


def _canonical_path(path: str) -> str:
    """Canonical workspace-path key, MIRRORING the read-before-write gate's
    canonicalizer (``disco.tools.builtin.files._canonical`` →
    ``posixpath.normpath(strip_redundant_workspace_prefix(path))``).

    Replicated here because core may not import the tools layer (package
    layering: core ← … ← tools). Both must agree, or two spellings of the SAME
    file diverge: the gate sets/clears its bit under the canonical key, while F9
    invalidation comparing RAW strings could miss a mutation recorded under a
    different spelling ('x.py' read, './x.py' or 'workspace/x.py' written) — F9
    would then dedup a STALE read AND ground a write against changed content. Strip
    ONE leading 'workspace/' or '/workspace/' (matching strip_redundant_workspace_
    prefix's one-prefix rule), then normpath to fold './', '//', '../'."""
    import posixpath

    for prefix in ("/workspace/", "workspace/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    return posixpath.normpath(path)


# ---------------------------------------------------------------------------
# F8 — GATED mid-turn arg truncation (assist-tier context-window reclaim).
#
# When assist is ON, after a `file_write` tool call has been CONFIRMED
# successful (a corresponding success tool-result exists), the stored
# `content` argument in that historical assistant message is shrunk to a
# short prefix + a marker, because the full content now lives on disk
# and in the workspace snapshot. This reclaims context window. Lossless:
# the content is recoverable via file_read or the snapshot.
#
# Truncate ONLY confirmed-successful writes. A write whose result is an
# error/failure must keep its FULL args (needed for the model to retry
# intelligently). A write with NO observation yet (unconfirmed) must also
# keep its full args — the model might still need to retry, and the
# transform has no evidence the write succeeded.
#
# Assist OFF (the capable-model default) → the transform is never
# invoked; messages are byte-identical to today. The transform applies
# at RENDER TIME only — the persisted event log is unchanged; the
# on-disk full content is the recovery surface.
# ---------------------------------------------------------------------------

# Length of the kept prefix. 200 chars is small enough to reclaim real
# context (~50 tokens per write) yet large enough to show the model the
# shape of what it wrote (imports, top-of-file constants, the first
# function signature) so a future turn that needs to recall "what's at
# the top of X.py?" doesn't always have to file_read.
_F8_PREFIX_CHARS = 200

# Marker template — must include the file path so the model can
# file_read the full content back when it needs to. The "\n… " prefix is
# a visual cue that the original content was longer (matches the
# existing snip convention in events._ARG_SNIP_CHARS).
_F8_TRUNCATION_MARKER_TEMPLATE = (
    "\n… [written to {path}; history display only; this marker is not file content. "
    "Current disk state is authoritative. Do not copy or re-send this marker; request "
    "only the exact range needed for the next change.]"
)


def _f8_confirmed_file_writes(events: list[Event]) -> dict[str, tuple[str, str]]:
    """For every file_write whose execution has been CONFIRMED successful,
    return ``{call_id: (path, full_content)}``.

    A file_write is "confirmed successful" when:

      * an ``ActionEvent`` exists with ``tool_call.tool_name == "file_write"``
        and ``tool_call.call_id == C``; AND
      * a later ``ObservationEvent`` exists with
        ``tool_result.success is True`` and ``tool_result.call_id == C``
        (the executor echoes the proposed call_id per
        ``DefaultToolExecutor.execute``); AND
      * NO ``AgentErrorEvent`` exists with ``tool_call_id == C`` (a
        failure "un-confirms" a prior success for the same call_id —
        defensive; the engine's one-observation-per-action invariant
        makes this rare in practice).

    A write with NO matching observation is treated as not-yet-confirmed
    (its full content stays — the model might still need it to retry or
    to understand what it was about to write). The full content is the
    ORIGINAL ``tool_call.arguments["content"]`` (the event-stored bytes),
    NOT the snipped version in the rendered message. Returning the
    original lets the F8 transform emit a real 200-char prefix instead
    of a generic "[[DISCO-ELIDED: N chars ...]]" marker.

    Returns a dict keyed by call_id so the message-render pass can
    correlate each assistant message's ``tool_calls[i]["id"]`` to the
    transform output in O(1).
    """
    actions_by_call: dict[str, ActionEvent] = {}
    failed_call_ids: set[str] = set()
    confirmed_call_ids: set[str] = set()
    for e in events:
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.tool_call.tool_name == "file_write":
                actions_by_call[e.tool_call.call_id] = e
        elif isinstance(e, ObservationEvent):
            cid = e.tool_result.call_id
            if e.tool_result.success:
                confirmed_call_ids.add(cid)
            else:
                failed_call_ids.add(cid)
        elif isinstance(e, AgentErrorEvent) and e.tool_call_id:
            failed_call_ids.add(e.tool_call_id)
    # A failure un-confirms a prior success (defensive — a single
    # call_id should never have BOTH a success and a failure observation
    # under the engine's contract, but be explicit).
    confirmed_call_ids -= failed_call_ids
    out: dict[str, tuple[str, str]] = {}
    for cid in confirmed_call_ids:
        action = actions_by_call.get(cid)
        if action is None or action.tool_call is None:
            continue
        args = action.tool_call.arguments or {}
        content = args.get("content")
        if not isinstance(content, str):
            continue
        path = args.get("path")
        path_str = str(path) if isinstance(path, str) and path else "?"
        out[cid] = (path_str, content)
    return out


# ---------------------------------------------------------------------------
# F9 — GATED read-only sliding-window dedup (assist-tier context/latency
# reclaim).
#
# When assist is ON, a read-only tool call that EXACTLY repeats a recent
# read-only call (same tool name + same arguments) within the last
# _F9_WINDOW_SIZE read-only calls is short-circuited: a pointer to the
# prior result is emitted as the ObservationEvent INSTEAD of re-executing
# the tool. This is a context/latency reclaim for weak models that
# re-read the same file within a single turn or across a few turns.
# Per-turn idempotent writes are already covered by the workspace
# snapshot; F9 covers READS only.
#
# Invalidation: when a read's `path` arg is mutated by a write/edit
# BETWEEN the prior read and now, the prior read is stale and MUST NOT
# be deduped (a stale pointer would be wrong — the on-disk content is
# now different from what the prior read returned). The invalidation
# check uses the existing A8 working-set logic (_WORKSPACE_MUTATING_TOOLS):
# any file_write / file_edit / file_append / file_replace_lines /
# file_insert_lines on the same path invalidates the cached read.
#
# "Read-only" is determined by the SAME source the engine already uses
# — executor.readonly_tool_names() via _readonly_tool_names(), with a
# fallback to the narrow _WORKSPACE_READ_TOOLS frozenset when the
# executor can't report it. The read-only signal is GROUNDED, not a
# new hardcoded list (mirrors the planner-safety backstop).
#
# Assist OFF (capable-model default) → the dedup path is never entered;
# every read re-executes, byte-identical to today. The dedup runs at
# EXECUTION TIME (not render time, unlike F8) — we never call
# executor.execute() for a deduped call. The persisted event shape
# stays the same (ActionEvent + ObservationEvent pair); the
# ObservationEvent's content is the F9 pointer, not the tool's actual
# output. Lossless: the model can always `file_read` again to force a
# fresh read.
# ---------------------------------------------------------------------------

# Sliding-window size: how far back (in read-only calls) to look for an
# exact-args repeat. Small and bounded (8) so a model can't re-anchor to
# a read from many turns ago. The window is in READ-ONLY CALLS, not
# steps, so unrelated mutating work between two identical reads doesn't
# burn the budget. When the window is exhausted without a match, the
# call re-executes normally (the F9 spec: "the window evicts old
# entries").
_F9_WINDOW_SIZE = 8

# Short pointer text emitted as the synthetic observation when a dedup
# fires. Names the tool + the args summary so the model knows which
# earlier read to look at. Lossless: file_read again recovers the live
# content if the model suspects staleness. The marker is short on
# purpose (a few dozen chars) — the goal is to reclaim the context the
# duplicated read would have eaten, not to re-render the prior output.
_F9_POINTER_TEMPLATE = (
    "[F9 dedup: {tool_name}({arg_summary}) identical to a recent read "
    "this turn — see the earlier result; file_read again only if you "
    "suspect it changed]"
)

# Maximum length of the arg summary in the pointer text. Keep it small
# (readable in one line) so a 2KB path doesn't bloat the pointer. The
# full args are recoverable from the prior action's rendered message;
# the pointer just needs to identify WHICH prior read.
_F9_ARG_SUMMARY_MAX_CHARS = 120


def _f9_arg_summary(args: dict | None) -> str:
    """F9 — render a short, single-line summary of a tool call's args for
    the dedup pointer text. Prefers the `path` (workspace reads), then
    the first non-empty string arg in stable key order. Length-bounded
    so a 2KB path or 1KB regex doesn't bloat the pointer."""
    if not isinstance(args, dict):
        return ""
    p = args.get("path")
    if isinstance(p, str) and p:
        s = p
    else:
        # No path — pick the first non-empty string arg in stable key
        # order (deterministic). Falls back to "" for empty args
        # (the pointer template still renders cleanly with an empty
        # arg summary — the tool name is the load-bearing identifier).
        s = ""
        for k in sorted(args.keys()):
            v = args[k]
            if isinstance(v, str) and v:
                s = f"{k}={v}"
                break
    if len(s) > _F9_ARG_SUMMARY_MAX_CHARS:
        s = s[: _F9_ARG_SUMMARY_MAX_CHARS - 1] + "\u2026"
    return s


def _f9_has_successful_observation(events: list[Event], action_id: str) -> bool:
    """F9 — does `action_id` have a successful ObservationEvent in `events`?
    A failed read returns empty/error content; pointing at a failed
    read would tell the model "see the earlier result" when there IS
    no useful earlier result. Only successful reads are dedupable. The
    walk terminates at the first matching observation for this action
    (the engine's one-observation-per-action invariant)."""
    for e in events:
        if isinstance(e, ObservationEvent) and e.action_id == action_id:
            return bool(e.tool_result.success)
    return False


def _f9_path_was_mutated_after(events: list[Event], path: str, after_seq: int) -> bool:
    """F9 — invalidation: was `path` mutated by a mutating tool call
    STRICTLY AFTER `after_seq`? Uses the A8 mutating-tool set
    (_WORKSPACE_MUTATING_TOOLS) as the single source of truth — a
    write/edit to the same path between the prior read and now
    invalidates the cached read (stale pointer).

    `after_seq` is the seq of the prior read being considered; we walk
    events with seq > after_seq looking for a write to `path`. The
    current action (the one being deduped) is excluded by the walk in
    _f9_dedupable_read (which feeds `events[:-1]`); a `file_read`
    call can't itself be a mutation so the upper bound is unnecessary.
    """
    if not isinstance(path, str) or not path:
        return False
    # Canonicalize BOTH sides so a mutation invalidates the matching read
    # regardless of path spelling (matches the gate's canonical key — see
    # _canonical_path). Raw '==' would let a write under './x.py' or
    # 'workspace/x.py' fail to invalidate a read of 'x.py', leaving F9 to dedup
    # AND ground a stale read.
    canon = _canonical_path(path)
    for e in events:
        if (e.seq or 0) <= after_seq:
            continue
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        if e.tool_call.tool_name not in _WORKSPACE_MUTATING_TOOLS:
            continue
        ep = e.tool_call.arguments.get("path")
        if isinstance(ep, str) and ep and _canonical_path(ep) == canon:
            return True
    return False


# ---------------------------------------------------------------------------
# W2 — RENDER-TIME history body-collapse for ALL tiers (not assist-gated).
#
# Every earlier file_read result for a path is replaced with a short
# "[superseded...]" notice when a LATER result for the same path exists in
# the prompt. This eliminates the bulk of repeated file_read context (e.g.
# the same file read 5× across history → 4 pages replaced by 4 one-liners).
# Exact observation receipts make read compaction revision/range aware. A full
# current-prompt snapshot may replace equivalent historical reads; within read
# history, only coverage already supplied by a retained read of the SAME active
# revision is redundant. Legacy reads remain verbatim. Older authenticated
# revisions become explicit stale facts rather than misleading grounding.
#
# Keep F9's EXECUTION-TIME short-circuit assist-gated (it changes which
# tool calls actually execute; changing that on capable models would hide
# information). This W2 collapse is render-only and always safe.
# ---------------------------------------------------------------------------

# This notice is emitted only after an exact same-revision/range prompt receipt
# proves that the CURRENT WORKSPACE block physically contains the replacement.
_SUPERSEDED_READ_NOTICE = (
    "[superseded file_read of {path} — current content is in the "
    "CURRENT WORKSPACE block in this prompt]"
)

# Compatibility-only legacy constant. The revision-aware renderer never emits it;
# absence of exact receipts now preserves the original body instead.
_SUPERSEDED_READ_NOTICE_NO_PIN = (
    "[historical file_read of {path} — no exact current revision is proven elsewhere; "
    "the original body must remain in the prompt]"
)

_REDUNDANT_READ_NOTICE = (
    "[redundant file_read of {path} — the same resource revision and exact "
    "range are retained elsewhere in this prompt]"
)

_STALE_READ_NOTICE = (
    "[stale file_read of {path} @ sha256:{digest} — a newer authenticated revision "
    "of this resource is retained in this prompt; this body is not current grounding]"
)


def collapse_superseded_reads(
    messages: list[LLMMessage],
    events: list[Event],
    *,
    pinned_full_paths: frozenset[str] | None = None,
    prompt_receipts: tuple[ObservationReceipt, ...] = (),
) -> list[LLMMessage]:
    """W2 RENDER-TIME transform — NOT assist-gated.

    Typed reads are compacted only when the same revision/range remains exact in
    another retained read or in ``prompt_receipts`` (normally a full CURRENT
    WORKSPACE body). A later partial read can therefore never evict an earlier
    complete read. Legacy reads without revision receipts are retained
    conservatively; historical delivery is not assumed to be current prompt
    presence.

    ``prompt_receipts`` describes exact bytes already present in non-history
    request blocks. The old ``pinned_full_paths`` argument remains as a source-
    compatible migration seam but confers no authority: a bare path cannot prove
    that a snapshot contains the same revision as a historical read.

    Does not collapse legacy/no-receipt reads, a revision/range not covered by a
    retained observation, non-file-read results, or non-tool messages.

    Returns a new list; input is not mutated.
    """
    # Build call_id → path for file_read actions from the event log.
    # _WORKSPACE_READ_TOOLS = frozenset({"file_read"}) — use the single
    # source of truth from this module rather than a new frozenset.
    file_read_calls: dict[str, str] = {}  # call_id → path
    for e in events:
        if (
            isinstance(e, ActionEvent)
            and e.tool_call is not None
            and e.tool_call.tool_name in _WORKSPACE_READ_TOOLS
        ):
            p = e.tool_call.arguments.get("path")
            if isinstance(p, str) and p:
                file_read_calls[e.tool_call.call_id] = canonical_workspace_identifier(p)

    if not file_read_calls:
        return messages

    records = read_receipt_records(events)
    record_by_call = {record.call_id: record for record in records}
    latest_revisions = latest_read_revision_by_resource(records)
    prompt_receipt_tuple = tuple(prompt_receipts)
    # A host-rebuilt CURRENT WORKSPACE body is newer authority than every
    # historical read in the append-only log, even when the bounded body covers
    # only a head/tail window.
    for receipt in prompt_receipt_tuple:
        latest_revisions[receipt.revision.resource] = receipt.revision
    selected_calls = {record.call_id for record in select_prompt_read_records(records)}
    latest_run_intent_seq = max(
        (
            event.seq
            for event in events
            if isinstance(event, WorkspaceMutationEvent)
            and event.operation.startswith("agent.run-intent.")
            and type(event.seq) is int
        ),
        default=None,
    )
    # A model-requested read in the active run is high-attention evidence: keep
    # its selected exact body in the recent tool-result position even when the
    # cacheable CURRENT WORKSPACE prefix contains the same revision. Replacing
    # that just-requested result with a pointer back to the prefix makes a
    # successful read look unanswered and caused a real read/read/read loop in
    # revision planning. The selected set is already resource/byte bounded, and
    # a newer run intent naturally makes prior-run reads compactable again.
    active_run_records = [
        record
        for record in records
        if latest_run_intent_seq is not None
        and record.observation_seq is not None
        and record.observation_seq > latest_run_intent_seq
    ]
    active_run_visible_calls = {
        record.call_id for record in active_run_records if record.call_id in selected_calls
    }
    # A targeted partial read may be set-theoretically redundant with an older
    # complete read, but its immediate answer still belongs in the next request.
    # Preserve at most that one extra result beyond the bounded selected set.
    if active_run_records:
        latest_active_record = max(
            active_run_records,
            key=lambda record: (
                record.observation_seq if record.observation_seq is not None else -1,
                record.action_seq if record.action_seq is not None else -1,
                record.call_id,
            ),
        )
        active_run_visible_calls.add(latest_active_record.call_id)
    messages_by_call: dict[str, list[LLMMessage]] = {}
    for message in messages:
        if message.role == "tool" and message.tool_call_id is not None:
            messages_by_call.setdefault(message.tool_call_id, []).append(message)

    def _exact_body_is_present(record_call_id: str) -> bool:
        record = record_by_call.get(record_call_id)
        candidates = messages_by_call.get(record_call_id, [])
        if record is None or len(candidates) != 1:
            return False
        content = candidates[0].content
        receipt = record.receipt

        if rendered_content_matches_receipt(receipt, content):
            return True
        # View.of adds one deterministic presentation prefix after the exact
        # tool body has been rendered. The prefix is not resource content; strip
        # only the seq-selected form, then authenticate the remaining bytes.
        seq = record.observation_seq
        prefix = ("", "Observation: ", "Output: ")[seq % 3] if seq is not None else ""
        return bool(
            prefix
            and content.startswith(prefix)
            and rendered_content_matches_receipt(receipt, content[len(prefix) :])
        )

    physically_present_calls = {
        call_id for call_id in selected_calls if _exact_body_is_present(call_id)
    }
    retained_read_receipts = (
        *prompt_receipt_tuple,
        *(
            record.receipt
            for record in records
            if record.call_id in selected_calls and record.call_id in physically_present_calls
        ),
    )

    # Keep the compatibility parameter deliberately inert until all callers have
    # migrated to exact prompt receipts.
    _ = pinned_full_paths

    # Build the output list: replace only receipt-proven redundant reads.
    out: list[LLMMessage] = []
    for msg in messages:
        if msg.role == "tool" and msg.tool_call_id in file_read_calls:
            path = file_read_calls[msg.tool_call_id]
            record = record_by_call.get(msg.tool_call_id or "")
            if record is None:
                # No revision/range receipt (an old persisted event or a fake
                # executor). Keep its bytes; latest-path guessing is the context
                # loss this transform exists to remove.
                out.append(msg)
                continue
            current_revision = latest_revisions.get(record.receipt.revision.resource)
            if (
                record.call_id in active_run_visible_calls
                and _exact_body_is_present(record.call_id)
                and current_revision == record.receipt.revision
            ):
                out.append(msg)
                continue
            if receipt_covered_in_current_prompt(record.receipt, prompt_receipt_tuple):
                out.append(
                    msg.model_copy(update={"content": _SUPERSEDED_READ_NOTICE.format(path=path)})
                )
                continue
            latest = latest_revisions.get(record.receipt.revision.resource)
            if latest is not None and latest != record.receipt.revision:
                if any(retained.revision == latest for retained in retained_read_receipts):
                    out.append(
                        msg.model_copy(
                            update={
                                "content": _STALE_READ_NOTICE.format(
                                    path=path,
                                    digest=record.receipt.revision.digest[:12],
                                )
                            }
                        )
                    )
                    continue
                out.append(msg)
                continue
            if record.call_id in selected_calls:
                out.append(msg)
                continue
            if receipt_covered_in_current_prompt(
                record.receipt,
                retained_read_receipts,
            ):
                out.append(
                    msg.model_copy(update={"content": _REDUNDANT_READ_NOTICE.format(path=path)})
                )
                continue
            out.append(msg)
            continue
        out.append(msg)
    return out


def _f9_dedupable_read(
    current_tool: str | None,
    current_args: dict | None,
    events: list[Event],
    *,
    readonly_names: frozenset[str] | None,
    window: int = _F9_WINDOW_SIZE,
) -> tuple[bool, str, str]:
    """F9 — return (dedupable, prior_action_id, pointer_text) if the
    current call is an exact repeat of a recent read-only call within
    the window AND nothing has invalidated the prior result since.

    Pure function: reads `events` only. The caller decides whether to
    act on the result and emits the synthetic observation. Assist
    gating is the caller's responsibility — this helper is
    unconditional.

    Args:
        current_tool: name of the tool about to be called.
        current_args: the arguments dict (must equal the prior call's
            args for "exact repeat" — same path, same offset/limit,
            etc.).
        events: the full event list. The CURRENT action is the LAST
            element (it was just emitted before the caller invokes
            this helper); this helper walks events[:-1] to scan only
            PRIOR events.
        readonly_names: frozenset of read-only tool names from the
            executor; None falls back to _WORKSPACE_READ_TOOLS (a
            narrower set — see _readonly_tool_names for the rationale).
        window: max number of recent read-only calls to scan. After
            seeing `window` prior read-only calls without a clean
            match, the helper gives up (window eviction).

    Returns:
        (True, prior_action_id, pointer_text) if dedupable.
        (False, "", "") otherwise.

    The search walks back through PRIOR events, finds the MOST RECENT
    ActionEvent with the same tool name + same args, validates the
    match (successful observation + no mutation since), and either
    returns it or keeps scanning for an older match. The search
    terminates early when the window is exhausted.
    """
    if not current_tool or not isinstance(current_args, dict):
        return (False, "", "")
    # Read-only check: ground on the executor's reported set when
    # available. Falling back to the engine's narrow
    # _WORKSPACE_READ_TOOLS frozenset (file_read) when the executor
    # can't report — the same signal the planner backstop uses.
    if readonly_names is not None:
        if current_tool not in readonly_names:
            return (False, "", "")
    elif current_tool not in _WORKSPACE_READ_TOOLS:
        return (False, "", "")

    # Walk back through PRIOR events. The current action is excluded
    # by indexing events[:-1] (the caller emitted it before invoking
    # us; the dedup is over PAST reads).
    prior_events = events[:-1] if events else []
    ro_calls_seen = 0
    for e in reversed(prior_events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        tool_name = e.tool_call.tool_name
        is_ro = (
            tool_name in readonly_names
            if readonly_names is not None
            else tool_name in _WORKSPACE_READ_TOOLS
        )
        if is_ro:
            ro_calls_seen += 1
            if ro_calls_seen > window:
                break  # window exhausted — give up (F9 eviction)
        # Not a candidate? Skip — keep walking back to find a match.
        if tool_name != current_tool:
            continue
        prior_args = e.tool_call.arguments or {}
        if prior_args != current_args:
            continue
        # Same tool + same args. Validate:
        # 1. Prior action has a successful observation.
        if not _f9_has_successful_observation(events, e.id):
            continue  # failed read; keep looking for an earlier success
        # 2. If the args name a path, no mutation to that path has
        #    occurred between the prior action and now.
        prior_path = prior_args.get("path")
        if isinstance(prior_path, str) and prior_path:
            if _f9_path_was_mutated_after(events, prior_path, e.seq or 0):
                continue  # stale — keep looking for an older clean match
        # Dedupable. Render the pointer.
        pointer = _F9_POINTER_TEMPLATE.format(
            tool_name=current_tool,
            arg_summary=_f9_arg_summary(current_args),
        )
        return (True, e.id, pointer)
    return (False, "", "")


# ---------------------------------------------------------------------------
# W-39 — GATED advisory shell-verify reminder (assist-tier).
#
# A weak model that re-runs an IDEMPOTENT verify it already passed (Dylan saw
# DeepSeek re-run `ls -la`) — the SHELL analogue of the F9 read-loop. F9 +
# collapse_superseded_reads cover ONLY file_read (_WORKSPACE_READ_TOOLS); the
# SHELL channel is NOT covered, so once condensation drops the earlier
# success observation the model loses the evidence that the verify already
# passed and re-runs it (the redundant verify/debug loop).
#
# Unlike F9 (which SKIPS the duplicate read), this is ADVISORY ONLY: a shell
# command may have side effects, so we NEVER auto-skip / block. When a shell
# command is byte-identical to one that produced a SUCCESSFUL observation
# earlier AND nothing in the workspace has MUTATED since (REUSES the A8
# mutating-tool set — the same "no mutation since" signal F9's
# _f9_path_was_mutated_after is built on), we inject ONE system-reminder
# noting it already passed, then the command executes normally. The model
# decides whether to proceed.
#
# Anti-spam: exactly one reminder per passed-and-unmutated streak for a given
# command — once reminded, we do not remind again until a workspace mutation
# resets the streak (a fresh write means re-verifying is legit again).
#
# Mutation tracking is PATH-AGNOSTIC here (a shell command has no single
# `path` arg): ANY mutating tool call after the prior successful run resets
# the streak. F9's _f9_path_was_mutated_after is path-scoped; W-39 needs the
# any-path variant (_w39_latest_mutation_seq) because a shell verify can
# depend on the whole workspace.
#
# GATED on the assist tier by its caller (mirrors F9). Assist OFF
# (capable-model default) → never invoked; byte-identical to today. Even when
# ON the command ALWAYS executes — the only observable change is one extra
# advisory MessageEvent before the (still-executed) call.
# ---------------------------------------------------------------------------

# The shell-exec tools whose re-run is a redundant verify when idempotent.
# Both carry the `command` arg (see ShellTool / ShellExecTool in
# packages/tools/.../builtin). code_exec is intentionally excluded — re-running
# code is more often a deliberate re-execution and it uses a different arg key.
_W39_SHELL_TOOLS = frozenset({"shell", "shell_exec"})

# Window of recent shell calls to scan back through (mirrors _F9_WINDOW_SIZE):
# a re-verify a model loops on is always recent, and a bounded window keeps
# the walk cheap.
_W39_WINDOW_SIZE = 8

# Stable marker so the anti-spam scan can recognize a prior W-39 reminder in
# the event log (and so logs/tests have a single source of truth).
_W39_REMINDER_SENTINEL = "[W-39 verify-dedup]"

# Length cap on the command echoed into the reminder so a giant heredoc/script
# doesn't bloat it — the command is identifiable by its prefix. The SAME
# shortened string is used for the anti-spam match (so truncation can't cause
# a false non-match).
_W39_COMMAND_MAX_CHARS = 200

# Advisory reminder text. ADVISORY ONLY — it never asserts "nothing changed"
# in absolute terms; it puts the judgment on the model ("re-run only if you
# changed something"). format-only (single source of truth).
_W39_REMINDER_TEMPLATE = (
    "<system-reminder>\n"
    "{sentinel} You already ran `{command}` earlier (step {step}) and it "
    "passed, and nothing has been written to the workspace since. Re-run it "
    "only if you have changed something relevant — otherwise act on the "
    "result you already have instead of re-verifying.\n"
    "</system-reminder>"
)


def _w39_short_command(command: str) -> str:
    """W-39 — bound the command echoed into the reminder (and used for the
    anti-spam match) so a giant script doesn't bloat the reminder."""
    s = command.strip()
    if len(s) > _W39_COMMAND_MAX_CHARS:
        s = s[: _W39_COMMAND_MAX_CHARS - 1] + "…"
    return s


def _w39_latest_mutation_seq(events: list[Event], *, before_seq: int | None = None) -> int:
    """W-39 — seq of the MOST RECENT workspace-mutating tool call (the A8
    mutating set: file_write / file_edit / file_append / file_replace_lines /
    file_insert_lines), or 0 if there has been none. Path-agnostic analogue of
    _f9_path_was_mutated_after — a shell verify has no single `path`, so ANY
    file mutation resets the 'already passed' streak. `before_seq` bounds the
    scan to events strictly before the current action (defensive)."""
    latest = 0
    for e in events:
        seq = e.seq or 0
        if before_seq is not None and seq >= before_seq:
            continue
        if (
            isinstance(e, ActionEvent)
            and e.tool_call is not None
            and e.tool_call.tool_name in _WORKSPACE_MUTATING_TOOLS
        ):
            latest = max(latest, seq)
    return latest


def _w39_reminder_emitted_after(events: list[Event], short_command: str, after_seq: int) -> bool:
    """W-39 anti-spam — has a W-39 reminder for `short_command` already been
    emitted with seq > after_seq? Keeps the advisory one-shot per
    passed-and-unmutated streak (no spam if the model loops 3+ times)."""
    for e in events:
        if (e.seq or 0) <= after_seq:
            continue
        if isinstance(e, MessageEvent) and e.message is not None:
            content = e.message.content
            if (
                isinstance(content, str)
                and _W39_REMINDER_SENTINEL in content
                and short_command in content
            ):
                return True
    return False


def _w39_shell_verify_reminder(
    current_tool: str | None,
    current_args: dict | None,
    events: list[Event],
    *,
    window: int = _W39_WINDOW_SIZE,
) -> tuple[bool, int, str]:
    """W-39 — ADVISORY (never skip). Return ``(should_remind, prior_seq,
    reminder_text)`` when the current shell call is byte-identical to a recent
    SUCCESSFUL shell call AND no workspace mutation has happened since AND we
    have not already reminded for this command in the current streak.

    Pure function over ``events`` (the CURRENT action is ``events[-1]``; the
    walk scans ``events[:-1]``). The caller (assist-gated) emits the reminder
    MessageEvent and then executes the command normally — NOTHING is skipped.

    Returns ``(False, 0, "")`` when no reminder should fire (not a shell tool,
    no identical prior success, a mutation since, or already reminded).
    """
    if current_tool not in _W39_SHELL_TOOLS or not isinstance(current_args, dict):
        return (False, 0, "")
    command = current_args.get("command")
    if not isinstance(command, str) or not command:
        return (False, 0, "")

    prior_events = events[:-1] if events else []
    # Streak boundary: the most recent workspace mutation resets the
    # "already passed" streak (0 = no mutation this run). Reused for BOTH the
    # mutation gate and the anti-spam window so they stay consistent.
    streak_start_seq = _w39_latest_mutation_seq(prior_events)

    short_command = _w39_short_command(command)
    shell_calls_seen = 0
    for e in reversed(prior_events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        tool_name = e.tool_call.tool_name
        if tool_name in _W39_SHELL_TOOLS:
            shell_calls_seen += 1
            if shell_calls_seen > window:
                break  # window exhausted — give up (mirrors F9 eviction)
        if tool_name != current_tool:
            continue
        if (e.tool_call.arguments or {}) != current_args:
            continue  # not byte-identical args — keep walking back
        # Same shell tool + identical args. Validate it SUCCEEDED earlier.
        if not _f9_has_successful_observation(events, e.id):
            # Failed / no-observation earlier run — re-running is legit
            # (the bug is re-running PASSED verifies). Keep looking for an
            # earlier identical SUCCESS.
            continue
        prior_seq = e.seq or 0
        # No workspace mutation since the prior successful run? If a write
        # landed after it, re-running the verify is legitimate (test b) —
        # do NOT remind, and an older run would be even more stale.
        if streak_start_seq >= prior_seq:
            return (False, 0, "")
        # Anti-spam: one reminder per passed-and-unmutated streak.
        if _w39_reminder_emitted_after(events, short_command, streak_start_seq):
            return (False, 0, "")
        reminder = _W39_REMINDER_TEMPLATE.format(
            sentinel=_W39_REMINDER_SENTINEL,
            command=short_command,
            step=prior_seq,
        )
        return (True, prior_seq, reminder)
    return (False, 0, "")
