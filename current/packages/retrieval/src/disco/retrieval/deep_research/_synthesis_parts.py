"""Extracted helpers for `deep_research.synthesis` (Epic 11-B, PKG-11-RETRIEVAL).

Split out of the old monolithic `synthesis.py` to clear its module-size cap
(>700 logical) and two callables' complexity caps (`_repair_tables`,
`synthesize_section` — both were over the mccabe-15 / logical-100 limits).
Each piece here owns exactly one decision; none of it changes behaviour.

This module imports its parent **lazily**: `from . import synthesis as
_synthesis` below binds the module object only (always safe — the submodule
is registered in `sys.modules` the instant its import begins, before any of
its top-level code runs). The few synthesis-private names this file needs
(`_grounded_extractive_fallback`, `_RouterWithCtx`, `_SYNTHESIS_TEMPERATURE`)
are resolved as `_synthesis.<name>` attribute lookups **inside function
bodies**, i.e. at call time, not at import time. `synthesis.py` imports THIS
module at its own top level (the normal parent-imports-child direction), so
by the time anything here is actually called, `synthesis.py` has long since
finished executing and every one of those attributes exists. A top-level
`from .synthesis import _grounded_extractive_fallback` here would instead
fail at import time, since synthesis.py hasn't reached that definition yet
when it imports this module.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from disco.core import ReportSection
from disco.core.events import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelRole,
)
from disco.core.think import strip_think_spans

from ..models import Passage
from . import synthesis as _synthesis
from .gather import GatherLegContext

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# ---------------------------------------------------------------------------
# BW-07 table repair (PY-0710: `_repair_tables` mccabe was 17, cap 15) — split
# so each piece owns one decision: gathering a candidate run, judging whether
# it's really a table, and rebuilding a confirmed one.
# ---------------------------------------------------------------------------

_DELIM_CELL_RE = re.compile(r"^\s*:?-{1,}:?\s*$")


def _is_delimiter_row(line: str) -> bool:
    """A GFM table delimiter row — every cell is ``-``/``:--``/``--:``/``:-:``."""
    s = line.strip()
    if "|" not in s or "-" not in s:
        return False
    inner = s.strip("|")
    cells = inner.split("|")
    return bool(cells) and all(_DELIM_CELL_RE.match(c) for c in cells)


def _normalize_table_row(line: str, ncols: int) -> str:
    """Re-emit a pipe row with leading/trailing pipes and exactly ``ncols``
    cells (pad short, clip long) so remark-gfm parses it."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = [c.strip() for c in inner.split("|")]
    if len(cells) < ncols:
        cells += [""] * (ncols - len(cells))
    elif len(cells) > ncols:
        cells = cells[:ncols]
    return "| " + " | ".join(cells) + " |"


def _gather_pipe_run(lines: list[str], start: int, n: int) -> list[str]:
    """Collect a run of consecutive pipe-bearing, non-fence, non-blank lines
    starting at ``start``."""
    run: list[str] = []
    j = start
    while j < n:
        lj = lines[j]
        if lj.lstrip().startswith("```") or "|" not in lj or not lj.strip():
            break
        run.append(lj)
        j += 1
    return run


def _header_cell_count(row: str) -> int:
    return len([c for c in row.strip().strip("|").split("|")])


def _looks_like_table(run: list[str], header_cells: int) -> bool:
    """A run qualifies as a real table if it already carries a delimiter row,
    or has >= 2 rows with a >= 2-cell header. A lone single-pipe line (a
    truncated fragment, or prose that happens to contain a ``|``) does not."""
    has_delim = any(_is_delimiter_row(r) for r in run)
    return has_delim or (len(run) >= 2 and header_cells >= 2)


def _rebuild_table(run: list[str], ncols: int) -> list[str]:
    """Normalize a confirmed table run: header + injected/kept delimiter row +
    body, each re-emitted to exactly ``ncols`` cells."""
    rebuilt: list[str] = [_normalize_table_row(run[0], ncols)]
    body = run[1:]
    if body and _is_delimiter_row(body[0]):
        body = body[1:]
    rebuilt.append("| " + " | ".join(["---"] * ncols) + " |")
    for r in body:
        rebuilt.append(_normalize_table_row(r, ncols))
    return rebuilt


