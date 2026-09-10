"""`FindAndEditTool.run`'s pre-decision scan phase.

Reads every selected file, marks it grounded, and collects each non-empty
regex match as a `_Match` alongside a per-file JSON-serializable result
record. An unreadable file becomes a recorded `skipped`/`read_error`; a
zero-length match is kept as a `skipped`/`zero_length_match` record without a
`_Match` (an empty span can never ground a safe `exact_replace`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from ...anatomy import ToolContext
from ..files import mark_read, record_read

_CONTEXT_RADIUS_LINES = 20


@dataclass(frozen=True)
class _Match:
    global_index: int
    file_index: int
    path: str
    start: int
    end: int
    line_start: int
    line_end: int
    text: str
    context: str


@dataclass(frozen=True)
class _ReadFile:
    path: str
    text: str
    sha256: str
    matches: list[_Match]
    result: dict[str, Any]


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _context_window(text: str, line_start: int, line_end: int) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, line_start - _CONTEXT_RADIUS_LINES)
    end = min(len(lines), line_end + _CONTEXT_RADIUS_LINES)
    width = len(str(end))
    return "\n".join(f"{line:>{width}}\t{lines[line - 1]}" for line in range(start, end + 1))


def _match_record(match: _Match) -> dict[str, Any]:
    return {
        "index": match.file_index,
        "global_index": match.global_index,
        "start": match.start,
        "end": match.end,
        "line_start": match.line_start,
        "line_end": match.line_end,
        "matched_text": match.text,
        "status": "pending",
    }


async def scan_selected_files(
    selected: list[str],
    regex: re.Pattern[str],
    ctx: ToolContext,
) -> tuple[list[_ReadFile], list[dict[str, Any]], list[_Match]]:
    """Read every selected file and collect its regex matches.

    Returns `(read_files, file_results, global_matches)`: `file_results` is the
    ordered per-selected-path result record (already present, in order, even
    for a file that failed to read); `read_files` covers only the files that
    were actually read; `global_matches` is the flat cross-file match list the
    decision phase iterates.
    """
    assert ctx.sandbox is not None
    files: list[_ReadFile] = []
    file_results: list[dict[str, Any]] = []
    global_matches: list[_Match] = []
    for path in selected:
        result: dict[str, Any] = {"path": path, "status": "pending", "matches": []}
        file_results.append(result)
        try:
            raw = await ctx.sandbox.read_file(path)
        except Exception as exc:  # noqa: BLE001 - unreadable files are recorded skips.
            result["status"] = "skipped"
            result["reason"] = "read_error"
            result["error"] = f"{type(exc).__name__}: {exc}"
            continue
        sha = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        total_lines = max(1, len(text.splitlines()))
        mark_read(ctx.conversation_id, path)
        record_read(
            ctx.conversation_id,
            path,
            sha=sha,
            start_line=1,
            end_line=total_lines,
            full=True,
        )
        file_matches: list[_Match] = []
        for re_match in regex.finditer(text):
            matched_text = re_match.group(0)
            result_index = len(result["matches"])
            if matched_text == "":
                rec = {
                    "index": result_index,
                    "global_index": len(global_matches),
                    "start": re_match.start(),
                    "end": re_match.end(),
                    "line_start": _line_for_offset(text, re_match.start()),
                    "line_end": _line_for_offset(text, re_match.end()),
                    "matched_text": matched_text,
                    "status": "skipped",
                    "reason": "zero_length_match",
                }
                result["matches"].append(rec)
                continue
            line_start = _line_for_offset(text, re_match.start())
            line_end = _line_for_offset(text, re_match.end())
            match = _Match(
                global_index=len(global_matches),
                file_index=result_index,
                path=path,
                start=re_match.start(),
                end=re_match.end(),
                line_start=line_start,
                line_end=line_end,
                text=matched_text,
                context=_context_window(text, line_start, line_end),
            )
            file_matches.append(match)
            global_matches.append(match)
            result["matches"].append(_match_record(match))
        result["sha256"] = sha
        result["status"] = "matched" if file_matches else "unchanged"
        files.append(
            _ReadFile(
                path=path,
                text=text,
                sha256=sha,
                matches=file_matches,
                result=result,
            )
        )
    return files, file_results, global_matches
