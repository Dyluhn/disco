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
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    WorkspaceMutationEvent,  # noqa: F401 — compatibility facade binding
)
from ..receipt_currency import latest_currency_boundary_seq
from ..script_identity import (
    SHELL_TOOLS,
    describe_script_fingerprint,
    foreground_script_fingerprints,
)
from ..tool_fingerprint import tool_call_fingerprint
from .resource_context import (  # noqa: F401 — compatibility facade bindings
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


def _f8_scan_events(
    events: list[Event],
) -> tuple[dict[str, ActionEvent], set[str], set[str]]:
    """Scan events for file_write actions, confirmed call ids, and failed call ids."""
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
    return actions_by_call, confirmed_call_ids, failed_call_ids


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
    actions_by_call, confirmed_call_ids, failed_call_ids = _f8_scan_events(events)
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
_F9_POINTER_SENTINEL = "[F9 dedup:"
_F9_POINTER_TEMPLATE = (
    _F9_POINTER_SENTINEL + " {tool_name}({arg_summary}) identical to a recent read "
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
    from .dedup_projection import _ReadProjection, collapse_one_message

    projection = _ReadProjection(events, messages, tuple(prompt_receipts))
    if not projection.file_read_calls:
        return messages
    _ = pinned_full_paths  # compatibility seam; deliberately inert
    return [collapse_one_message(msg, projection) for msg in messages]


def _f9_is_readonly(tool_name: str, readonly_names: frozenset[str] | None) -> bool:
    if readonly_names is not None:
        return tool_name in readonly_names
    return tool_name in _WORKSPACE_READ_TOOLS


def _f9_candidate_is_valid(
    e: ActionEvent,
    events: list[Event],
    current_tool: str,
    current_args: dict,
) -> bool:
    """True when a prior action is a valid dedup candidate.

    Same tool/args, successful observation, no path mutation since.
    """
    if e.tool_call.tool_name != current_tool:
        return False
    prior_args = e.tool_call.arguments or {}
    if prior_args != current_args:
        return False
    if not _f9_has_successful_observation(events, e.id):
        return False
    prior_path = prior_args.get("path")
    if isinstance(prior_path, str) and prior_path:
        if _f9_path_was_mutated_after(events, prior_path, e.seq or 0):
            return False
    return True


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
    if not _f9_is_readonly(current_tool, readonly_names):
        return (False, "", "")

    prior_events = events[:-1] if events else []
    ro_calls_seen = 0
    for e in reversed(prior_events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        tool_name = e.tool_call.tool_name
        if _f9_is_readonly(tool_name, readonly_names):
            ro_calls_seen += 1
            if ro_calls_seen > window:
                break  # window exhausted — give up (F9 eviction)
        if not _f9_candidate_is_valid(e, events, current_tool, current_args):
            continue
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
# Anti-spam: one reminder per repetition OCCURRENCE. Once reminded about a given
# prior run, we do not repeat that advice — but if the model repeats AGAIN after
# being told, the repeat it just made is a new occurrence and the advisory
# re-arms once more. A workspace mutation resets the whole streak regardless (a
# fresh write means re-verifying is legit again).
#
# This read "exactly one reminder per passed-and-unmutated streak" until
# 2026-08-06x, and that window was the F47 defect: the streak runs from the last
# mutation, while `ThrashOracle` counts CONSECUTIVE identical actions. When the
# two diverge the counted group can run silent — `p4_ff_node_restart` @97652 spent
# its one shot at seq 261 and was then faulted for the group at 275/284/287,
# having been told nothing inside it. Pivoting on the occurrence keeps the
# advisory bounded while guaranteeing a notice precedes the repeat that trips
# the cap.
#
# TWO classes are covered, not one (F47). Byte-identical calls
# (`tool_call_fingerprint`), and re-runs of the same SCRIPT under a different
# command spelling (`disco.core.script_identity`) — the class `ThrashOracle`
# grades `repeated_semantic_shell_verification` on, which this memo could not
# see until the identity owner moved down into `disco.core`.
#
# Mutation tracking is PATH-AGNOSTIC here (a shell command has no single
# `path` arg): ANY mutating tool call after the prior successful run resets
# the streak. F9's _f9_path_was_mutated_after is path-scoped; W-39 needs the
# any-path variant (_w39_latest_mutation_seq) because a shell verify can
# depend on the whole workspace.
#
# NOT gated on the assist tier — deliberately, and unlike F9's read-dedup
# above. The caller's guard is
# `if not loop._assist and call.tool_name not in _W39_SHELL_TOOLS: return`
# (`loop/observation_execution.py::_prepare_observation`), so a SHELL call
# reaches this memo with assist OFF; only non-shell calls take the early
# return. A6.3/A11 un-gated it on purpose: assist is OFF for capable models,
# which is exactly the population register #5's three firings came from, so an
# assist-gated memo would have been dead code in every canary that reproduced
# the defect. That caller's docstring is the authority for the split.
# Either way the command ALWAYS executes — the only observable change is one
# extra advisory MessageEvent before the (still-executed) call.
#
# This block said the opposite until 2026-08-06e (finding F26): it claimed the
# memo was assist-gated and "never invoked" on the capable-model default. Do
# not restore that reading from memory — check the caller.
# ---------------------------------------------------------------------------

# The shell-exec tools whose re-run is a redundant verify when idempotent.
# Both carry the `command` arg (see ShellTool / ShellExecTool in
# packages/tools/.../builtin). code_exec is intentionally excluded — re-running
# code is more often a deliberate re-execution and it uses a different arg key.
# Bound to the shared owner (2026-08-06x, F47) rather than re-declared, so the
# memo and the oracle cannot disagree about which tools carry a shell command.
_W39_SHELL_TOOLS = SHELL_TOOLS

# Window of recent shell calls to scan back through (mirrors _F9_WINDOW_SIZE):
# a re-verify a model loops on is always recent, and a bounded window keeps
# the walk cheap.
_W39_WINDOW_SIZE = 8

# Stable marker so the anti-spam scan can recognize a prior W-39 reminder in
# the event log (and so logs/tests have a single source of truth).
_W39_REMINDER_SENTINEL = "[W-39 verify-dedup]"

# F47 (2026-08-06x) — the sentinel for the SCRIPT-identity class. Deliberately a
# distinct string rather than a suffix on the same one: the anti-spam scan is a
# substring match, and `"[W-39 verify-dedup]" in "[W-39 verify-dedup:script]…"`
# is False because of the bracket, so the two classes cannot suppress each other.
_W39_SCRIPT_REMINDER_SENTINEL = "[W-39 verify-dedup:script]"

# Length cap on the command echoed into the reminder so a giant heredoc/script
# doesn't bloat it — the command is identifiable by its prefix. The SAME
# shortened string is used for the anti-spam match (so truncation can't cause
# a false non-match).
_W39_COMMAND_MAX_CHARS = 200

# Length cap on the PRIOR RESULT echoed back. Larger than the command cap
# because the result is the actual signal — the model is being handed the answer
# it already has — but still bounded so a build log is not re-inlined whole.
_W39_RESULT_MAX_CHARS = 800

# Advisory reminder text. ADVISORY ONLY — it never asserts "nothing changed"
# in absolute terms; it puts the judgment on the model ("re-run only if you
# changed something"). format-only (single source of truth).
#
# 2026-08-06z (GROUNDED FEEDBACK constraints 1 and 3): the claim is now scoped to
# what the sentence can actually support. It used to read "and nothing has
# changed since", which is an absolute claim about the world; what the product
# knows is that the RECORD shows no currency boundary since that run — the same
# predicate, and the same evidence, the repeat-cap will be graded by. Saying the
# larger thing was the F49 error one dimension over (a claim asserted wider than
# its warrant), and the narrower sentence is strictly more useful to the agent
# because it names WHERE the assurance comes from.
_W39_REMINDER_TEMPLATE = (
    "<system-reminder>\n"
    "{sentinel} You already ran `{command}` earlier (step {step}) and it "
    "passed, and this run's record shows no change since then. Re-run it "
    "only if you have changed something relevant — otherwise act on the "
    "result you already have instead of re-verifying.{result}\n"
    "</system-reminder>"
)

# F47 (2026-08-06x) — the SCRIPT-class reminder. The command the agent is about
# to issue is NOT byte-identical to the one it repeats, so the text names the
# SCRIPT (the question) and the earlier command (what it actually ran) rather
# than pretending the two commands were the same. Same advisory posture: the
# judgment stays with the model and the command still executes.
_W39_SCRIPT_REMINDER_TEMPLATE = (
    "<system-reminder>\n"
    "{sentinel} You already ran `{script}` at step {step} (as `{command}`) and it "
    "passed, and this run's record shows no change since then. Spelling the command "
    "differently asks the same question. Re-run it only if you have changed "
    "something relevant — otherwise act on the result you already have instead of "
    "re-verifying.{result}\n"
    "</system-reminder>"
)

# The prior result block, appended only when a result was actually captured.
# A6.3 §2's sentence is "here is that result"; an empty block would promise one
# and deliver nothing.
_W39_RESULT_BLOCK = "\n\nThat run produced:\n{result}"


def _w39_short_command(command: str) -> str:
    """W-39 — bound the command echoed into the reminder (and used for the
    anti-spam match) so a giant script doesn't bloat the reminder."""
    s = command.strip()
    if len(s) > _W39_COMMAND_MAX_CHARS:
        s = s[: _W39_COMMAND_MAX_CHARS - 1] + "…"
    return s


def _w39_prior_result_excerpt(events: list[Event], action_id: str) -> str:
    """A6.3 §2 — the prior RESULT, not merely the fact that a prior run passed.

    The memo's governing sentence is *"you ran this exact command at seq N;
    nothing has changed since; **here is that result**"*. Without the result the
    model is told an answer exists but not what it was, which is a weaker signal
    than the one the design specifies — and in every one of register #5's three
    firings the model already HAD a usable answer. Bounded so a large build log
    never lands in the prompt twice.
    """
    for e in events:
        if isinstance(e, ObservationEvent) and e.action_id == action_id:
            content = e.tool_result.content
            if not isinstance(content, str) or not content.strip():
                return ""
            text = content.strip()
            if len(text) > _W39_RESULT_MAX_CHARS:
                text = text[: _W39_RESULT_MAX_CHARS - 1] + "…"
            return text
    return ""


def _currency_view(event: Event) -> dict[str, object]:
    """Project ONE typed event into the serialized shape the shared currency
    owner reads (`disco.core.receipt_currency`).

    An adapter, not a rule: it moves values, it never decides anything. Every
    decision belongs to the owner, so the memo and the oracle cannot disagree.
    Only the keys the owner reads are projected — this runs per event per shell
    call, and a full `model_dump()` would pay for the whole payload to answer a
    six-field question.
    """
    view: dict[str, object] = {"kind": event.kind.value, "seq": event.seq or 0, "id": event.id}
    source = getattr(event, "source", None)
    if source is not None:
        view["source"] = source.value if isinstance(source, EventSource) else str(source)
    if isinstance(event, ActionEvent) and event.tool_call is not None:
        view["tool_call"] = {
            "tool_name": event.tool_call.tool_name,
            "arguments": event.tool_call.arguments,
        }
    if isinstance(event, ObservationEvent):
        view["action_id"] = event.action_id
        result = event.tool_result
        view["tool_result"] = {
            "success": result.success,
            "tool_name": result.tool_name,
            "structured": result.structured if isinstance(result.structured, dict) else None,
        }
    if isinstance(event, MessageEvent):
        view["meta"] = event.meta if isinstance(event.meta, dict) else {}
    if isinstance(event, StatusEvent):
        view["detail"] = event.detail
        transition = event.plan_verification_transition
        view["plan_verification_transition"] = (
            transition.model_dump(mode="json") if transition is not None else None
        )
    return view


def _w39_freshness_boundary_seq(events: list[Event], *, before_seq: int | None = None) -> int:
    """A6.3 §3.2 — seq of the most recent event that ended the prior result's currency.

    **This is no longer a loop-side counterpart of the oracle's boundary — it is
    the SAME predicate** (`disco.core.receipt_currency`, 2026-08-06z, GROUNDED
    FEEDBACK constraint 2: *"one currency predicate, two consumers"*).

    It previously counted three boundary kinds and deliberately skipped two the
    oracle had (approved plan-predicate scope, trusted mutation receipt) on the
    grounds that reproducing harness adjudication here would be the second
    implementation A6.3 §3 forbids. That reasoning was right about the hazard and
    wrong about the remedy: the answer is not for the memo to know less, it is
    for neither side to own the rule. Both now import it.

    Two gaps close, and neither was hypothetical. Skipping those two kinds let the
    memo assert "nothing has changed since" across an epoch the grader had already
    closed — a false assurance, which W-39's own contract forbids. And treating a
    bare workspace-mutating ACTION as a boundary silenced the memo while the cap,
    which resets only on a TRUSTED mutation receipt, kept counting: silent because
    the answer looked stale, counted because the cap disagreed. That is the F47
    shape, and it is the gap that actually fired.

    Note the direction. The echo now goes stale LESS eagerly, which means it
    speaks in exactly the window it is graded against. It does not thereby claim
    more than it knows: the reminder text is a projection of this decision (see
    `_W39_REMINDER_TEMPLATE`), and it now says what the record establishes — no
    change OF RECORD since the earlier run — rather than the absolute "nothing has
    changed" it used to assert.

    The remaining conservatism is the ``window`` bound in
    `_w39_shell_verify_reminder`, which is a scan limit, not a disagreement about
    the world.
    """
    return latest_currency_boundary_seq(
        (_currency_view(e) for e in events),
        before_seq=before_seq,
        failed_action_ids=_failed_action_ids(events),
    )


def _failed_action_ids(events: list[Event]) -> frozenset[str]:
    """Ids of actions whose failure is ON THE RECORD.

    The loop's half of the outcome map the oracle already builds
    (`_thrash_checks.action_outcomes`). The shared currency owner needs it because
    "did that write actually land?" is not answerable from the action event alone.

    Positively-recorded failures ONLY. An action with no observation yet is
    UNKNOWN, and unknown must not be read as "changed nothing" — that is the
    false-assurance direction W-39 promises never to take.
    """
    return frozenset(
        e.action_id
        for e in events
        if isinstance(e, ObservationEvent) and not e.tool_result.success and e.action_id
    )


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


def _w39_reminder_emitted_after(
    events: list[Event], sentinel: str, key: str, after_seq: int
) -> bool:
    """W-39 anti-spam — has a reminder of THIS class naming `key` already been
    emitted with seq > after_seq?

    **The pivot `after_seq` is the OCCURRENCE being pointed at, not the freshness
    streak start (F47, 2026-08-06x).** The two are different windows and the gap
    between them is what killed `p4_ff_node_restart` @97652: the streak start was
    a workspace mutation far upstream, the memo spent its one shot at seq 261,
    and the streak the oracle actually counted — 275/284/287 — ran silent, so the
    run was faulted for ignoring advice the product never gave inside the group.

    Pivoting on the occurrence keeps the advisory one-shot per repetition (no
    spam when the model loops) while re-arming it whenever the model repeats
    AGAIN after being told: the repeat it just made is a new occurrence, later
    than the last reminder, so the guard opens exactly once more.
    """
    for e in events:
        if (e.seq or 0) <= after_seq:
            continue
        if isinstance(e, MessageEvent) and e.message is not None:
            content = e.message.content
            if isinstance(content, str) and sentinel in content and key in content:
                return True
    return False


def _w39_candidate_command(e: ActionEvent, *, exclude_verify_probe: bool) -> str | None:
    """The shell command of a prior action, or None if it is not a candidate.

    ``exclude_verify_probe`` mirrors the oracle's `_script_command`, which drops
    `verify_probe` actions from the semantic-shell group. The memo must group the
    same population or the coverage duty is not met. The EXACT-call class carries
    no such exclusion in the oracle (`_longest_identical_streak` counts every
    action), so it passes False and stays byte-identical to pre-F47 behaviour.
    """
    if e.tool_call is None or e.tool_call.tool_name not in _W39_SHELL_TOOLS:
        return None
    if exclude_verify_probe:
        meta = getattr(e, "meta", None)
        if isinstance(meta, dict) and meta.get("verify_probe"):
            return None
    command = e.tool_call.arguments.get("command")
    return command if isinstance(command, str) and command else None


def _w39_matched_class(
    e: ActionEvent,
    *,
    current_fingerprint: str,
    current_script_fingerprints: frozenset[str],
    short_command: str,
) -> tuple[str, str] | None:
    """Which answered-question class this prior call shares with the current one.

    Returns ``(sentinel, anti_spam_key)``, or None. The two classes are exactly
    the two `ThrashOracle` counts answered-question repetitions on, and both
    resolve their identity through `disco.core` so the memo and the oracle cannot
    group differently (A6.3 §3.1, extended to the second class at F47):

    * **exact call** — `tool_call_fingerprint`. Checked first, so whenever the
      pre-F47 memo would have fired, this one fires with the same text.
    * **same script** — `foreground_script_fingerprints`. The class that faulted
      `diag_script_run` @97903, where one script ran under three spellings and
      the memo, keyed only on byte-identical calls, never saw it.
    """
    if e.tool_call is None:
        return None
    exact = _w39_candidate_command(e, exclude_verify_probe=False)
    if exact is not None and (
        tool_call_fingerprint(e.tool_call.tool_name, e.tool_call.arguments) == current_fingerprint
    ):
        return (_W39_REMINDER_SENTINEL, short_command)
    if not current_script_fingerprints:
        return None
    candidate = _w39_candidate_command(e, exclude_verify_probe=True)
    if candidate is None:
        return None
    shared = current_script_fingerprints & foreground_script_fingerprints(candidate)
    if not shared:
        return None
    return (_W39_SCRIPT_REMINDER_SENTINEL, describe_script_fingerprint(sorted(shared)[0]))


def _w39_reminder_text(
    e: ActionEvent,
    events: list[Event],
    *,
    sentinel: str,
    key: str,
    prior_seq: int,
    short_command: str,
) -> str:
    prior_result = _w39_prior_result_excerpt(events, e.id)
    result = _W39_RESULT_BLOCK.format(result=prior_result) if prior_result else ""
    if sentinel == _W39_REMINDER_SENTINEL:
        return _W39_REMINDER_TEMPLATE.format(
            sentinel=sentinel,
            command=short_command,
            step=prior_seq,
            result=result,
        )
    prior_command = _w39_candidate_command(e, exclude_verify_probe=True) or ""
    return _W39_SCRIPT_REMINDER_TEMPLATE.format(
        sentinel=sentinel,
        script=key,
        command=_w39_short_command(prior_command),
        step=prior_seq,
        result=result,
    )


def _w39_validate_candidate(
    e: ActionEvent,
    events: list[Event],
    current_fingerprint: str,
    streak_start_seq: int,
    short_command: str,
    current_script_fingerprints: frozenset[str] = frozenset(),
) -> tuple[bool, int, str, str]:
    """Validate a prior shell call; return (valid, prior_seq, sentinel, key).

    Identity comes from the SHARED owners the `ThrashOracle` grades runs with
    (A6.3 §3.1, binding): `disco.core.tool_fingerprint` for the exact-call class
    and `disco.core.script_identity` for the script class. Neither is
    reimplemented here. A memo firing on a different equivalence class than the
    oracle that grades the run is worse than no memo — the disagreement stays
    invisible until a canary fires on a class the memo never saw, which is
    precisely what 2026-08-06v measured.
    """
    matched = _w39_matched_class(
        e,
        current_fingerprint=current_fingerprint,
        current_script_fingerprints=current_script_fingerprints,
        short_command=short_command,
    )
    if matched is None:
        return (False, 0, "", "")
    sentinel, key = matched
    if not _f9_has_successful_observation(events, e.id):
        return (False, 0, "", "")
    prior_seq = e.seq or 0
    if streak_start_seq >= prior_seq:
        return (False, 0, "", "")
    if _w39_reminder_emitted_after(events, sentinel, key, prior_seq):
        return (False, 0, "", "")
    return (True, prior_seq, sentinel, key)


# F47 (2026-08-07f) — the plan-tracking tools brought under the notice invariant.
# The RENDERING lives in `dedup_plan_notice.py`; only the class declarations stay
# here, beside `_W39_SHELL_TOOLS`, so every tool class the notice covers is
# declared in one place. See that module for why this class exists at all.
#
# `propose_plan_update` and `submit_plan` are deliberately excluded:
# `meta_tool_planning._auto_approve_revision` already emits its own
# identical-revision nudge, and a second notice would double-fire on one event.
_W39_PLAN_TOOLS = frozenset({"plan_step", "update_plan_progress"})

# Every tool class that can produce an answered-question notice. This is the set
# `_prepare_observation` gates on, so widening the notice means adding here — one
# place, rather than a second guard that can drift from this one.
_W39_NOTICE_TOOLS = _W39_SHELL_TOOLS | _W39_PLAN_TOOLS


def _w39_shell_verify_reminder(
    current_tool: str | None,
    current_args: dict | None,
    events: list[Event],
    *,
    window: int = _W39_WINDOW_SIZE,
) -> tuple[bool, int, str]:
    """W-39 — ADVISORY (never skip). Return ``(should_remind, prior_seq,
    reminder_text)`` when the current shell call repeats a recent SUCCESSFUL
    shell call — byte-identically, or by re-running the same script under a
    different spelling — AND no workspace mutation has happened since AND we have
    not already reminded for that occurrence.

    Pure function over ``events`` (the CURRENT action is ``events[-1]``; the
    walk scans ``events[:-1]``). The caller emits the reminder MessageEvent and
    then executes the command normally — NOTHING is skipped.

    **Bounded conservatism, stated rather than hidden.** Two things make this
    predicate narrower than the oracle's counter, and both fail toward silence
    rather than toward a false assurance:

    * the freshness boundary resets on ANY workspace mutation, where the oracle's
      semantic group resets only on a mutation of the script's own language
      family;
    * the walk gives up after ``window`` shell calls, where the oracle scans the
      whole run.

    So the memo can miss a repeat the oracle counts. It can never claim "nothing
    has changed" when something has.

    Returns ``(False, 0, "")`` when no reminder should fire.
    """
    if current_tool not in _W39_SHELL_TOOLS or not isinstance(current_args, dict):
        return (False, 0, "")
    command = current_args.get("command")
    if not isinstance(command, str) or not command:
        return (False, 0, "")

    prior_events = events[:-1] if events else []
    streak_start_seq = _w39_freshness_boundary_seq(prior_events)
    short_command = _w39_short_command(command)
    current_fingerprint = tool_call_fingerprint(current_tool, current_args)
    current_script_fingerprints = foreground_script_fingerprints(command)
    shell_calls_seen = 0
    for e in reversed(prior_events):
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        tool_name = e.tool_call.tool_name
        if tool_name in _W39_SHELL_TOOLS:
            shell_calls_seen += 1
            if shell_calls_seen > window:
                break  # window exhausted — give up (mirrors F9 eviction)
        valid, prior_seq, sentinel, key = _w39_validate_candidate(
            e,
            events,
            current_fingerprint,
            streak_start_seq,
            short_command,
            current_script_fingerprints,
        )
        if not valid:
            continue
        return (
            True,
            prior_seq,
            _w39_reminder_text(
                e,
                events,
                sentinel=sentinel,
                key=key,
                prior_seq=prior_seq,
                short_command=short_command,
            ),
        )
    return (False, 0, "")
