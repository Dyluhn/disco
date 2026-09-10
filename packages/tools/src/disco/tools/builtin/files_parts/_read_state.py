"""Per-conversation read-before-rewrite tracker (F1) and its CD-TOOLS-1 sha-aware
per-path read records for the fresh-edit guard.

Structure: {conv_id: {"read_since_write": set[str]}}
  read_since_write: canonical paths (workspace-prefix-stripped) for which a
    successful FileReadTool.run or success-observation-bearing mutator has
    shown current content for this conversation.
    A write to a path that (a) EXISTS on disk AND (b) is NOT in this set is
    REFUSED — the model must file_read the file first.
    A NEW file (does not exist yet) is always allowed.
Module-level so it's per-process; the per-conversation key keeps state
isolated between agents/sessions. Applies to ALL model tiers (not gated on
ctx.assist) — the thrash root-cause hits capable models too.
"""

from __future__ import annotations

from typing import Any

from ._canonical import _canonical

_read_state: dict[str, dict[str, Any]] = {}


def reset_read_tracker() -> None:
    """Clear all per-conversation read-since-write state. Tests only; not part
    of the tool API."""
    _read_state.clear()


def mark_read(conv_id: str, path: str) -> None:
    """Set the read-since-write bit for `path` in `conv_id` WITHOUT executing a
    real file_read.

    The bit is normally set only inside ``FileReadTool.run`` (an explicit
    model-issued read of disk bytes). But the agent loop can put a file's CURRENT
    content in front of the model by OTHER grounded means that never reach the
    tool:

      * the CURRENT WORKSPACE snapshot pins the file's full, disk-fresh bytes in
        the prompt this turn (view_render ``out_pinned_full``); and
      * the F9 read-dedup short-circuits an identical re-read into a pointer at
        the current bytes (the prior read's result), never calling the executor.

    In BOTH cases the model HAS the file's current content — a subsequent
    file_write is genuinely grounded, NOT a blind rewrite-from-memory. Yet the
    read-before-write gate (keyed on this set) would refuse it because no
    ``FileReadTool.run`` fired, leaving the model unable to read (it only gets a
    pointer/snapshot) AND unable to write (gated) → the unrecoverable
    file_write loop that drove the live shell-exec fallback. The loop calls this
    to record those snapshot-/dedup-backed grounded reads so the gate clears.

    Canonicalizes the path the same way the gate does, so a snapshot path and a
    later write spelling resolve to one key."""
    if not isinstance(path, str) or not path:
        return
    _conv_state(conv_id)["read_since_write"].add(_canonical(path))


def clear_conversation_read_state(conv_id: str) -> None:
    """Remove the F1 tracker entry for a single conversation.

    Called by DefaultToolExecutor.kill() so the module-level dict does not
    grow unbounded in long-running processes (each killed executor cleans up
    its own per-conversation bucket).  The per-conversation-id key design
    already prevents cross-conversation leaks; this call prevents the dict
    from accumulating dead entries indefinitely.

    Tests should use reset_read_tracker() for a full clear between runs."""
    _read_state.pop(conv_id, None)


def _conv_state(conv_id: str) -> dict[str, Any]:
    s = _read_state.get(conv_id)
    if s is None:
        # read_since_write: the coarse F1 bit (path has current visible grounding).
        # reads: CD-TOOLS-1 sha-aware per-path read records for the fresh-edit guard —
        #   {canonical_path: {"sha": <full-file sha at read>, "ranges": [(start,end)],
        #    "full": bool, "anchored_full": bool}}. `ranges` are 1-based inclusive
        #   line spans the model SAW
        #   un-elided (a real file_read page or mutator success view); `full` is True
        #   when that numbered view covers the whole file. `anchored_full` records
        #   complete textual knowledge from model-authored bytes across owned
        #   mutations; it can ground text-anchored edits, never line-number edits.
        # targeted_read_grounded: reads[path] came from model-visible numbered content that can
        # ground targeted edits. Refusal-delivered reads set this without read_since_write;
        # successful mutators set both.
        s = {
            "read_since_write": set(),
            "edit_grounded": set(),
            "targeted_read_grounded": set(),
            "no_op_edit_counts": {},
            "old_text_not_found_counts": {},
            "reads": {},
        }
        _read_state[conv_id] = s
    s.setdefault("reads", {})  # back-compat for buckets created before CD-TOOLS-1
    s.setdefault("edit_grounded", set())  # back-compat for buckets created before REL-RC-D
    s.setdefault("targeted_read_grounded", set())  # back-compat for buckets created before REL-RC-G
    s.setdefault("no_op_edit_counts", {})  # back-compat for buckets created before REL-RC-L
    s.setdefault("old_text_not_found_counts", {})  # back-compat for buckets created before REL-6
    return s


def _increment_no_op_edit_count(conv_id: str, path: str) -> int:
    counts = _conv_state(conv_id)["no_op_edit_counts"]
    canon = _canonical(path)
    count = int(counts.get(canon, 0)) + 1
    counts[canon] = count
    return count


def _increment_old_text_not_found_count(conv_id: str, path: str) -> int:
    counts = _conv_state(conv_id)["old_text_not_found_counts"]
    canon = _canonical(path)
    count = int(counts.get(canon, 0)) + 1
    counts[canon] = count
    return count


def _clear_grounding(conv_id: str, path: str) -> None:
    """[REL-RC-D/G] Fully un-ground `path`: drop the read-since-write, edit-grounded, and
    targeted-read bits. Used for mutating paths that do not return a current numbered
    observation (for example safe_write_file and external run_script edits)."""
    canon = _canonical(path)
    st = _conv_state(conv_id)
    st["read_since_write"].discard(canon)
    st["edit_grounded"].discard(canon)
    st["targeted_read_grounded"].discard(canon)
    st["no_op_edit_counts"].pop(canon, None)
    st["old_text_not_found_counts"].pop(canon, None)


def record_read(
    conv_id: str, path: str, *, sha: str, start_line: int, end_line: int, full: bool
) -> None:
    """CD-TOOLS-1: record that the model SAW lines [start_line, end_line] of `path`'s
    CURRENT bytes (sha) un-elided. A new sha (the file changed) resets the ranges."""
    if not isinstance(path, str) or not path:
        return
    reads = _conv_state(conv_id)["reads"]
    key = _canonical(path)
    rec = reads.get(key)
    if rec is None or rec.get("sha") != sha:
        rec = {"sha": sha, "ranges": [], "full": False}
        reads[key] = rec
    rec["ranges"].append((int(start_line), int(end_line)))
    if full:
        rec["full"] = True


def _covers(ranges: list[tuple[int, int]], lo: int, hi: int) -> bool:
    """True if the line span [lo,hi] is fully inside the union of shown ranges."""
    need = set(range(lo, hi + 1))
    for a, b in ranges:
        need -= set(range(a, b + 1))
        if not need:
            return True
    return not need
