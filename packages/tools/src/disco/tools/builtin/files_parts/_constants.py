"""Shared constants, thresholds, and the elision-marker regex for the file tools.

Extracted from ``files.py`` to reduce module complexity; the public facade
(``files.py``) re-imports every name here unchanged, so every existing import
path (production code and tests) keeps working exactly as before.
"""

from __future__ import annotations

import re

from ...anatomy import Capability

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
def _pressure_directive(path: str = "", shown: int = 0, total: int = 0) -> str:
    """The large-file read directive, RENDERED for the file it suppressed.

    Constraint 4: this fires on EVERY whole-file read of a large file, so an
    agent re-reading three big files got the identical paragraph three times and
    was never told which read had been truncated or by how much. The path and
    the measured line counts come from the same slice the reader just built.
    """
    named = f"`{path}` " if path else ""
    measured = f" You were shown lines 1-{shown} of {total}." if shown and total else ""
    return (
        f"{named}file is large; the full page is suppressed.{measured} Do NOT "
        "re-read this file whole — pick the symbol/class/section you need, then "
        "either:\n"
        "  (a) run a grep/ripgrep tool to locate it (e.g. `rg -n 'Symbol' <path>`), "
        "then file_read a targeted line range (`offset=<n>, limit=<m>`); or\n"
        "  (b) file_read a small line range directly to scan structure.\n"
        "Reading this file whole again will just be truncated the same way."
    )


_PRESSURE_DIRECTIVE = _pressure_directive()

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

# CD-TOOLS-1 — the internal elision-marker family, re-expressed locally so the tools
# package does not import from disco.core here. Mirrors core.events
# _ELISION_MARKER_RE / _ELISION_PARAPHRASE_RE: the canonical
# "[[DISCO-ELIDED: N chars ...]]" sentinel and historical angle-bracket render
# markers the model must never echo back into source.
_EDIT_ELISION_RE = re.compile(
    r"(?:"
    # F01: reserved canonical prefix, including a one-bracket/truncated form and
    # the historical non-count-first variant observed in the poisoned CSS.
    r"\[{1,2}\s*DISCO-ELIDED\s*:"
    r"|"
    r"\[\[\s*DISCO-ELIDED:\s*\d[\d,]*\s*chars\b[^\]]*?\]\]"
    r"|"
    r"<\s*\d[\d,]*\s*chars\b[^>]*?\b(?:elided|full\s+content|placeholder)\b[^>]*>"
    r"|"
    r"<(?=[^>]*\b(?:elided|full\s+content|placeholder)\b)[^>]*"
    r"(?:re-issue the call or file_read the path|do not copy this placeholder"
    r"|do not copy or re-send|already applied to the workspace)[^>]*>"
    r"|"
    # Historical angle marker with a truncated/missing closing bracket.
    r"<\s*(?:\d[\d,]*\s*chars?|content)\b[^\r\n>]{0,500}"
    r"\b(?:elided|placeholder|full\s+content)\b"
    r")",
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
_UPDATED_REGION_WINDOW_RADIUS = 10
_FILE_WRITE_SUCCESS_HEAD_LINES = 40

# Provider-visible argument guidance. This is emitted as JSON Schema
# ``maxLength`` metadata without making Pydantic reject a fully-formed larger
# call at runtime. Providers have repeatedly truncated oversized tool-call JSON
# before local validation could run; bounded chunks are already the system
# contract, and this makes that same contract visible at generation time.
_MODEL_WRITE_CHUNK_MAX_CHARS = 12_000