def repair_tables(markdown: str) -> str:
    """BW-07 — repair malformed GFM tables so remark-gfm parses them instead of
    falling back to a literal-pipe paragraph. The preserved public entry point
    is `synthesis._repair_tables`, a thin wrapper around this function."""
    lines = markdown.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence or "|" not in line:
            out.append(line)
            i += 1
            continue

        run = _gather_pipe_run(lines, i, n)
        j = i + len(run)
        header_cells = _header_cell_count(run[0])
        if not _looks_like_table(run, header_cells):
            out.extend(run)
            i = j
            continue

        out.extend(_rebuild_table(run, max(2, header_cells)))
        i = j
    return "\n".join(out)


# ---------------------------------------------------------------------------
# `synthesize_section` decomposition (PY-0711/0712: logical 185 / cap 100,
# mccabe 26 / cap 15) — the truncation-continuation loop and the shared
# extractive-fallback-or-bail path, each in its own callable.
# ---------------------------------------------------------------------------


def _join_continuation_text(markdown: str, stripped_extra: str) -> str:
    """Join a continuation fragment onto ``markdown`` per the three BW-07
    cases: a clean line/word boundary (join on a fresh line), a table row
    split right after its closing pipe but before the newline (also a fresh
    line — a direct glue would fuse two rows via a tell-tale ``||``), or a cut
    mid-token/mid-cell (glue with no separator so the fragment completes)."""
    if markdown[-1:].isspace():
        return markdown.rstrip() + "\n" + stripped_extra
    if markdown[-1:] == "|" and stripped_extra[:1] == "|":
        return markdown + "\n" + stripped_extra
    return markdown + stripped_extra


async def continue_truncated_section(
    router: LLMRouter,
    instruction: str,
    markdown: str,
    resp: CompletionResponse,
    *,
    leg_context: GatherLegContext,
) -> str:
    """BW-07 (2) truncation guard: continue a ``finish_reason == 'length'``
    section from its exact cut point, bounded to 2 continuations."""
    cont = 0
    while resp.finish_reason == "length" and cont < 2:
        cont += 1
        try:
            resp = await cast(_synthesis._RouterWithCtx, router).complete(
                CompletionRequest(
                    profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                    messages=[
                        LLMMessage(
                            role="system",
                            content=_synthesis._EVIDENCE_SYSTEM_PROMPT,
                        ),
                        LLMMessage(role="user", content=instruction),
                        LLMMessage(role="assistant", content=markdown.rstrip()),
                        LLMMessage(
                            role="user",
                            content=(
                                "Your previous reply was cut off. Continue "
                                "EXACTLY where you stopped — do not repeat any "
                                "earlier text and do not add a preamble. If you "
                                "were mid-table, finish the table."
                            ),
                        ),
                    ],
                    temperature=_synthesis._SYNTHESIS_TEMPERATURE,
                    max_tokens=1400,
                ),
                context=leg_context.call_context,
            )
        except Exception:  # noqa: BLE001 — keep the partial section
            break
        extra = strip_think_spans(resp.text, keep_edge_whitespace=True)
        if not extra.strip():
            break
        markdown = _join_continuation_text(markdown, extra.lstrip())
    return markdown


async def fallback_section_or_markdown(
    passages: list[Passage],
    *,
    section_id: str,
    title: str,
    emit: EmitFn,
    reason: str,
    empty_markdown: str,
) -> tuple[str, ReportSection | None]:
    """Grounded extractive fallback + telemetry. If the fallback is ALSO
    empty, returns the honest-failure `ReportSection` so the caller can
    short-circuit; never erases evidence to report the telemetry."""
    markdown = _synthesis._grounded_extractive_fallback(passages)
    try:
        await emit(
            "synthesize_section_fallback",
            {
                "section": title,
                "reason": reason,
                "passages_preserved": min(len(passages), 3),
            },
        )
    except Exception:  # noqa: BLE001 — telemetry cannot erase evidence
        pass
    if not markdown:
        return "", ReportSection(
            id=section_id, title=title, markdown=empty_markdown, confidence="low"
        )
    return markdown, None
