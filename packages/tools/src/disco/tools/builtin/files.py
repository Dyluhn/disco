"""Dedicated file tools — tool-sandbox-contract.md §9 [OH: file_rules].

Dedicated file tools, NOT shell redirection — this sidesteps the string-escaping
failures of piping model output through bash. All operate within the sandbox
instance's jailed workspace (the instance rejects path escapes).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..sandbox.base import strip_redundant_workspace_prefix

_FS = frozenset({Capability.FILESYSTEM})

# A read result longer than the observation snip cap (events._OBS_SNIP_CHARS=8000)
# gets destructively snipped (head+tail) at context ingestion, leaving the model a
# corrupted middle — so it re-reads forever and never commits an edit (observed
# live). So file_read PAGES by a CHARACTER budget kept safely under that cap: each
# read returns intact, line-numbered lines + an explicit "read more with offset=…".
_READ_CHAR_BUDGET = 7_000

# F7 — pressure-aware head-only.
#
# LIMITATION: ToolContext exposes `assist` but not the condenser's token_count
# pressure signal (that lives in engine.py / view.py and isn't threaded into the
# tool call). So we gate on `assist=True` + a LARGE-FILE SIZE HEURISTIC: files
# whose total content exceeds twice the read char budget are the ones that
# today's paging has to split across multiple pages anyway — exactly the
# payload size that gets most-corrupted by the snip pass and most-likely to
# push the model into the next condenser tick. For those, we return only a
# small HEAD slice and a prescriptive directive (grep, then file_read a
# targeted line range) so the model stops dumping whole files into context
# when it's already close to the limit. Assist-OFF behavior is unchanged.
_PRESSURE_HEAD_BUDGET = 2_000  # head-only slice when the gate fires (well under the snip cap)
# 14_000 — files that would otherwise span multiple pages
_PRESSURE_FILE_THRESHOLD = _READ_CHAR_BUDGET * 2
# Prescriptive directive: "the file is big; don't read it whole, do this instead."
# Static so a unit test can assert on it; worded so the weak-model tier picks
# the cheap, deterministic path (grep → targeted read) over a full dump.
_PRESSURE_DIRECTIVE = (
    "file is large; the full page is suppressed. Do NOT re-read this file "
    "whole — pick the symbol/class/section you need, then either:\n"
    "  (a) run a grep/ripgrep tool to locate it (e.g. `rg -n 'Symbol' <path>`), "
    "then file_read a targeted line range (`offset=<n>, limit=<m>`); or\n"
    "  (b) file_read a small line range directly to scan structure.\n"
    "Reading this file whole again will just be truncated the same way."
)

# A line-number prefix the model may have copied out of a numbered file_read
# ("  123\t<code>"). file_edit strips it defensively so a paste-back still matches.
_LINENO_PREFIX = re.compile(r"(?m)^\s*\d+\t")

# ROOT-2 (slides spiral): generated BINARY deliverables. file_write only ever writes
# TEXT (args.content is a str), so a file_write targeting an EXISTING file of one of
# these types would CORRUPT the artifact. The confused post-generation agent tried to
# file_write content="placeholder" into a finished .pptx repeatedly (a core driver of
# the spiral) — refuse it with a clear, terminal message so the agent stops and
# recognises the deliverable is already produced. A NEW file is never blocked.
_BINARY_DELIVERABLE_EXTS = frozenset(
    {"pptx", "pdf", "docx", "xlsx", "png", "jpg", "jpeg", "gif", "mp3", "mp4", "wav", "zip"}
)

# Per-conversation read-before-rewrite tracker (F1).
# Structure: {conv_id: {"read_since_write": set[str]}}
#   read_since_write: canonical paths (workspace-prefix-stripped) for which a
#     successful FileReadTool.run has occurred since the path's last successful
#     mutation (file_write / file_append / file_edit / file_replace_lines /
#     file_insert_lines / file_str_replace) in this conversation.
#     A write to a path that (a) EXISTS on disk AND (b) is NOT in this set is
#     REFUSED — the model must file_read the file first.
#     A NEW file (does not exist yet) is always allowed.
# Module-level so it's per-process; the per-conversation key keeps state
# isolated between agents/sessions. Applies to ALL model tiers (not gated on
# ctx.assist) — the thrash root-cause hits capable models too.
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
        # read_since_write: the coarse F1 bit (path grounded since last write).
        # reads: CD-TOOLS-1 sha-aware per-path read records for the fresh-edit guard —
        #   {canonical_path: {"sha": <full-file sha at read>, "ranges": [(start,end)],
        #    "full": bool}}. `ranges` are 1-based inclusive line spans the model SAW
        #   un-elided (a real file_read page); `full` is True for a whole-file read.
        # targeted_read_grounded: reads[path] came from model-visible numbered content that can
        # ground targeted edits without granting read_since_write / blind file_write.
        s = {
            "read_since_write": set(),
            "edit_grounded": set(),
            "targeted_read_grounded": set(),
            "reads": {},
        }
        _read_state[conv_id] = s
    s.setdefault("reads", {})  # back-compat for buckets created before CD-TOOLS-1
    s.setdefault("edit_grounded", set())  # back-compat for buckets created before REL-RC-D
    s.setdefault("targeted_read_grounded", set())  # back-compat for buckets created before REL-RC-G
    return s


def _clear_grounding(conv_id: str, path: str) -> None:
    """[REL-RC-D/G] Fully un-ground `path`: drop the read-since-write, edit-grounded, and
    targeted-read bits. Called by every line-shifting/non-anchored mutation (full/blind file_write &
    file_append & safe_write, line-shifting file_replace_lines & file_insert_lines, and external
    run_script edits) — after those the model must get fresh content before it can edit or rewrite
    again."""
    canon = _canonical(path)
    st = _conv_state(conv_id)
    st["read_since_write"].discard(canon)
    st["edit_grounded"].discard(canon)
    st["targeted_read_grounded"].discard(canon)


# CD-TOOLS-1 — the internal elision-marker family, re-expressed locally so the tools
# package does not import from disco.core (layering). Mirrors core.events
# _ELISION_MARKER_RE / _ELISION_PARAPHRASE_RE: an "<N chars … elided|full content …>"
# render marker the model must never echo back into source.
_EDIT_ELISION_RE = re.compile(
    r"<\s*\d[\d,]*\s*chars\b[^>]*?\b(?:elided|full\s+content|placeholder)\b[^>]*>",
    re.IGNORECASE,
)

# CD-TOOLS-1: the fresh-read REQUIREMENT applies only to files large enough that their content
# is elided from the model's context view — i.e. over the arg-snip threshold (core.events
# _ARG_SNIP_CHARS = 1500). Below it, a file_write's body / a read stays in context un-elided, so
# the model reliably has the bytes and reproducing `old` is safe — requiring a read there would
# only break legitimate small-file edits (Mode B is a LARGE-file phenomenon). The elision-MARKER
# rejection is unconditional (a marker is never valid source, at any size).
_GUARD_FRESH_READ_MIN_BYTES = 1_500
_REFUSAL_READ_FULL_MAX_BYTES = 64 * 1024
_LINE_REFUSAL_WINDOW_RADIUS = 40
_LINE_SUCCESS_WINDOW_RADIUS = 40


def _has_elision_marker(*texts: str | None) -> bool:
    return any(t is not None and _EDIT_ELISION_RE.search(t) is not None for t in texts)


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


def _fresh_read_required(path: str, why: str) -> ToolOutcome:
    return ToolOutcome(
        success=False,
        error="FRESH_READ_REQUIRED",
        content=(
            f"Edit refused — {why}. Read {path} first (file_read), then edit using the exact "
            "text you see. (This is a corrective nudge, not a failure — nothing was changed.)"
        ),
        structured={
            "kind": "fresh_read_required",
            "path": path,
            "reason": why,
            "next_required_action": "file_read",
            "suggested_args": {"path": path},
        },
    )


def _line_span(total: int, attempted: tuple[int, int] | None, radius: int) -> tuple[int, int]:
    """Return a 1-based inclusive line window clamped to the file."""
    if total <= 0:
        return (1, 1)
    if attempted is None:
        lo = hi = 1
    else:
        lo = max(1, min(attempted[0], total))
        hi = max(lo, min(attempted[1], total))
    return (max(1, lo - radius), min(total, hi + radius))


def _numbered_line_window(
    text: str, *, start_line: int, end_line: int, total_lines: int
) -> str:
    lines = text.splitlines()
    if total_lines <= 0:
        return ""
    window = "\n".join(lines[start_line - 1 : end_line])
    return _number_lines(window, start_line)


def _line_refusal_read(
    conv_id: str,
    path: str,
    *,
    current_bytes: bytes,
    sha: str,
    attempted_lines: tuple[int, int] | None,
) -> tuple[str, dict[str, Any]]:
    """Render and record the fresh content carried by a line-edit refusal.

    The refusal is a real read for future targeted edits, but it deliberately does NOT set
    read_since_write, so a blind file_write remains blocked until an explicit file_read.
    """
    text = current_bytes.decode("utf-8", errors="replace")
    total = len(text.splitlines())
    full = len(current_bytes) <= _REFUSAL_READ_FULL_MAX_BYTES
    if full:
        start, end = (1, max(total, 1))
    else:
        start, end = _line_span(total, attempted_lines, _LINE_REFUSAL_WINDOW_RADIUS)

    canon = _canonical(path)
    st = _conv_state(conv_id)
    st["reads"][canon] = {
        "sha": sha,
        "full": full,
        "ranges": [(start, end)],
    }
    st["targeted_read_grounded"].add(canon)

    label = "full current file" if full else "current window"
    numbered = _numbered_line_window(text, start_line=start, end_line=end, total_lines=total)
    header = (
        f"\n\nFresh {label} for {path} "
        f"[lines {start}-{end} of {total}; total lines: {total}]. "
        "Line numbers may have shifted — use these for the corrected line edit:\n"
    )
    delivered = {
        "path": path,
        "sha256": sha,
        "full": full,
        "ranges": [(start, end)],
        "total_lines": total,
    }
    return header + numbered, delivered


def _with_line_refusal_read(
    outcome: ToolOutcome,
    conv_id: str,
    path: str,
    *,
    current_bytes: bytes,
    sha: str,
    attempted_lines: tuple[int, int] | None,
    anchored: bool = False,
    line_refusal_read: bool,
) -> ToolOutcome:
    if not line_refusal_read or outcome.error not in {"STALE_FILE_CONTEXT", "FRESH_READ_REQUIRED"}:
        return outcome
    if anchored and len(current_bytes) > _REFUSAL_READ_FULL_MAX_BYTES:
        return outcome
    fresh_content, delivered = _line_refusal_read(
        conv_id, path, current_bytes=current_bytes, sha=sha, attempted_lines=attempted_lines
    )
    structured = dict(outcome.structured or {})
    structured["delivered_read"] = delivered
    return ToolOutcome(
        success=False,
        error=outcome.error,
        content=(outcome.content or "") + fresh_content,
        structured=structured,
    )


def _line_success_content(
    path: str,
    text: str,
    *,
    changed_lines: tuple[int, int],
    prefix: str,
) -> str:
    total = len(text.splitlines())
    start, end = _line_span(total, changed_lines, _LINE_SUCCESS_WINDOW_RADIUS)
    numbered = _numbered_line_window(text, start_line=start, end_line=end, total_lines=total)
    return (
        f"{prefix}\n\nUpdated content for {path} "
        f"[lines {start}-{end} of {total}; total lines: {total}]. "
        "Line numbers may have shifted — use these for any next line edit:\n"
        f"{numbered}"
    )


def guard_fresh_edit(
    conv_id: str,
    path: str,
    *,
    current_bytes: bytes,
    old: str | None = None,
    new: str | None = None,
    edit_lines: tuple[int, int] | None = None,
    anchored: bool = True,
    attempted_lines: tuple[int, int] | None = None,
    line_refusal_read: bool = False,
) -> ToolOutcome | None:
    """CD-TOOLS-1 fresh-edit guard. Returns a BLOCKING ToolOutcome (the caller must NOT mutate
    the file) when the model lacks fresh, complete grounding of the edit region — else None.

    Checks, in order: (1) an internal elision marker in old/new → ELISION_MARKER_REJECTED;
    (2) the file changed under the model since its last read → STALE_FILE_CONTEXT;
    (3) the model has no current grounding since the last write (editing blind) → FRESH_READ_
    REQUIRED; (4) the only grounding is a PARTIAL read not covering the edit region → FRESH_READ_
    REQUIRED. `read_since_write` (set by file_read AND the workspace-snapshot pin / F9 dedup, and
    CLEARED on every mutation), a current refusal-delivered targeted read, or anchored edit_grounded
    is the current-grounding signal for targeted edits; the Mode-B full-rewrite case still uses only
    `read_since_write`, so a refusal-delivered targeted read never permits a blind file_write."""
    import hashlib

    if _has_elision_marker(old, new):
        return ToolOutcome(
            success=False,
            error="ELISION_MARKER_REJECTED",
            content=(
                f"Edit refused — the edit text for {path} contains an internal elision "
                "placeholder (e.g. '<… chars elided …>'); that marker is render-only and must "
                "never be written into a file. Read the file, then edit with the real text."
            ),
            structured={
                "kind": "elision_marker_rejected",
                "path": path,
                "next_required_action": "file_read",
                "suggested_args": {"path": path},
            },
        )
    # Small files stay fully in the model's context (never elided) → the model has the bytes;
    # requiring a fresh read there would only break legitimate small-file edits.
    if len(current_bytes) <= _GUARD_FRESH_READ_MIN_BYTES:
        return None
    sha = hashlib.sha256(current_bytes).hexdigest()
    st = _read_state.get(conv_id) or {}
    canon = _canonical(path)
    rec = (st.get("reads") or {}).get(canon)
    # [REL-RC-D/G] grounding for an EDIT comes from the coarse read_since_write bit, a current
    # refusal-delivered targeted read, or — for ANCHORED callers only — prior edit_grounded.
    # edit_grounded does NOT count for LINE-BASED callers: an anchored edit can shift line numbers.
    # A line-edit refusal can, however, deliver and record fresh numbered content as `reads[path]`
    # plus targeted_read_grounded; that grounds corrected targeted edits, not blind file_write.
    rec_current = rec is not None and rec.get("sha") == sha
    grounded = (
        canon in (st.get("read_since_write") or set())
        or (rec_current and canon in (st.get("targeted_read_grounded") or set()))
        or (anchored and canon in (st.get("edit_grounded") or set()))
    )
    if rec is not None and rec.get("sha") != sha:
        outcome = ToolOutcome(
            success=False,
            error="STALE_FILE_CONTEXT",
            content=(
                f"Edit refused — {path} has changed since you last read it, so your edit text "
                "may target stale content. Read it again (file_read), then edit."
            ),
            structured={
                "kind": "stale_file_context",
                "path": path,
                "next_required_action": "file_read",
                "suggested_args": {"path": path},
            },
        )
        return _with_line_refusal_read(
            outcome,
            conv_id,
            path,
            current_bytes=current_bytes,
            sha=sha,
            attempted_lines=attempted_lines or edit_lines,
            anchored=anchored,
            line_refusal_read=line_refusal_read,
        )
    if not grounded:
        outcome = _fresh_read_required(
            path, "you have not read this file's current content since it last changed"
        )
        return _with_line_refusal_read(
            outcome,
            conv_id,
            path,
            current_bytes=current_bytes,
            sha=sha,
            attempted_lines=attempted_lines or edit_lines,
            anchored=anchored,
            line_refusal_read=line_refusal_read,
        )
    # grounded, current sha: if the only sha-aware grounding is a PARTIAL read (not the whole
    # file), the edited region must fall inside what was actually read. A FULL read (rec.full)
    # covers everything, so it is exempt. When the region is UNDETERMINED (edit_lines is None —
    # e.g. file_edit/file_str_replace matched `old` via the forgiving/whitespace-tolerant path
    # after an exact find missed), we cannot prove the mutated lines were seen → fail closed so a
    # partial read can't mutate unread lines (Codex round-1).
    if rec is not None and not rec.get("full"):
        if edit_lines is None:
            outcome = _fresh_read_required(
                path,
                "could not confirm the exact lines you are editing were in the part of the file "
                "you read",
            )
            return _with_line_refusal_read(
                outcome,
                conv_id,
                path,
                current_bytes=current_bytes,
                sha=sha,
                attempted_lines=attempted_lines or edit_lines,
                anchored=anchored,
                line_refusal_read=line_refusal_read,
            )
        if not _covers(rec.get("ranges", []), edit_lines[0], edit_lines[1]):
            outcome = _fresh_read_required(
                path, "the lines you are editing were not in the part of the file you read"
            )
            return _with_line_refusal_read(
                outcome,
                conv_id,
                path,
                current_bytes=current_bytes,
                sha=sha,
                attempted_lines=attempted_lines or edit_lines,
                anchored=anchored,
                line_refusal_read=line_refusal_read,
            )
    return None


def reground_after_anchored_edit(
    conv_id: str, path: str, new_bytes: bytes, *, line_numbers_valid: bool = False
) -> None:
    """[REL-RC-D/G] After a HOST-VALIDATED ANCHORED edit (file_edit / file_str_replace /
    exact_replace), or a line-count-preserving file_replace_lines edit, ADVANCE the file's grounding
    to the just-written bytes instead of discarding it. Set `line_numbers_valid=True` only when the
    edit is known not to have shifted numeric targets.

    Root cause this fixes: the successful-mutation paths discarded only the coarse read_since_write
    bit and left `reads[path].sha` at the PRE-edit value. So the model's OWN next same-file edit saw
    rec.sha(v1) != disk_sha(v2) and was refused as STALE_FILE_CONTEXT — the guard misreporting a
    tracked, deterministic, host-validated edit as EXTERNAL drift. Back-to-back same-file edits then
    wedged the loop into STUCK (the REL-RC-D revise_thrice failure).

    Preserves the PRIOR grounding SHAPE (Codex plan gate): keep whole-file grounding ONLY if the
    read was already full; NEVER promote a partial read to whole-file. A partial/absent prior
    grounding is DROPPED (not advanced) — advancing it would either lie about whole-file knowledge
    or carry line ranges that this edit may have shifted; the next edit then honestly requires a
    fresh read (auto-groundable) rather than inheriting a stale-but-fresh-sha partial window.

    Grounding is granted on the SEPARATE `edit_grounded` signal, NOT `read_since_write`: an anchored
    edit lets the model make its NEXT anchored edit (guard_fresh_edit honors edit_grounded), but the
    coarse read bit stays cleared so a blind full file_write is STILL refused until a genuine read
    (the Mode-B rewrite-from-memory protection is untouched).

    Deliberately NOT called for line-shifting line edits (file_replace_lines with a different output
    line count / file_insert_lines) or full/blind writes (file_write / file_append / safe_write_file)
    or external mutations (run_script): a later numeric line target or a from-memory rewrite can be
    stale even under a correct fresh sha, so those keep clearing grounding and force a fresh targeted
    read/refusal-read."""
    import hashlib

    canon = _canonical(path)
    st = _conv_state(conv_id)
    prior = (st.get("reads") or {}).get(canon)
    # The read bit is always cleared on mutation (a blind full rewrite still needs a fresh read).
    st["read_since_write"].discard(canon)
    if prior is not None and prior.get("full"):
        # Whole-file grounding stays whole-file, advanced to the post-edit bytes the engine wrote,
        # and re-grants EDIT grounding so the model's own next anchored edit isn't false-STALE.
        new_line_count = new_bytes.count(b"\n") + 1
        st["reads"][canon] = {
            "sha": hashlib.sha256(new_bytes).hexdigest(),
            "full": True,
            "ranges": [(1, new_line_count)],
        }
        st["edit_grounded"].add(canon)
        if line_numbers_valid:
            st["targeted_read_grounded"].add(canon)
        else:
            st["targeted_read_grounded"].discard(canon)
    else:
        # Partial/absent grounding: drop the stale sha record so it can't produce a FALSE
        # STALE_FILE_CONTEXT, and clear edit grounding → the next edit gets an honest
        # FRESH_READ_REQUIRED (never promote partial context to whole-file).
        (st.get("reads") or {}).pop(canon, None)
        st["edit_grounded"].discard(canon)
        st["targeted_read_grounded"].discard(canon)


def _canonical(path: str) -> str:
    """Canonical tracker key so the read-before-rewrite guard can't be bypassed by
    spelling the same file differently. Strips the redundant workspace prefix
    ('workspace/foo' == '/workspace/foo' == 'foo') AND normalizes './', '//' and
    '../' segments ('./x.py' == 'x.py') via posixpath.normpath, so a read of one
    spelling and a mutate of another resolve to ONE key."""
    import posixpath

    return posixpath.normpath(strip_redundant_workspace_prefix(path))


def _number_lines(text: str, start: int = 1) -> str:
    """Render text with right-aligned 1-based line numbers + a tab, so the model
    can target precise ranges with file_replace_lines / file_insert_lines — the
    robust way to edit a large file without reproducing its exact bytes."""
    lines = text.splitlines()
    if not lines:
        return ""
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{start + i:>{width}}\t{ln}" for i, ln in enumerate(lines))


def _strip_line_numbers(s: str) -> str:
    """Remove accidental `N\\t` line-number prefixes the model copied from a read."""
    return _LINENO_PREFIX.sub("", s)


def _norm_ws(s: str) -> str:
    """Whitespace-normalized form for forgiving matching: strip BOTH ends of each
    line + drop blank leading/trailing lines. Tolerates the #1 cause of failed exact
    matches — indentation and trailing-space drift — which small models get wrong
    constantly. The replacement still uses the caller's `new` verbatim, so the edit's
    own indentation is whatever the model intended."""
    return "\n".join(ln.strip() for ln in s.strip("\n").splitlines())


# ---------------------------------------------------------------------------
# W3 — syntax gate helpers
# ---------------------------------------------------------------------------


def _syntax_errors(path: str, text: str) -> list[str]:
    """Return error-kind tokens for `text` parsed as `path`'s type.

    Returns one string per distinct error kind (e.g. "SyntaxError",
    "JSONDecodeError"). Line numbers and messages are deliberately EXCLUDED
    from the returned strings so the diff-filter (`introduced = post - pre`)
    is stable across whole-file rewrites: a pre-existing SyntaxError at line
    5 stays "SyntaxError" regardless of whether the rewrite moves it to line 1.
    This prevents false positives where a pre-existing messy file is punished
    every time it is written.

    Supported: .py (compile), .json (json.loads).
    Unsupported (tree-sitter not installed): .html/.css/.js/.ts/.tsx/.jsx
    — those return [] so unsupported-format files are never blocked.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == "py":
        try:
            compile(text, path, "exec")
        except SyntaxError:
            return ["SyntaxError"]
        return []
    if ext == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return ["JSONDecodeError"]
        return []
    # tree-sitter unavailable: html/css/js/ts/tsx/jsx/yaml/yml unsupported → []
    return []


