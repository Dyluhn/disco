"""Post-mutation "updated region" success-content rendering + the CD-TOOLS-1
grounding it records for the model on a successful edit/write."""

from __future__ import annotations

import hashlib
import re

from ._canonical import _canonical
from ._constants import (
    _FILE_WRITE_SUCCESS_HEAD_LINES,
    _REFUSAL_READ_FULL_MAX_BYTES,
    _UPDATED_REGION_WINDOW_RADIUS,
)
from ._read_state import _conv_state
from ._refusal_views import _line_span, _numbered_line_window


def _bounded_updated_region_view(view: str) -> str:
    raw = view.encode("utf-8")
    if len(raw) <= _REFUSAL_READ_FULL_MAX_BYTES:
        return view
    notice = "\n[updated-region view truncated at 64KB]"
    budget = max(0, _REFUSAL_READ_FULL_MAX_BYTES - len(notice.encode("utf-8")))
    return raw[:budget].decode("utf-8", errors="ignore").rstrip() + notice


def _post_change_line_span(before: str, after: str) -> tuple[int, int]:
    """Best-effort 1-based span of changed lines in the post-edit file."""
    from difflib import SequenceMatcher

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    spans: list[tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in SequenceMatcher(None, before_lines, after_lines).get_opcodes():
        if tag == "equal":
            continue
        spans.append((j1 + 1, max(j2, j1 + 1)))
    if not spans:
        return (1, max(1, len(after_lines)))
    return (min(lo for lo, _hi in spans), max(hi for _lo, hi in spans))


def _record_success_grounding(
    conv_id: str,
    path: str,
    post_write_bytes: bytes,
    *,
    start_line: int,
    end_line: int,
    full: bool,
    model_authored_full: bool,
    preserve_prior_anchored_full: bool,
) -> None:
    """Advance model-visible grounding across a successful owned mutation.

    A whole-file write is fully known because the model supplied every committed
    byte. Likewise, a targeted edit preserves an earlier full-file view: the
    model knew the old file and supplied the exact change. Partial views remain
    partial, so edits into genuinely unseen regions still fail closed.
    """
    canon = _canonical(path)
    st = _conv_state(conv_id)
    prior = st["reads"].get(canon) or {}
    prior_anchored_full = preserve_prior_anchored_full and bool(
        prior.get("full") or prior.get("anchored_full")
    )
    st["read_since_write"].add(canon)
    st["edit_grounded"].add(canon)
    st["targeted_read_grounded"].add(canon)
    st["reads"][canon] = {
        "sha": hashlib.sha256(post_write_bytes).hexdigest(),
        "full": full,
        "anchored_full": model_authored_full or prior_anchored_full,
        "ranges": [(start_line, end_line)],
    }
    st["no_op_edit_counts"].pop(canon, None)
    st["old_text_not_found_counts"].pop(canon, None)


def _updated_region_success_content(
    conv_id: str,
    path: str,
    post_write_bytes: bytes,
    *,
    prefix: str,
    changed_lines: tuple[int, int] | None = None,
    file_write_head: bool = False,
    model_authored_full: bool = False,
    preserve_prior_anchored_full: bool = True,
) -> str:
    text = post_write_bytes.decode("utf-8", errors="replace")
    total = len(text.splitlines())
    if total <= 0:
        start, end = (1, 0)
    elif file_write_head:
        start, end = (1, min(total, _FILE_WRITE_SUCCESS_HEAD_LINES))
    else:
        start, end = _line_span(total, changed_lines or (1, 1), _UPDATED_REGION_WINDOW_RADIUS)
    numbered = (
        _numbered_line_window(text, start_line=start, end_line=end, total_lines=total)
        if total > 0 and end >= start
        else ""
    )
    parts = [f"applied — lines {start}-{end} now read:"]
    if numbered:
        parts.append(numbered)
    if file_write_head:
        # A WHOLE-FILE write is the one case where the host can honestly certify
        # the entire artifact: the committed bytes ARE the content the model
        # supplied, and they are digested right here. Say so.
        #
        # Counted-promotion evidence 2026-07-27 (p4_ff_react_steer seed 400005):
        # every file_write was immediately followed by a file_read of the same
        # path — write 1777 chars / read back 7320, write 1050 / read back 8831.
        # The receipt showed only a head window and a line count, so it never
        # confirmed the TAIL landed; re-reading was the rational way to find out.
        # The host already knew. This states the fact instead of making the model
        # spend a turn rediscovering it.
        #
        # Deliberately scoped to whole-file writes: after a TARGETED edit the
        # model did not supply the whole file, so the same claim would be false.
        # This is a statement of fact, not a prohibition — re-reading remains
        # correct whenever external mutation, truncation, or verification calls
        # for it.
        parts.append(f"[total lines: {total}]")
        parts.append(
            f"[complete — {len(post_write_bytes)} bytes, sha256 "
            f"{hashlib.sha256(post_write_bytes).hexdigest()[:12]}. The file now contains "
            f"EXACTLY the content you supplied, verbatim; only the head is echoed above "
            f"to save context. A read-back to confirm this write is unnecessary.]"
        )
    raw_view = "\n".join(parts)
    view = _bounded_updated_region_view(raw_view)
    delivered_end = end
    if view != raw_view:
        delivered_numbers = [int(m.group(1)) for m in re.finditer(r"(?m)^\s*(\d+)\t", view)]
        delivered_end = max(delivered_numbers) if delivered_numbers else start - 1
    full = total <= 0 or (start == 1 and delivered_end >= total)
    _record_success_grounding(
        conv_id,
        path,
        post_write_bytes,
        start_line=start,
        end_line=delivered_end,
        full=full,
        model_authored_full=file_write_head or model_authored_full,
        preserve_prior_anchored_full=preserve_prior_anchored_full,
    )
    return f"{prefix}\n\n{view}"
