"""CD-TOOLS-1 fresh-edit guard: refuse a mutation when the model lacks fresh,
complete grounding of the edit region.

``guard_fresh_edit`` is decomposed into three single-purpose steps — an
elision check, a read-grounding lookup, and a violation classifier — so each
stays small and independently readable; the top-level function only sequences
them and applies the shared line-refusal-read rendering to whichever
violation (if any) came back.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ...anatomy import ToolOutcome
from ._canonical import _canonical
from ._constants import _GUARD_FRESH_READ_MIN_BYTES
from ._elision import _has_elision_marker
from ._read_state import _covers, _read_state
from ._refusal_views import _fresh_read_required, _with_line_refusal_read


def _elision_rejected_outcome(path: str, old: str | None, new: str | None) -> ToolOutcome | None:
    if not _has_elision_marker(old, new):
        return None
    return ToolOutcome(
        success=False,
        error="ELISION_MARKER_REJECTED",
        content=(
            f"Edit refused — the edit text for {path} contains an internal elision "
            "placeholder (e.g. '[[DISCO-ELIDED: ...]]'); that marker is render-only "
            "and must never be written into a file. Read the file, then edit with the "
            "real text."
        ),
        structured={
            "kind": "elision_marker_rejected",
            "path": path,
            "next_required_action": "file_read",
            "suggested_args": {"path": path},
        },
    )


def _read_grounding(
    conv_id: str, path: str, sha: str, *, anchored: bool
) -> tuple[dict[str, Any] | None, bool]:
    """Look up the sha-aware read record for `path` and whether the model is
    currently grounded for an edit at `sha` (see `guard_fresh_edit` docstring
    for the three grounding signals combined here)."""
    st = _read_state.get(conv_id) or {}
    canon = _canonical(path)
    rec = (st.get("reads") or {}).get(canon)
    rec_current = rec is not None and rec.get("sha") == sha
    grounded = (
        canon in (st.get("read_since_write") or set())
        or (rec_current and canon in (st.get("targeted_read_grounded") or set()))
        or (anchored and canon in (st.get("edit_grounded") or set()))
    )
    return rec, grounded


def _fresh_edit_violation(
    path: str,
    *,
    sha: str,
    rec: dict[str, Any] | None,
    grounded: bool,
    edit_lines: tuple[int, int] | None,
) -> ToolOutcome | None:
    """Classify the (rec, grounded) state into the blocking outcome it implies,
    or None when the edit is fully grounded. Checks, in order: (1) the file
    changed under the model since its last read → STALE_FILE_CONTEXT; (2) the
    model has no current grounding since the last write (editing blind) →
    FRESH_READ_REQUIRED; (3) the only grounding is a PARTIAL read not covering
    the edit region → FRESH_READ_REQUIRED."""
    if rec is not None and rec.get("sha") != sha:
        return ToolOutcome(
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
    if not grounded:
        return _fresh_read_required(
            path, "you have not read this file's current content since it last changed"
        )
    # grounded, current sha: if the only sha-aware grounding is a PARTIAL read (not the whole
    # file), the edited region must fall inside what was actually read. A FULL read (rec.full)
    # covers everything, so it is exempt. When the region is UNDETERMINED (edit_lines is None —
    # e.g. file_edit/file_str_replace matched `old` via the forgiving/whitespace-tolerant path
    # after an exact find missed), we cannot prove the mutated lines were seen → fail closed so a
    # partial read can't mutate unread lines (Codex round-1).
    if rec is not None and not rec.get("full"):
        if edit_lines is None:
            return _fresh_read_required(
                path,
                "could not confirm the exact lines you are editing were in the part of the file "
                "you read",
            )
        if not _covers(rec.get("ranges", []), edit_lines[0], edit_lines[1]):
            return _fresh_read_required(
                path, "the lines you are editing were not in the part of the file you read"
            )
    return None


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
    elision_outcome = _elision_rejected_outcome(path, old, new)
    if elision_outcome is not None:
        return elision_outcome
    # Small files stay fully in the model's context (never elided) → the model has the bytes;
    # requiring a fresh read there would only break legitimate small-file edits.
    if len(current_bytes) <= _GUARD_FRESH_READ_MIN_BYTES:
        return None
    sha = hashlib.sha256(current_bytes).hexdigest()
    # [REL-RC-D/G] grounding for an EDIT comes from the coarse read_since_write bit, a current
    # refusal-delivered targeted read, or — for ANCHORED callers only — prior edit_grounded.
    # edit_grounded does NOT count for LINE-BASED callers: an anchored edit can shift line numbers.
    # A line-edit refusal can, however, deliver and record fresh numbered content as `reads[path]`
    # plus targeted_read_grounded; that grounds corrected targeted edits, not blind file_write.
    rec, grounded = _read_grounding(conv_id, path, sha, anchored=anchored)
    violation = _fresh_edit_violation(
        path, sha=sha, rec=rec, grounded=grounded, edit_lines=edit_lines
    )
    if violation is None:
        return None
    return _with_line_refusal_read(
        violation,
        conv_id,
        path,
        current_bytes=current_bytes,
        sha=sha,
        attempted_lines=attempted_lines or edit_lines,
        anchored=anchored,
        line_refusal_read=line_refusal_read,
    )