async def _gated_write(
    ctx: ToolContext,
    path: str,
    new_bytes: bytes,
    old_text: str | None,
) -> ToolOutcome | None:
    """Write new_bytes to path; auto-revert to old_text if new content introduces
    syntax errors that were not already present (W3 diff-filter).

    Returns a failure ToolOutcome when new errors are introduced, None otherwise.
    Callers proceed to their own success outcome when None is returned.
    The diff-filter compares error-KIND tokens (not messages/line numbers) so
    pre-existing messy files aren't punished by whole-file rewrites that shift
    line numbers without changing the nature of the breakage.
    """
    assert ctx.sandbox is not None
    new_text = new_bytes.decode("utf-8", errors="replace")
    pre = _syntax_errors(path, old_text) if old_text is not None else []
    post = _syntax_errors(path, new_text)
    introduced = [e for e in post if e not in pre]
    await ctx.sandbox.write_file(path, new_bytes)
    if not introduced:
        return None
    if old_text is not None:
        # AUTO-REVERT: restore previous content so the workspace stays consistent.
        await ctx.sandbox.write_file(path, old_text.encode("utf-8"))
        kept = "it was NOT applied (previous content kept)"
    else:
        kept = "it was applied (new file; no prior content to revert)"
    return ToolOutcome(
        success=False,
        error="syntax_gate_reverted",
        content=(
            f"Your edit to {path} introduced syntax error(s); {kept}: "
            f"{'; '.join(introduced)}. "
            "Fix the snippet and try a DIFFERENT edit. "
            "DO NOT re-run the same failed edit — it will fail identically."
        ),
    )


class FileReadArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to read.")
    # C-1 (filesystem-as-memory): line-range reads so a large file can be
    # navigated by path + selective read instead of dumped whole into context.
    offset: int | None = Field(
        default=None, description="1-based line to start at (omit to read from the top)."
    )
    limit: int | None = Field(
        default=None, description="Max number of lines to read (omit for the rest of the file)."
    )


class FileReadTool:
    definition = ToolDef(
        name="file_read",
        description=(
            "Read a UTF-8 text file from the workspace, with 1-based LINE NUMBERS. "
            "For large files pass `offset` (1-based start line) + `limit` (line "
            "count) to read a slice. Prefer `file_edit` (pass the exact text you see "
            "as `old`) for targeted changes; the line numbers also let you target "
            "`file_replace_lines`, but re-read right before each line edit since they "
            "shift after every change."
        ),
        args_model=FileReadArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner
    )

    async def run(self, args: FileReadArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None  # sandbox tools always receive an instance
        data = await ctx.sandbox.read_file(args.path)
        # F1 — set the read-since-write bit for this path so a subsequent
        # file_write is allowed (the happy path: read → write). Applies to ALL
        # tiers (not gated on ctx.assist) — the thrash root-cause hits capable
        # models too, and the guard must be symmetric. The internal
        # ctx.sandbox.read_file() calls inside _gated_write and each mutator's
        # own read do NOT go through this method, so they do NOT set the bit —
        # only an explicit model-issued file_read counts as grounding evidence.
        import hashlib

        _disk_sha = hashlib.sha256(data).hexdigest()  # CD-TOOLS-1 fresh-edit grounding
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        start = max((args.offset or 1) - 1, 0)
        if start >= total and total > 0:
            # An offset PAST end-of-file shows NO content — it must NOT grant grounding
            # (read_since_write) or the CD-TOOLS-1/2 fresh-edit guard could be bypassed by a
            # `file_read(offset=huge)` that read nothing (Codex CD-TOOLS-2 round-2). A prior real
            # read's grounding is untouched (we simply don't ADD here).
            return ToolOutcome(
                success=True,
                content=f"[lines {start + 1}-{total} of {total} — offset past end of file]",
            )
        # F1 — grant the read-since-write grounding bit ONLY now that we know content WILL be
        # shown (the branches below all render real lines, incl. the empty-file fall-through).
        # Set before file_write's read-before-write gate AND the CD-TOOLS-1 edit guard rely on it.
        _conv_state(ctx.conversation_id)["read_since_write"].add(_canonical(args.path))
        # F7 — pressure-aware head-only. Fires ONLY when:
        #   - ctx.assist is on (the weak-model tier the gate exists to protect),
        #   - the caller did NOT pass an explicit offset/limit (a targeted read
        #     is already cheap; the gate would just break a working flow), and
        #   - the file is large enough that today's paging would have to split
        #     it across multiple pages (the size proxy for "this is going to
        #     cost real context budget"). When all three hold, return a small
        #     head slice + a directive telling the model to grep, then read a
        #     line range — not the full page. The assist-OFF branch and the
        #     no-pressure (small file / explicit range) branch are untouched:
        #     they fall through to today's paging below.
        if (
            ctx.assist
            and args.offset is None
            and args.limit is None
            and len(text) > _PRESSURE_FILE_THRESHOLD
        ):
            # Number a HEAD slice kept under the pressure head budget. Same
            # width/numbering convention as the regular page so a follow-up
            # file_read(offset=K+1, limit=N) is byte-consistent.
            width = len(str(total)) or 1
            head: list[str] = []
            used = 0
            for i, ln in enumerate(lines):
                line = f"{i + 1:>{width}}\t{ln}"
                if head and used + len(line) + 1 > _PRESSURE_HEAD_BUDGET:
                    break
                head.append(line)
                used += len(line) + 1
            head_to = len(head)
            record_read(
                ctx.conversation_id, args.path, sha=_disk_sha, start_line=1,
                end_line=max(head_to, 1), full=(head_to >= total),
            )
            header = (
                f"[lines 1-{head_to} of {total} (file: {len(text)} chars) — "
                f"HEAD-ONLY under context pressure]\n"
            )
            return ToolOutcome(
                success=True,
                content=header + "\n".join(head) + "\n\n" + _PRESSURE_DIRECTIVE,
            )
        # A page that fits under the snip cap, line-numbered from the absolute start
        # so line numbers are correct. If `limit` is given, respect it but still cap
        # by chars so a huge limit can't corrupt the result.
        #
        # CW-6: the page budget is capability-derived. The runtime stamps
        # ctx.read_char_budget from derive_context_caps(assist, driver_context_window)
        # so an assist-OFF (capable) model reads a file that fits the snapshot pin in
        # ONE shot — and the matching _OBS_SNIP_CHARS override keeps that large
        # observation from being render-snipped to a corrupted head/tail. Unset
        # (assist-ON / executors that don't thread it) ⇒ the static 7k default.
        read_budget = ctx.read_char_budget or _READ_CHAR_BUDGET
        # CW P1-b (round-2): assist-OFF budgets the page on RAW file chars (the SAME unit
        # the snapshot pin uses), so a file whose RAW size fits the per-file pin reads in
        # ONE shot; line numbers are applied to the selected slice AFTER this check, and
        # their render overhead is absorbed by the assist-OFF observation-snip cap (raised
        # in tandem — see context_budget). assist-ON keeps the obs-snip at the 8k baseline,
        # so its page must stay under it: that tier keeps charging the LINE-NUMBERED render
        # (byte-identical to today) so a short-line page can't render past the snip cap.
        budget_raw = not ctx.assist
        out: list[str] = []
        width = len(str(total)) or 1
        used = 0
        i = start
        cap = start + args.limit if args.limit is not None else total
        # The raw charge must equal the file's true byte size: the FINAL line carries no
        # trailing newline unless `text` ends in one, so charging +1 for it would
        # over-count by 1 and page a file that exactly fits the pin (codex round-3).
        ends_nl = text.endswith("\n")
        while i < min(cap, total):
            line = f"{i + 1:>{width}}\t{lines[i]}"
            raw_nl = 1 if (i < total - 1 or ends_nl) else 0
            charge = (len(lines[i]) + raw_nl) if budget_raw else (len(line) + 1)
            if out and used + charge > read_budget:
                break
            out.append(line)
            used += charge
            i += 1
        shown_to = i
        # CD-TOOLS-1: record the lines the model saw un-elided. full iff this single page
        # covered the WHOLE file (from line 1 to the last line).
        record_read(
            ctx.conversation_id, args.path, sha=_disk_sha, start_line=start + 1,
            end_line=max(shown_to, start + 1), full=(start == 0 and shown_to >= total),
        )
        # there's more file to read below if we didn't reach the end (whether we
        # stopped on the char budget or the caller's limit)
        more = f"; read more with offset={shown_to + 1}" if shown_to < total else ""
        header = f"[lines {start + 1}-{shown_to} of {total}{more}]\n"
        return ToolOutcome(success=True, content=header + "\n".join(out))


class FileWriteArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to write.")
    content: str = Field(description="Full UTF-8 content to write.")


class FileWriteTool:
    definition = ToolDef(
        name="file_write",
        description=(
            "The PREFERRED way to author file content: write (create/overwrite) a "
            "UTF-8 text file in the workspace with its full content. Use this instead "
            "of shell redirection. To change part of an existing file, use file_edit."
        ),
        args_model=FileWriteArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileWriteArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (g := await _governed_guard(ctx.sandbox, args.path)) is not None:
            return g
        # Read existing content once — used by both the F1 guard and the W3 syntax gate.
        old_text: str | None = None
        try:
            old_text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — absent file is fine, that just means "new"
            old_text = None
        # F1 — read-before-rewrite guard (ALL tiers, no assist gate). Refuse a
        # file_write to an EXISTING file if there has been no successful
        # file_read of it since the path's last successful mutation in this
        # conversation. A NEW (nonexistent) file is always allowed — there is no
        # prior content to ground on. The old F3 "second untracked write forces
        # replace" escape hatch is REMOVED; the only way past this refusal is an
        # actual file_read (which sets the read-since-write bit), or using a
        # targeted edit tool (file_replace_lines / file_insert_lines / file_edit)
        # which does not require a full-rewrite guard because it operates on
        # specific lines anchored to the current content.
        if old_text is not None:  # file exists (we read it above)
            # ROOT-2 — binary-deliverable clobber guard. The target is an EXISTING file
            # of a generated-binary type; a text file_write would corrupt it. Refuse
            # with a terminal message so the agent stops trying to "fix"/overwrite a
            # finished deck/doc and recognises it is already delivered. (Only EXISTING
            # binaries are blocked — a new text file of any name is still allowed.)
            ext = args.path.rsplit(".", 1)[-1].lower() if "." in args.path else ""
            if ext in _BINARY_DELIVERABLE_EXTS:
                return ToolOutcome(
                    success=False,
                    error="binary_deliverable_clobber",
                    content=(
                        f"{args.path} is a generated binary deliverable; do not overwrite "
                        f"it with text. It is already produced and delivered. If it truly "
                        f"needs to change, regenerate it with the tool that produced it "
                        f"(e.g. slides_generate) — never file_write into it."
                    ),
                )
            canonical = _canonical(args.path)
            if canonical not in _conv_state(ctx.conversation_id)["read_since_write"]:
                return ToolOutcome(
                    success=False,
                    error="read_before_write",
                    content=(
                        f"file_write refused: {args.path} already exists and has not been "
                        f"read since the last write to it. Read it first "
                        f"(file_read) to ground your edit in the current content, or use a "
                        f"targeted edit (file_replace_lines / file_insert_lines / file_edit) "
                        f"instead of rewriting the whole file from memory."
                    ),
                )
        # W3 — syntax gate: write new bytes; auto-revert if new content introduces errors.
        raw = args.content.encode("utf-8")
        gated = await _gated_write(ctx, args.path, raw, old_text)
        if gated is not None:
            return gated
        # F1 — clear the read-since-write bit: the file has been mutated, so the
        # next file_write must be preceded by another file_read.
        _clear_grounding(ctx.conversation_id, args.path)
        return ToolOutcome(
            success=True, content=f"wrote {len(raw)} bytes to {args.path}", artifacts=[args.path]
        )


class FileAppendArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to append to.")
    content: str = Field(description="UTF-8 content to append (created if absent).")


class FileAppendTool:
    """Append to a file via the file API — the dedicated replacement for shell
    `>>` (which corrupts on quotes/`$`/backticks). Closes the one legitimate
    reason a model reaches for shell redirection (Cluster 9 <file_rules>)."""

    definition = ToolDef(
        name="file_append",
        description=(
            "Append UTF-8 content to a workspace file (creating it if absent). Use "
            "this instead of shell `>>` — raw-shell append corrupts on special "
            "characters."
        ),
        args_model=FileAppendArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileAppendArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (g := await _governed_guard(ctx.sandbox, args.path)) is not None:
            return g
        old_text: str | None = None
        existing = b""
        try:
            existing = await ctx.sandbox.read_file(args.path)
            old_text = existing.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — absent file → start empty
            existing = b""
            old_text = None
        combined = existing + args.content.encode("utf-8")
        # W3 — syntax gate: write combined; auto-revert to old if errors introduced.
        gated = await _gated_write(ctx, args.path, combined, old_text)
        if gated is not None:
            return gated
        # F1 — file_append is a successful mutation: clear the read-since-write bit.
        _clear_grounding(ctx.conversation_id, args.path)
        return ToolOutcome(
            success=True,
            content=f"appended {len(args.content.encode('utf-8'))} bytes to {args.path}",
            artifacts=[args.path],
        )


class FileListArgs(BaseModel):
    path: str = Field(default=".", description="Workspace-relative directory to list.")


class FileListTool:
    definition = ToolDef(
        name="file_list",
        description="List the entries of a directory in the workspace.",
        args_model=FileListArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner
    )

    async def run(self, args: FileListArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        entries = await ctx.sandbox.list_dir(args.path)
        return ToolOutcome(
            success=True,
            content="\n".join(entries) if entries else "(empty)",
            structured={"path": args.path, "entries": entries},
        )


class FileEditArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    old: str = Field(description="Text to replace (the first occurrence).")
    new: str = Field(description="Replacement text.")


def _forgiving_replace(text: str, old: str, new: str) -> tuple[str | None, str]:
    """Replace the first occurrence of `old` with `new`, FORGIVINGLY — small models
    (and large ones) rarely reproduce a long substring byte-perfectly. Tries, in
    order: exact match; with accidental line-number prefixes stripped from `old`;
    whitespace-normalized match (per-line rstrip, drop blank edges) located back in
    the original text. Returns (updated_text_or_None, note). None ⇒ not found."""
    if old in text:
        return text.replace(old, new, 1), "exact"
    old2 = _strip_line_numbers(old)
    if old2 != old and old2 in text:
        return text.replace(old2, new, 1), "stripped line numbers"
    # whitespace-normalized: find the contiguous line span whose rstrip'd form
    # equals the rstrip'd `old`, then splice the ORIGINAL lines out.
    target = _norm_ws(old2)
    if target:
        doc = text.splitlines(keepends=True)
        norm = [x.strip() for x in doc]
        tgt = target.split("\n")
        for i in range(0, len(norm) - len(tgt) + 1):
            if norm[i : i + len(tgt)] == tgt:
                updated = "".join(doc[:i]) + new + ("" if new.endswith("\n") else "\n") + "".join(
                    doc[i + len(tgt) :]
                )
                return updated, "whitespace-normalized"
    return None, "not found"


def _nearest_anchor(text: str, old: str) -> str:
    """A short hint for a failed edit: the line in the file most similar to the
    first non-blank line of `old`, so the model can re-aim."""
    first = next((ln.strip() for ln in _strip_line_numbers(old).splitlines() if ln.strip()), "")
    if not first:
        return ""
    token = first[:24]
    for n, ln in enumerate(text.splitlines(), 1):
        if token and token in ln:
            return f" (similar text near line {n}: {ln.strip()[:60]!r})"
    return ""


class FileEditTool:
    definition = ToolDef(
        name="file_edit",
        description=(
            "Replace the first occurrence of `old` with `new` in a workspace file. "
            "Matching is forgiving (tolerates indentation / trailing-space drift and "
            "pasted-in line numbers). This is the SAFEST targeted edit — it anchors on "
            "the text you give, so it can't hit the wrong place. For a large file, read "
            "the relevant section first (file_read with offset/limit) and pass that exact "
            "snippet as `old`."
        ),
        args_model=FileEditArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileEditArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (g := await _governed_guard(ctx.sandbox, args.path)) is not None:
            return g
        # Intent no-op: old and new are LITERALLY identical (after stripping any
        # line-number prefixes the model copied). This is distinct from a
        # whitespace-only edit (old≠new, which must apply) — here the model asked
        # for no change at all, so refuse before touching the file regardless of how
        # the file's own whitespace happens to differ. The ground-truth guard below
        # still catches the "applied result is unchanged" case.
        if _strip_line_numbers(args.old) == _strip_line_numbers(args.new):
            return ToolOutcome(
                success=False,
                content=(
                    f"file_edit refused: `old` and `new` are identical — this asks for "
                    f"no change to {args.path}. If you already applied this edit, move "
                    "on; otherwise give the NEW content you want."
                ),
                error="no_op_edit",
            )
        _raw = await ctx.sandbox.read_file(args.path)
        text = _raw.decode("utf-8", errors="replace")
        # CD-TOOLS-1 fresh-edit guard: refuse (no mutation) when the model lacks fresh/complete/
        # un-elided grounding of the edit region. Compute the region from `old`'s exact location.
        _idx = text.find(args.old)
        _elines = (
            (text.count("\n", 0, _idx) + 1, text.count("\n", 0, _idx) + 1 + args.old.count("\n"))
            if _idx >= 0
            else None
        )
        _blocked = guard_fresh_edit(
            ctx.conversation_id,
            args.path,
            current_bytes=_raw,
            old=args.old,
            new=args.new,
            edit_lines=_elines,
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked
        updated, how = _forgiving_replace(text, args.old, _strip_line_numbers(args.new))
        if updated is None:
            return ToolOutcome(
                success=False,
                content=(
                    f"`old` not found in {args.path} (tried exact + whitespace-tolerant)."
                    + _nearest_anchor(text, args.old)
                    + " Tip: read the file for line numbers, then use file_replace_lines."
                ),
                error="old_text_not_found",
            )
        # No-op guard, on GROUND TRUTH: refuse only when the replacement leaves the
        # file byte-identical. Checking the *applied* result (not an abstract
        # old-vs-new compare) lets a legitimate whitespace-only edit through — a
        # re-indent or trailing-space cleanup matches ws-tolerantly but writes
        # `new` verbatim, so `updated != text` and it applies — while still catching
        # the real no-op: an already-applied edit (or old≈new) that changes nothing.
        if updated == text:
            return ToolOutcome(
                success=False,
                content=(
                    f"file_edit refused: this edit leaves {args.path} unchanged — the "
                    "new content already matches what's on disk (it may have been "
                    "applied on an earlier turn). No further action is needed; move on."
                ),
                error="no_op_edit",
            )
        # W3 — syntax gate: write updated; auto-revert to text if errors introduced.
        gated = await _gated_write(ctx, args.path, updated.encode("utf-8"), text)
        if gated is not None:
            return gated
        # [REL-RC-D] file_edit is a HOST-VALIDATED ANCHORED mutation: advance grounding to the
        # post-edit bytes (don't discard) so the model's own next same-file edit isn't false-STALE.
        reground_after_anchored_edit(ctx.conversation_id, args.path, updated.encode("utf-8"))
        return ToolOutcome(
            success=True, content=f"edited {args.path} ({how})", artifacts=[args.path]
        )


class FileReplaceLinesArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    start_line: int = Field(description="First line to replace (1-based, inclusive).")
    end_line: int = Field(description="Last line to replace (1-based, inclusive).")
    new_text: str = Field(description="Replacement text for that line range (can be multi-line).")


class FileReplaceLinesTool:
    """Surgical, large-file-friendly edit: replace an inclusive 1-based LINE RANGE
    with new text. The model reads the numbered file, picks the range, and writes
    the replacement — no need to reproduce the old bytes. Works for any model/size."""

    definition = ToolDef(
        name="file_replace_lines",
        description=(
            "Replace lines [start_line, end_line] (1-based, inclusive) of a file with "
            "`new_text`. Good for large files where reproducing exact text is hard. "
            "IMPORTANT: re-read the file IMMEDIATELY before each call — line numbers "
            "shift after any edit, and a stale range silently overwrites the wrong lines. "
            "Use file_insert_lines to insert without replacing."
        ),
        args_model=FileReplaceLinesArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileReplaceLinesArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (g := await _governed_guard(ctx.sandbox, args.path)) is not None:
            return g
        _raw = await ctx.sandbox.read_file(args.path)
        text = _raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        n = len(lines)
        if args.start_line < 1 or args.end_line < args.start_line or args.start_line > n + 1:
            return ToolOutcome(
                success=False,
                content=(
                    f"bad range [{args.start_line},{args.end_line}] for {args.path} "
                    f"({n} lines). start_line must be 1..{n + 1}, end_line >= start_line."
                ),
                error="bad_range",
            )
        # CD-TOOLS-1 fresh-edit guard: line numbers shift after any edit, so a stale/elided
        # range silently overwrites the wrong lines — require fresh, covering grounding first.
        _blocked = guard_fresh_edit(
            ctx.conversation_id, args.path, current_bytes=_raw, new=args.new_text,
            edit_lines=(args.start_line, min(args.end_line, max(n, 1))),
            anchored=False,  # [REL-RC-D] line-based: a prior edit's line-shift can stale these numbers
            attempted_lines=(args.start_line, min(args.end_line, max(n, 1))),
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked
        # Deletion guard: empty new_text over a real range is the silent-data-loss path
        # (a miscounted range replaced with nothing — the exact gpt-oss-120b failure).
        if _strip_line_numbers(args.new_text).strip() == "":
            end_g = min(args.end_line, n)
            doomed = max(0, end_g - args.start_line + 1)
            return ToolOutcome(
                success=False,
                content=(
                    f"file_replace_lines refused: new_text is empty — this would DELETE "
                    f"lines {args.start_line}-{end_g} ({doomed} lines) of {args.path} with "
                    "no replacement, the usual symptom of a miscounted range. To remove "
                    "code, use file_write to rewrite the file without those lines; to clear "
                    "a block on purpose, replace it with a placeholder comment."
                ),
                error="empty_replacement_refused",
            )
        new_lines = _strip_line_numbers(args.new_text).split("\n")
        end = min(args.end_line, n)
        result = lines[: args.start_line - 1] + new_lines + lines[end:]
        out = "\n".join(result)
        if text.endswith("\n"):
            out += "\n"
        # W3 — syntax gate: write result; auto-revert to text if errors introduced.
        gated = await _gated_write(ctx, args.path, out.encode("utf-8"), text)
        if gated is not None:
            return gated
        replaced = max(0, end - args.start_line + 1)
        if replaced == len(new_lines):
            # [REL-RC-G] Same line count means numeric line targets did not shift; advance the
            # full-file grounding shape just like an anchored edit so consecutive line edits work.
            reground_after_anchored_edit(
                ctx.conversation_id, args.path, out.encode("utf-8"), line_numbers_valid=True
            )
        else:
            # F1 — line-shifting replacement: clear grounding; the next line edit must use the
            # fresh numbered content delivered on refusal or do an explicit file_read.
            _clear_grounding(ctx.conversation_id, args.path)
        changed_hi = args.start_line + max(len(new_lines), 1) - 1
        return ToolOutcome(
            success=True,
            content=_line_success_content(
                args.path,
                out,
                changed_lines=(args.start_line, changed_hi),
                prefix=(
                    f"replaced lines {args.start_line}-{end} of {args.path} "
                    f"({replaced}→{len(new_lines)} lines)"
                ),
            ),
            artifacts=[args.path],
        )


class FileInsertLinesArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    after_line: int = Field(
        description="Insert AFTER this 1-based line (0 = at the very top of the file)."
    )
    text: str = Field(description="Text to insert (can be multi-line).")


class FileInsertLinesTool:
    """Insert text after a given line WITHOUT replacing anything — the clean way to
    ADD a block (a new section/app) to a large file by line number."""

    definition = ToolDef(
        name="file_insert_lines",
        description=(
            "Insert `text` AFTER line `after_line` (1-based; 0 = top) of a file, without "
            "replacing anything. Read the file for line numbers first. The reliable way "
            "to ADD a block to a large file."
        ),
        args_model=FileInsertLinesArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileInsertLinesArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (g := await _governed_guard(ctx.sandbox, args.path)) is not None:
            return g
        _raw = await ctx.sandbox.read_file(args.path)
        text = _raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        n = len(lines)
        # CD-TOOLS-1 guard: the insert point relies on current line numbers — require fresh
        # grounding + reject an elision marker in the inserted text (no region coverage needed).
        _blocked = guard_fresh_edit(
            ctx.conversation_id, args.path, current_bytes=_raw, new=args.text,
            edit_lines=(
                max(args.after_line, 1),
                max(1, min(args.after_line + 1, n)),
            ),
            anchored=False,  # [REL-RC-D] line-based: a prior edit's line-shift can stale this insert point
            attempted_lines=(max(args.after_line, 1), max(args.after_line, 1)),
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked
        if args.after_line < 0 or args.after_line > n:
            return ToolOutcome(
                success=False,
                content=f"after_line {args.after_line} out of range for {args.path} (0..{n}).",
                error="bad_line",
            )
        ins = _strip_line_numbers(args.text).split("\n")
        result = lines[: args.after_line] + ins + lines[args.after_line :]
        out = "\n".join(result)
        if text.endswith("\n"):
            out += "\n"
        # W3 — syntax gate: write result; auto-revert to text if errors introduced.
        gated = await _gated_write(ctx, args.path, out.encode("utf-8"), text)
        if gated is not None:
            return gated
        # F1 — file_insert_lines is a successful mutation: clear the read-since-write bit.
        _clear_grounding(ctx.conversation_id, args.path)
        changed_start = args.after_line + 1
        changed_hi = changed_start + max(len(ins), 1) - 1
        return ToolOutcome(
            success=True,
            content=_line_success_content(
                args.path,
                out,
                changed_lines=(changed_start, changed_hi),
                prefix=f"inserted {len(ins)} lines after line {args.after_line} of {args.path}",
            ),
            artifacts=[args.path],
        )


# ---------------------------------------------------------------------------
# W4 — capability-gated anchored str-replace (offered to ANCHORED_EDIT models)
# ---------------------------------------------------------------------------


def _occurrence_lines(text: str, needle: str) -> list[int]:
    """Return 1-based line numbers of every occurrence of needle in text."""
    results: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        results.append(text[:idx].count("\n") + 1)
        start = idx + max(len(needle), 1)
    return results


class FileStrReplaceArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    old_str: str = Field(
        description="Exact text to find (must appear EXACTLY ONCE in the file)."
    )
    new_str: str = Field(description="Replacement text.")


class FileStrReplaceTool:
    """W4 — anchored str-replace for capable models (Requirement.ANCHORED_EDIT).

    Clean-room of OpenHands str_replace: reads the whole file, locates ALL
    occurrences of old_str, requires exactly one (multiple → error with line
    numbers; zero → whitespace-strip retry, then 'did not appear verbatim').
    No forgiving normalization — anchored on live disk text.
    Registry withholds this tool from the weak tier via advertised_tools.
    """

    definition = ToolDef(
        name="file_str_replace",
        description=(
            "Replace text appearing EXACTLY ONCE in a workspace file. "
            "Reads the current file and finds ALL occurrences of `old_str`: "
            "multiple matches → error with line numbers (make `old_str` unique first); "
            "zero matches (after a whitespace-strip retry) → 'did not appear verbatim'. "
            "Anchored on live disk text — no forgiving normalization. "
            "Offered only to capable models (Requirement.ANCHORED_EDIT); "
            "weak tier should use file_write or file_edit."
        ),
        args_model=FileStrReplaceArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileStrReplaceArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (g := await _governed_guard(ctx.sandbox, args.path)) is not None:
            return g
        _raw = await ctx.sandbox.read_file(args.path)
        text = _raw.decode("utf-8", errors="replace")
        # CD-TOOLS-1 fresh-edit guard (anchored exact replace) — region from old_str's location.
        _idx = text.find(args.old_str)
        _elines = (
            (text.count("\n", 0, _idx) + 1, text.count("\n", 0, _idx) + 1 + args.old_str.count("\n"))
            if _idx >= 0
            else None
        )
        _blocked = guard_fresh_edit(
            ctx.conversation_id, args.path, current_bytes=_raw,
            old=args.old_str, new=args.new_str, edit_lines=_elines,
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked

        count = text.count(args.old_str)
        if count > 1:
            lines = _occurrence_lines(text, args.old_str)
            return ToolOutcome(
                success=False,
                error="old_str_not_unique",
                content=(
                    f"Multiple occurrences ({count}) of `old_str` found in {args.path} "
                    f"at lines {lines}. Please ensure it is unique before applying."
                ),
            )

        if count == 0:
            # Whitespace-strip retry: one chance with leading/trailing whitespace removed.
            stripped = args.old_str.strip()
            if stripped and stripped != args.old_str:
                retry_count = text.count(stripped)
                if retry_count == 1:
                    new_text = text.replace(stripped, args.new_str, 1)
                    gated = await _gated_write(ctx, args.path, new_text.encode("utf-8"), text)
                    if gated is not None:
                        return gated
                    # [REL-RC-D] anchored mutation → advance grounding to the post-edit bytes.
                    reground_after_anchored_edit(
                        ctx.conversation_id, args.path, new_text.encode("utf-8")
                    )
                    return ToolOutcome(
                        success=True,
                        content=f"replaced in {args.path} (whitespace-stripped match)",
                        artifacts=[args.path],
                    )
            return ToolOutcome(
                success=False,
                error="old_str_not_found",
                content=f"`old_str` did not appear verbatim in {args.path}.",
            )

        # Exactly one occurrence — apply and pass through W3 gate.
        new_text = text.replace(args.old_str, args.new_str, 1)
        gated = await _gated_write(ctx, args.path, new_text.encode("utf-8"), text)
        if gated is not None:
            return gated
        # [REL-RC-D] anchored mutation → advance grounding to the post-edit bytes.
        reground_after_anchored_edit(ctx.conversation_id, args.path, new_text.encode("utf-8"))
        return ToolOutcome(
            success=True,
            content=f"replaced in {args.path}",
            artifacts=[args.path],
        )


def _all_occurrences(text: str, sub: str) -> list[int]:
    """Non-overlapping start offsets of `sub` in `text` (advances by len(sub))."""
    out: list[int] = []
    if not sub:
        return out
    i = text.find(sub)
    while i != -1:
        out.append(i)
        i = text.find(sub, i + len(sub))
    return out


async def _atomic_write(sandbox: Any, path: str, data: bytes) -> None:
    """CD-TOOLS-3: commit `data` to `path` atomically where the backend supports it (a sibling
    tmp + os.replace — ProcessSandbox.atomic_write), else fall back to a single write_file (still
    logical all-or-nothing — the caller has already validated everything in memory)."""
    aw = getattr(sandbox, "atomic_write", None)
    if aw is None:
        await sandbox.write_file(path, data)
    else:
        await aw(path, data)


async def _governed_relpath(sandbox: Any, path: str) -> str:
    """The REAL (symlink-followed) workspace-relative path, forward-slashed. Uses the sandbox's
    resolve_relpath (which follows symlinks via the guest realpath + rejects jail escapes) so a
    symlink can't forge a non-governed name — implemented on BOTH ProcessSandbox and the container
    backends. Falls back to the lexical canonical strip only if the method is absent or the real
    location can't be verified; in that case the mutator's own jailed write fails closed anyway."""
    rr = getattr(sandbox, "resolve_relpath", None)
    if rr is not None:
        try:
            return str(await rr(path)).replace("\\", "/")
        except Exception:  # noqa: BLE001 — an escaping/unverifiable path is rejected by the write
            pass
    return _canonical(path)


def _is_governed_artifact(relpath: str) -> bool:
    """True for the host-reserved `.disco/` namespace (appspec/tweaks/versions/context) — those
    are authored only by semantic tools, never the generic writer."""
    return relpath == ".disco" or relpath.startswith(".disco/")


# CD-TOOLS-4 — route a governed-artifact write to the SEMANTIC tool that owns it. (match-rule,
# tool, why); a rule ending in "/" matches a prefix, else an exact path. Order = most specific
# first. A governed path with NO match is host-managed with no generic editor (no false tool name).
_GOVERNED_ROUTING: tuple[tuple[str, str, str], ...] = (
    (".disco/appspec.json", "app_set_tweak", "it is the AppKit app spec"),
    (".disco/tweaks.json", "app_set_tweak", "it holds the app's tweak values"),
    (".disco/versions/", "app_snapshot_version", "it is a version snapshot"),
    (".disco/context/", "context_memory", "it is durable context memory"),
)


def _route_for_governed(relpath: str) -> tuple[str | None, str]:
    """The semantic tool (and reason) that owns `relpath`, or (None, '') if it is governed but
    has no generic editor."""
    for rule, tool, why in _GOVERNED_ROUTING:
        if rule.endswith("/"):
            if relpath.startswith(rule):
                return tool, why
        elif relpath == rule:
            return tool, why
    return None, ""


async def _governed_guard(
    sandbox: Any, path: str, *, error: str = "GOVERNED_ARTIFACT_REJECTED"
) -> ToolOutcome | None:
    """CD-TOOLS-4 artifact-aware guard, shared by ALL generic mutators: refuse a write whose REAL
    (symlink-followed) path is in the host-governed `.disco/` namespace, ROUTING the model to the
    owning semantic tool (or saying plainly there is no generic editor). Returns a blocking
    ToolOutcome, or None if the path is not governed. None of the semantic tools route through the
    generic mutators (they write `.disco/` via the sandbox directly), so this never blocks them.
    `error` lets safe_write_file keep its campaign-named SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED code
    while sharing one routing implementation.

    Short-circuit: a path already LEXICALLY under `.disco/` is governed without resolving (cheap +
    backend-agnostic); only a non-`.disco` lexical path needs the real-path resolve to catch a
    symlink that reaches INTO `.disco/`."""
    rel = _canonical(path)
    if not _is_governed_artifact(rel):
        rel = await _governed_relpath(sandbox, path)
    if not _is_governed_artifact(rel):
        return None
    tool, why = _route_for_governed(rel)
    route = (
        f"Use {tool} to change it ({why})."
        if tool
        else "It is host-managed; do not edit it with a generic write tool."
    )
    return ToolOutcome(
        success=False,
        error=error,
        content=(
            f"Refused — {path} resolves to the host-managed .disco/ namespace ({rel}). {route}"
        ),
        structured={
            "kind": "governed_artifact_rejected",
            "path": path,
            "resolved": rel,
            "route_to": tool,
        },
    )


class ExactReplaceEdit(BaseModel):
    old_string: str = Field(description="Exact text to find — must occur once unless multi=true.")
    new_string: str = Field(
        description="Replacement, written LITERALLY (no regex/backref/template expansion)."
    )


class ExactReplaceArgs(BaseModel):
    path: str = Field(description="Workspace-relative file to edit.")
    edits: list[ExactReplaceEdit] = Field(
        description="One or more exact replacements; applied ALL-or-NOTHING (atomic)."
    )
    # NOTE: there is deliberately NO `require_fresh_read` toggle — the fresh-read/coverage guard
    # is MANDATORY and non-bypassable (a model opt-out would re-open the Mode-B large-file thrash;
    # Codex CD-TOOLS-2 round-1). It is size-gated, so small files never need a separate read.
    expected_sha256: str | None = Field(
        default=None,
        description="If set, refuse unless the file's CURRENT sha256 equals this (optimistic concurrency).",
    )
    multi: bool = Field(
        default=False,
        description="Allow an old_string to match more than once and replace EVERY occurrence.",
    )


class ExactReplaceTool:
    """CD-TOOLS-2 — the atomic exact-replacement primitive (Claude Design dc_*_str_replace).

    Replaces fragile broad file_edit for targeted edits: each old_string must match EXACTLY
    (literal — no whitespace forgiving), the whole batch applies all-or-nothing, and EVERY check
    runs IN MEMORY before a single byte is written (so a failed batch never touches disk — no
    write-then-revert). Reuses the CD-TOOLS-1 fresh-edit guard for stale/elision/grounding."""

    definition = ToolDef(
        name="exact_replace",
        description=(
            "Apply one or more EXACT string replacements to a file, atomically. Each `old_string` "
            "must occur exactly once (set multi=true to replace all occurrences); `new_string` is "
            "written literally (no regex/backref expansion). The whole batch applies all-or-nothing "
            "— if ANY edit fails (no match, duplicate, overlap, or it would introduce a syntax "
            "error) NOTHING is written. Read the file first (the exact text you see is what to "
            "match). Pass expected_sha256 to refuse if the file changed under you."
        ),
        args_model=ExactReplaceArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: ExactReplaceArgs, ctx: ToolContext) -> ToolOutcome:
        import hashlib

        assert ctx.sandbox is not None
        if (g := await _governed_guard(ctx.sandbox, args.path)) is not None:  # CD-TOOLS-4: close the bypass
            return g
        if not args.edits:
            return ToolOutcome(
                success=False, error="EXACT_REPLACE_BATCH_FAILED", content="exact_replace: no edits supplied."
            )
        raw = await ctx.sandbox.read_file(args.path)
        text = raw.decode("utf-8", errors="replace")

        # (1) elision marker in any old/new — never let a render placeholder enter source.
        for e in args.edits:
            if _has_elision_marker(e.old_string, e.new_string):
                return ToolOutcome(
                    success=False,
                    error="ELISION_MARKER_REJECTED",
                    content=(
                        f"exact_replace refused — an edit for {args.path} contains an internal elision "
                        "placeholder (e.g. '<… chars elided …>'); read the file and use the real text."
                    ),
                    structured={
                        "kind": "elision_marker_rejected",
                        "path": args.path,
                        "next_required_action": "file_read",
                        "suggested_args": {"path": args.path},
                    },
                )

        # (2) optimistic concurrency: caller-supplied expected sha must match the current disk bytes.
        cur_sha = hashlib.sha256(raw).hexdigest()
        if args.expected_sha256 is not None and args.expected_sha256 != cur_sha:
            return ToolOutcome(
                success=False,
                error="STALE_FILE_CONTEXT",
                content=(
                    f"exact_replace refused — {args.path} now hashes to {cur_sha[:12]}…, not the expected "
                    f"{args.expected_sha256[:12]}…; it changed since you read it. Read it again, then edit."
                ),
                structured={
                    "kind": "stale_file_context",
                    "path": args.path,
                    "next_required_action": "file_read",
                    "suggested_args": {"path": args.path},
                },
            )

        # (3) per-edit match counts + collect ALL match spans on the ORIGINAL text.
        spans: list[tuple[int, int, str]] = []  # (start, end, new_string)
        applied: list[dict[str, Any]] = []
        for e in args.edits:
            occ = _all_occurrences(text, e.old_string)
            if len(occ) == 0:
                return ToolOutcome(
                    success=False,
                    error="EXACT_REPLACE_NO_MATCH",
                    content=f"exact_replace: old_string not found in {args.path}: {e.old_string[:60]!r}.",
                )
            if len(occ) > 1 and not args.multi:
                return ToolOutcome(
                    success=False,
                    error="EXACT_REPLACE_DUPLICATE_MATCH",
                    content=(
                        f"exact_replace: old_string occurs {len(occ)}× in {args.path} — make it unique or "
                        f"set multi=true to replace all: {e.old_string[:60]!r}."
                    ),
                )
            targets = occ if args.multi else occ[:1]
            for s in targets:
                spans.append((s, s + len(e.old_string), e.new_string))
            applied.append(
                {"old_string": e.old_string[:60], "occurrences": len(occ), "replaced": len(targets)}
            )

        # (4) overlap: two matched regions intersecting on the ORIGINAL text → ambiguous, reject.
        spans.sort(key=lambda t: t[0])
        for i in range(1, len(spans)):
            if spans[i][0] < spans[i - 1][1]:
                return ToolOutcome(
                    success=False,
                    error="EXACT_REPLACE_BATCH_FAILED",
                    content=(
                        f"exact_replace: edits overlap in {args.path} (matched regions intersect) — "
                        "split or de-duplicate them."
                    ),
                )

        # (5) fresh-read/grounding/coverage — MANDATORY, per edit region (reuse the CD-TOOLS-1
        # guard; size-gated so small files pass freely). Non-bypassable: no opt-out arg exists.
        for s, end, _repl in spans:
            lo_line = text.count("\n", 0, s) + 1
            hi_line = text.count("\n", 0, end) + 1
            blocked = guard_fresh_edit(
                ctx.conversation_id, args.path, current_bytes=raw, edit_lines=(lo_line, hi_line),
                line_refusal_read=True,
            )
            if blocked is not None:
                return blocked

        # (6) splice by position (descending so earlier indices stay valid) — str slicing, NOT re.sub,
        #     so '$', '${x}', backrefs in new_string are literal.
        new_text = text
        for s, end, repl in sorted(spans, key=lambda t: t[0], reverse=True):
            new_text = new_text[:s] + repl + new_text[end:]

        # (7) syntax pre-check IN MEMORY (no write yet): the batch must not INTRODUCE new errors.
        pre = _syntax_errors(args.path, text)
        introduced = [er for er in _syntax_errors(args.path, new_text) if er not in pre]
        if introduced:
            return ToolOutcome(
                success=False,
                error="EXACT_REPLACE_BATCH_FAILED",
                content=(
                    f"exact_replace: the batch would introduce syntax error(s) in {args.path}: "
                    f"{'; '.join(introduced)} — NOT applied. Fix the snippet and retry."
                ),
            )

        # (8) all checks passed → ONE atomic write (never write-then-revert). All-or-nothing.
        new_bytes = new_text.encode("utf-8")
        await _atomic_write(ctx.sandbox, args.path, new_bytes)
        # [REL-RC-D] exact_replace is anchored (position-spliced from live-disk snippet matches) →
        # advance grounding to the post-edit bytes so a follow-up same-file edit isn't false-STALE.
        reground_after_anchored_edit(ctx.conversation_id, args.path, new_bytes)
        return ToolOutcome(
            success=True,
            content=f"exact_replace applied {len(spans)} replacement(s) to {args.path}.",
            artifacts=[args.path],
            structured={
                "path": args.path,
                "bytes": len(new_bytes),
                "sha256": hashlib.sha256(new_bytes).hexdigest(),
                "applied": applied,
            },
        )


class SafeWriteFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative file to write (create or overwrite).")
    content: str = Field(description="Full UTF-8 content to write.")
    allow_shrink: bool = Field(
        default=False,
        description="Set true to permit a write that shrinks an existing file by >50% (otherwise refused).",
    )
    expected_sha256: str | None = Field(
        default=None,
        description="If set, refuse unless the file's CURRENT sha256 equals this (also grounds the write).",
    )


class SafeWriteFileTool:
    """CD-TOOLS-3 — the SAFER whole-file writer (a superset of file_write) for governed/large
    artifacts. Validates everything IN MEMORY, then commits atomically (tmp+rename). Guards the
    weak-model whole-file-clobber (a >50% shrink that truncates a built file), refuses writing a
    host-governed `.disco/` artifact via the generic path, and rejects an elision marker."""

    definition = ToolDef(
        name="safe_write_file",
        description=(
            "Write a whole file safely (create or overwrite). Like file_write but with guards: it "
            "refuses to shrink an existing file by >50% (pass allow_shrink=true if intended), "
            "refuses to clobber a host-managed .disco/ artifact (use the semantic tool), rejects "
            "elision placeholders, and writes atomically. Read the file first to overwrite it; "
            "pass expected_sha256 to confirm you have the current version."
        ),
        args_model=SafeWriteFileArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: SafeWriteFileArgs, ctx: ToolContext) -> ToolOutcome:
        import hashlib

        assert ctx.sandbox is not None
        # (1) never let a render elision placeholder become file content.
        if _has_elision_marker(args.content):
            return ToolOutcome(
                success=False,
                error="ELISION_MARKER_REJECTED",
                content=(
                    f"safe_write_file refused — content for {args.path} contains an internal elision "
                    "placeholder (e.g. '<… chars elided …>'); read the file and write the real text."
                ),
                structured={
                    "kind": "elision_marker_rejected",
                    "path": args.path,
                    "next_required_action": "file_read",
                },
            )
        # (2) governed-artifact guard (CD-TOOLS-4) — shared with all generic mutators; routes to
        # the owning semantic tool. Keeps the campaign-named code for safe_write_file.
        if (g := await _governed_guard(ctx.sandbox, args.path, error="SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED")) is not None:
            return g
        # (3) inspect the existing file (None = new).
        old_text: str | None = None
        old_bytes = b""
        try:
            old_bytes = await ctx.sandbox.read_file(args.path)
            old_text = old_bytes.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — absent file → a new write, no prior content to guard
            old_text = None
        matching_sha = False
        if old_text is not None:
            cur_sha = hashlib.sha256(old_bytes).hexdigest()
            if args.expected_sha256 is not None:
                if args.expected_sha256 != cur_sha:
                    return ToolOutcome(
                        success=False,
                        error="STALE_FILE_CONTEXT",
                        content=(
                            f"safe_write_file refused — {args.path} now hashes to {cur_sha[:12]}…, not "
                            f"the expected {args.expected_sha256[:12]}…; it changed since you read it."
                        ),
                        structured={
                            "kind": "stale_file_context",
                            "path": args.path,
                            "next_required_action": "file_read",
                        },
                    )
                matching_sha = True
            # binary-deliverable clobber (parity with file_write) — a text write corrupts a binary.
            ext = args.path.rsplit(".", 1)[-1].lower() if "." in args.path else ""
            if ext in _BINARY_DELIVERABLE_EXTS:
                return ToolOutcome(
                    success=False,
                    error="binary_deliverable_clobber",
                    content=(
                        f"safe_write_file refused — {args.path} is an existing {ext} (a generated binary); "
                        "a text write would corrupt it. It is already delivered."
                    ),
                )
            grounded = _canonical(args.path) in (
                (_read_state.get(ctx.conversation_id) or {}).get("read_since_write") or set()
            )
            # read-before-rewrite (F1 parity): overwrite an existing file only if grounded (read
            # since last write) OR proven current via a matching expected_sha256.
            if not grounded and not matching_sha:
                return _fresh_read_required(
                    args.path, "you have not read this file's current content since it last changed"
                )
            # SHRINK guard — a >50% char shrink truncates a built file (the weak-model clobber).
            if len(args.content) < 0.5 * len(old_text) and not args.allow_shrink and not matching_sha:
                return ToolOutcome(
                    success=False,
                    error="SAFE_WRITE_SHRINK_REJECTED",
                    content=(
                        f"safe_write_file refused — this would shrink {args.path} from {len(old_text)} to "
                        f"{len(args.content)} chars (>50% smaller), which usually means an accidental "
                        "truncation/clobber. Pass allow_shrink=true (or expected_sha256) if intended."
                    ),
                    structured={
                        "kind": "safe_write_shrink_rejected",
                        "path": args.path,
                        "old_chars": len(old_text),
                        "new_chars": len(args.content),
                    },
                )
        # (4) syntax pre-check IN MEMORY — never introduce a new syntax error (no write-then-revert).
        pre = _syntax_errors(args.path, old_text or "")
        introduced = [e for e in _syntax_errors(args.path, args.content) if e not in pre]
        if introduced:
            return ToolOutcome(
                success=False,
                error="syntax_gate_rejected",
                content=(
                    f"safe_write_file refused — this content introduces syntax error(s) in {args.path}: "
                    f"{'; '.join(introduced)} — NOT written. Fix it and retry."
                ),
            )
        # (5) all checks passed → ONE atomic commit (tmp+rename where supported).
        new_bytes = args.content.encode("utf-8")
        await _atomic_write(ctx.sandbox, args.path, new_bytes)
        _clear_grounding(ctx.conversation_id, args.path)
        return ToolOutcome(
            success=True,
            content=f"safe_write_file wrote {len(new_bytes)} bytes to {args.path}.",
            artifacts=[args.path],
            structured={
                "path": args.path,
                "bytes": len(new_bytes),
                "sha256": hashlib.sha256(new_bytes).hexdigest(),
            },
        )
