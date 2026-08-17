"""Harness-owned destructive-elision detector — independent of product-private code.

This mirrors the tested behavior of ``disco.core.observations.scan_for_destructive_elision``
without importing product-private modules. The output-truth oracle must not depend
on product internals for its own evidence adjudication.

Rule set v1: a finished deliverable must not contain unrecoverable elision
placeholder text ("(elided)" / "[trimmed]" / "content omitted" / "truncated for
brevity" with no recover cue). A marker paired with a recover cue on the same
line is fine — it tells the model how to fetch the omitted part.
"""

from __future__ import annotations

# Exact phrases (case-insensitive) that signal content was thrown away.
DESTRUCTIVE_ELISION_MARKERS: tuple[str, ...] = (
    "(elided)",
    "[trimmed]",
    "content omitted",
    "[content omitted]",
    "truncated for brevity",
)

# Tokens that make a truncation RECOVERABLE — if any appears on the same line as a
# marker, the truncation is fine (it tells the model how to get the rest).
RECOVER_CUES: tuple[str, ...] = (
    "file_read",
    "file_edit",
    "re-run",
    "rerun",
    "offset=",
    "grep",
    "read the full",
    "full output at",
    "full diagnostics at",
    "read the manifest",
    "read more",
)


def scan_for_destructive_elision(text: str) -> list[str]:
    """Return the offending lines: those containing a DESTRUCTIVE_ELISION_MARKER
    with NO RECOVER_CUE on the same line. Empty list = clean.
    """
    out: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(m in low for m in DESTRUCTIVE_ELISION_MARKERS) and not any(
            c in low for c in RECOVER_CUES
        ):
            out.append(line.strip())
    return out
