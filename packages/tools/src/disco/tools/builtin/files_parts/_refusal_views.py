"""Refusal-outcome builders that carry FRESH content back to the model.

Every blocking refusal in the CD-TOOLS-1/REL-6/REL-RC-L family renders the
current disk content (or a window of it) into the refusal's `content`, so the
model can recover in the SAME turn instead of spending a round-trip on a
plain file_read. This module owns that shared rendering + the refusal
outcome shapes; the individual tool `run()` methods just call these.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ...anatomy import ToolOutcome
from ._canonical import _canonical
from ._constants import _LINE_REFUSAL_WINDOW_RADIUS, _REFUSAL_READ_FULL_MAX_BYTES
from ._fuzzy_match import _best_fuzzy_old_match_lines, _find_text_lines
from ._read_state import (
    _conv_state,
    _increment_no_op_edit_count,
    _increment_old_text_not_found_count,
)
from ._text_norm import _number_lines


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


def _numbered_line_window(text: str, *, start_line: int, end_line: int, total_lines: int) -> str:
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
    window_radius: int = _LINE_REFUSAL_WINDOW_RADIUS,
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
        start, end = _line_span(total, attempted_lines, window_radius)

    canon = _canonical(path)
    st = _conv_state(conv_id)
    prior = st["reads"].get(canon) or {}
    anchored_full = prior.get("sha") == sha and bool(prior.get("anchored_full"))
    st["reads"][canon] = {
        "sha": sha,
        "full": full,
        "anchored_full": anchored_full,
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
    structured["next_required_action"] = "retry_targeted_edit"
    structured.pop("suggested_args", None)
    reason = structured.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = (
            "the file changed since your last read"
            if outcome.error == "STALE_FILE_CONTEXT"
            else "the attempted region was not grounded"
        )
    guidance = (
        f"Edit refused — {reason}; nothing was changed. Fresh current content for {path} "
        "is included below and now counts as the required read for a corrected targeted edit. "
        "Retry the corrected edit directly; call file_read only if you need a different region."
    )
    return ToolOutcome(
        success=False,
        error=outcome.error,
        content=guidance + fresh_content,
        structured=structured,
    )


def _no_op_edit_refusal(
    conv_id: str,
    path: str,
    *,
    base_content: str,
    current_bytes: bytes | None,
    attempted_lines: tuple[int, int] | None,
) -> ToolOutcome:
    """REL-RC-L: no-op refusals carry current content and escalate repeated attempts."""
    count = _increment_no_op_edit_count(conv_id, path)
    content = base_content
    structured: dict[str, Any] = {
        "kind": "no_op_edit",
        "path": path,
        "no_op_edit_count": count,
    }
    if current_bytes is not None and (
        attempted_lines is not None or len(current_bytes) <= _REFUSAL_READ_FULL_MAX_BYTES
    ):
        sha = hashlib.sha256(current_bytes).hexdigest()
        fresh_content, delivered = _line_refusal_read(
            conv_id,
            path,
            current_bytes=current_bytes,
            sha=sha,
            attempted_lines=attempted_lines,
        )
        content += fresh_content
        structured["delivered_read"] = delivered
    if count >= 2:
        content += (
            "\n\nRepeated no-op edit: this change may ALREADY be applied — do not "
            "re-send this edit; verify the region above, then update plan progress "
            "or move to the next step."
        )
    return ToolOutcome(
        success=False,
        error="no_op_edit",
        content=content,
        structured=structured,
    )


def _no_op_write_refusal(
    conv_id: str,
    path: str,
    *,
    tool_name: str,
    current_bytes: bytes,
) -> ToolOutcome:
    """REL-RC-M: byte-identical whole-file writes are refused, not "successful" no-ops."""
    count = _increment_no_op_edit_count(conv_id, path)
    content = (
        f"{tool_name} refused: {path} already contains exactly this content — no change "
        "was needed. If you were verifying, the content is confirmed below; update plan "
        "progress or move to the next step."
    )
    structured: dict[str, Any] = {
        "kind": "no_op_write",
        "path": path,
        "no_op_write_count": count,
    }
    if len(current_bytes) <= _REFUSAL_READ_FULL_MAX_BYTES:
        sha = hashlib.sha256(current_bytes).hexdigest()
        fresh_content, delivered = _line_refusal_read(
            conv_id,
            path,
            current_bytes=current_bytes,
            sha=sha,
            attempted_lines=None,
        )
        content += fresh_content
        structured["delivered_read"] = delivered
    if count >= 2:
        content += (
            "\n\nRepeated no-op write: this file may ALREADY contain the intended "
            "change — do not re-send this write; verify the content above, then "
            "update plan progress or move to the next step."
        )
    return ToolOutcome(
        success=False,
        error="no_op_write",
        content=content,
        structured=structured,
    )


def _old_text_not_found_refusal(
    conv_id: str,
    path: str,
    *,
    current_bytes: bytes,
    attempted_old: str,
    attempted_new: str,
    error: str,
    tool_name: str = "file_edit",
    base_content: str | None = None,
) -> ToolOutcome:
    """REL-6: anchored old-text misses are self-recovering refusal reads."""
    count = _increment_old_text_not_found_count(conv_id, path)
    text = current_bytes.decode("utf-8", errors="replace")
    already_applied = _find_text_lines(text, attempted_new)
    if already_applied is not None:
        start, end = already_applied
        content = (
            f"the replacement text is already present at lines {start}-{end} — "
            "the edit already applied; do not re-issue it."
        )
    else:
        content = (
            f"{tool_name} refused: your `old` text was not found in {path} "
            "(the file has changed since you composed it, or the anchor differs in whitespace / "
            "unicode — e.g. decorative characters often drift)."
        )
        if base_content:
            content += "\n\n" + base_content
    structured: dict[str, Any] = {
        "kind": "old_text_not_found",
        "path": path,
        "old_text_not_found_count": count,
    }
    delivered_content = False
    if already_applied is not None:
        delivered_content = len(current_bytes) <= _REFUSAL_READ_FULL_MAX_BYTES
        attempted_lines = already_applied if delivered_content else None
    else:
        match_lines = _best_fuzzy_old_match_lines(text, attempted_old)
        delivered_content = (
            len(current_bytes) <= _REFUSAL_READ_FULL_MAX_BYTES or match_lines is not None
        )
        attempted_lines = match_lines
    if delivered_content:
        sha = hashlib.sha256(current_bytes).hexdigest()
        if len(current_bytes) > _REFUSAL_READ_FULL_MAX_BYTES and attempted_lines is not None:
            content += "\n\nBest fuzzy match region in the current file:"
        fresh_content, delivered = _line_refusal_read(
            conv_id,
            path,
            current_bytes=current_bytes,
            sha=sha,
            attempted_lines=attempted_lines,
            window_radius=10,
        )
        content += fresh_content
        structured["delivered_read"] = delivered
    elif len(current_bytes) > _REFUSAL_READ_FULL_MAX_BYTES:
        content += (
            "\n\nNo plausible fuzzy match was found, and the file is larger than the "
            "64KB refusal-read cap. Read the exact region you want to change, then retry."
        )
    if already_applied is None:
        content += (
            "\n\nAnchor your next edit on the CURRENT text shown above (copy it exactly), "
            "or use file_replace_lines with the line numbers shown."
        )
    if count >= 2:
        if already_applied is not None:
            content += "\n\nRepeated already-applied edit: do not re-send this replacement."
        elif delivered_content:
            content += (
                "\n\nRepeated old-text miss: your `old` text does not appear in the file "
                "— do NOT re-send it; the exact current content is above, copy the region "
                "you want to change precisely."
            )
        else:
            content += (
                "\n\nRepeated old-text miss: your `old` text does not appear in the file "
                "— do NOT re-send it; this file is too large to include without a match "
                "anchor, so read the exact region you want to change and copy it precisely."
            )
    return ToolOutcome(
        success=False,
        error=error,
        content=content,
        structured=structured,
    )
