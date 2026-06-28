"""CXT-5 tests: the destructive-elision scan engine + recoverable_excerpt, and a
regression that the in-tree recoverable markers are NOT flagged."""

from __future__ import annotations

from disco.core.observations import (
    DESTRUCTIVE_ELISION_MARKERS,
    recoverable_excerpt,
    scan_for_destructive_elision,
)


# --- scan: positive (destructive, no recover cue) -----------------------------
def test_scan_flags_forbidden_markers_without_recover_cue() -> None:
    for marker in DESTRUCTIVE_ELISION_MARKERS:
        text = f"some source\n...{marker}...\nmore source"
        flagged = scan_for_destructive_elision(text)
        assert flagged, f"{marker!r} should be flagged when no recover cue present"


def test_scan_flags_classic_destructive_phrases() -> None:
    bad = "def f():\n    # ...(content omitted)...\n    pass\n# truncated for brevity"
    flagged = scan_for_destructive_elision(bad)
    assert len(flagged) == 2


# --- scan: negative (recoverable markers in-tree must NOT flag) ---------------
def test_scan_does_not_flag_recoverable_markers() -> None:
    recoverable = "\n".join(
        [
            "… [snipped 1,234 chars — re-run the tool or use file_read for the full output] …",
            "… [9,000 more chars — this file is large. file_read(path, offset, limit)] …",
            "[lines 1-50 of 900; read more with offset=51]",
            "…[truncated; full output at .disco-spill-x.log — file_read or grep it]",
            "… (truncated — file_read the manifest files directly for the full body)",
            "[full browser diagnostics (80 console, 30 network) at .disco-spill-browser-x.json — file_read it]",
        ]
    )
    assert scan_for_destructive_elision(recoverable) == []


def test_scan_marker_with_cue_on_same_line_is_clean() -> None:
    # a destructive phrase is tolerated IF the line names a recover path
    line = "content omitted here — file_read the path for the rest"
    assert scan_for_destructive_elision(line) == []


# --- recoverable_excerpt ------------------------------------------------------
def test_recoverable_excerpt_partial() -> None:
    full = "\n".join(f"line{i}" for i in range(1, 101))  # 100 lines
    exc = recoverable_excerpt(full, path="src/App.tsx", shown_start=1, shown_end=40)
    assert exc["kind"] == "file_excerpt"
    assert exc["complete"] is False
    assert exc["shown_ranges"] == [[1, 40]]
    assert exc["omitted_ranges"] == [[41, 100]]
    assert len(exc["sha256"]) == 64
    assert exc["recover"]["tool"] == "file_read"
    assert exc["recover"]["args"]["offset"] == 41


def test_recoverable_excerpt_complete() -> None:
    full = "a\nb\nc"
    exc = recoverable_excerpt(full, path="x.txt", shown_start=1, shown_end=3)
    assert exc["complete"] is True
    assert exc["omitted_ranges"] == []


def test_recoverable_excerpt_sha_stable() -> None:
    full = "stable content\nsecond line"
    a = recoverable_excerpt(full, path="p", shown_start=1, shown_end=1)
    b = recoverable_excerpt(full, path="p", shown_start=1, shown_end=1)
    assert a["sha256"] == b["sha256"]
