"""CD-TOOLS-1 — the fresh-edit guard. An exact/line edit may not run against stale, incomplete,
or elided model-visible source. These exercise the guard logic directly (it operates on the
module read-state via record_read / mark_read), covering every required negative + the legit
ranged-read positive (no false-block)."""

from __future__ import annotations

import hashlib

from disco.tools.builtin.files import (
    guard_fresh_edit,
    mark_read,
    record_read,
    reset_read_tracker,
)

CONV = "conv-fresh-edit"
PATH = "src/App.tsx"
# A LARGE file (>1500 chars, the _ARG_SNIP_CHARS elision threshold) so the fresh-read requirement
# actually applies — Mode B is a large-file phenomenon. 60 padded lines, with line2/line4/line5
# present as edit anchors.
_LINES = [f"line{i} {'x' * 40}" for i in range(1, 61)]
BODY = ("\n".join(_LINES) + "\n").encode("utf-8")
N = len(_LINES)
SHA = hashlib.sha256(BODY).hexdigest()
assert len(BODY) > 1500  # the guard only engages above the elision threshold

# small-file anchors stay literal: "line2" etc. occur once each as a line prefix.


def _ground_full() -> None:
    """Simulate a full file_read of the current bytes: a complete whole-file range + the
    read-since-write grounding bit."""
    reset_read_tracker()
    record_read(CONV, PATH, sha=SHA, start_line=1, end_line=N, full=True)
    mark_read(CONV, PATH)


def test_edit_after_full_fresh_read_is_allowed() -> None:
    _ground_full()
    assert guard_fresh_edit(CONV, PATH, current_bytes=BODY, old="line2", new="X", edit_lines=(2, 2)) is None


def test_ungrounded_edit_is_blocked_fresh_read_required() -> None:
    reset_read_tracker()  # never read / grounded
    out = guard_fresh_edit(CONV, PATH, current_bytes=BODY, old="line2", new="X", edit_lines=(2, 2))
    assert out is not None and out.success is False and out.error == "FRESH_READ_REQUIRED"
    assert (out.structured or {}).get("next_required_action") == "file_read"


def test_edit_after_file_changed_since_read_is_stale() -> None:
    _ground_full()  # recorded against SHA(BODY)
    changed = BODY + b"line6 added by another tool\n"  # different sha now on disk
    out = guard_fresh_edit(CONV, PATH, current_bytes=changed, old="line2", new="X", edit_lines=(2, 2))
    assert out is not None and out.error == "STALE_FILE_CONTEXT"


def test_elision_marker_in_new_is_rejected() -> None:
    _ground_full()
    out = guard_fresh_edit(
        CONV, PATH, current_bytes=BODY, old="line2",
        new="line2\n<4500 chars elided — re-issue the call>", edit_lines=(2, 2),
    )
    assert out is not None and out.error == "ELISION_MARKER_REJECTED"


def test_elision_marker_in_old_is_rejected() -> None:
    _ground_full()
    out = guard_fresh_edit(
        CONV, PATH, current_bytes=BODY, old="<120 chars elided>", new="real", edit_lines=None
    )
    assert out is not None and out.error == "ELISION_MARKER_REJECTED"


def test_partial_read_not_covering_region_is_blocked() -> None:
    reset_read_tracker()
    record_read(CONV, PATH, sha=SHA, start_line=1, end_line=2, full=False)  # only saw lines 1-2
    mark_read(CONV, PATH)
    out = guard_fresh_edit(CONV, PATH, current_bytes=BODY, old="line5", new="X", edit_lines=(5, 5))
    assert out is not None and out.error == "FRESH_READ_REQUIRED"


def test_ranged_read_covering_region_is_allowed() -> None:
    # a ranged read that DOES cover the edited lines must NOT be false-blocked (no whole-file read forced).
    reset_read_tracker()
    record_read(CONV, PATH, sha=SHA, start_line=3, end_line=5, full=False)
    mark_read(CONV, PATH)
    assert guard_fresh_edit(CONV, PATH, current_bytes=BODY, old="line4", new="X", edit_lines=(4, 4)) is None


def test_partial_read_undetermined_region_is_blocked() -> None:
    # a forgiving/whitespace match (exact find missed → edit_lines=None) on a PARTIALLY-read file
    # must fail closed: we cannot prove the mutated lines were seen (Codex round-1 hole).
    reset_read_tracker()
    record_read(CONV, PATH, sha=SHA, start_line=1, end_line=3, full=False)
    mark_read(CONV, PATH)
    out = guard_fresh_edit(CONV, PATH, current_bytes=BODY, old="zz", new="y", edit_lines=None)
    assert out is not None and out.error == "FRESH_READ_REQUIRED"


def test_full_read_undetermined_region_is_allowed() -> None:
    # a FULL read covers the whole file → an undetermined region is fine (no false-block).
    _ground_full()
    assert guard_fresh_edit(CONV, PATH, current_bytes=BODY, old="zz", new="y", edit_lines=None) is None


def test_blocked_edit_returns_no_mutation_signal() -> None:
    # the guard only RETURNS a blocking outcome; it never touches the filesystem itself.
    reset_read_tracker()
    out = guard_fresh_edit(CONV, PATH, current_bytes=BODY, old="line2", new="X", edit_lines=(2, 2))
    assert out is not None and out.success is False  # the caller must early-return on this
