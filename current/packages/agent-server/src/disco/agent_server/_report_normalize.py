"""Passage-text normalization — the shared MD/PDF export seam.

Scraped passages arrive with two classes of junk that both export formats
inherited verbatim:
  1. Backslash-escaped markdown links quoted as literal noise, e.g.
     ``\\[proprietary\\](https://en.wikipedia.org/wiki/Proprietary\\_software)``.
  2. Table debris: one-line pseudo-tables (a paragraph full of ``' | '``
     separators with no header/separator row) and genuine pipe tables whose
     rows disagree with the header width.
``_normalized_report`` cleans both on a copy of the ReportEvent and is the
single seam shared by serialize_markdown (fmt=md) and _build_pdf_html
(fmt=pdf via serialize_pdf) — fixes land once, both formats inherit them.

Private decomposition of report_export.py (the `_synthesis_parts` pattern):
report_export re-exports what its serializers and table renderer need, so the
public seam stays ``disco.agent_server.report_export``.
"""

from __future__ import annotations

import re

from disco.core import ReportEvent

_ESCAPED_LINK_RE = re.compile(r"\\\[((?:[^\\\]]|\\.)*?)\\\]\(([^()\s]+)\)")
_MD_ESCAPE_CHAR_RE = re.compile(r"\\([\\`*_{}\[\]()<>#+\-.!|~])")
_EXTERNAL_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_ALIGN_RE = re.compile(r"^:?-{3,}:?$")


def _unescape_md(text: str) -> str:
    """Drop backslash-escapes of markdown punctuation (``\\_`` → ``_``)."""
    return _MD_ESCAPE_CHAR_RE.sub(r"\1", text)


def _rewrite_escaped_link(m: re.Match[str]) -> str:
    """One ``\\[text\\](url)`` occurrence → clean prose.

    External http(s) urls that add information keep it as ``text (url)``;
    relative/anchor urls (site chrome like ``/faq``) reduce to the anchor text.
    """
    text = _unescape_md(m.group(1)).strip()
    url = _unescape_md(m.group(2)).strip()
    if not text:
        return url if _EXTERNAL_URL_RE.match(url) else ""
    if _EXTERNAL_URL_RE.match(url) and url != text:
        return f"{text} ({url})"
    return text


def _is_pseudo_table_line(line: str) -> bool:
    """A non-table line with high pipe density — a scraped table fragment
    flattened into one paragraph of ``' | '`` separators."""
    return line.count("|") >= 3 and line.count(" | ") >= 2


def _format_pipe_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _split_table_row(line: str) -> list[str]:
    """Split a pipe-table row while preserving escaped pipes inside cells."""
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]

    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in row:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    cells.append("".join(buf).strip())
    return cells


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(_TABLE_ALIGN_RE.fullmatch(c.strip()) for c in cells)


def _normalize_table_block(lines: list[str], i: int, header: str, out: list[str]) -> int:
    """Consume one genuine pipe table starting at ``lines[i]`` (whose header
    line, already link-rewritten, is ``header``): pad/truncate the separator
    row and every body row to the header's column count. Appends the repaired
    rows to ``out`` and returns the index of the first line past the table.
    """
    width = len(_split_table_row(header))
    out.append(header)
    sep = _split_table_row(lines[i + 1])
    out.append(
        lines[i + 1]
        if len(sep) == width
        else _format_pipe_row((sep + ["---"] * width)[:width])
    )
    i += 2
    while (
        i < len(lines)
        and lines[i].strip()
        and "|" in lines[i]
        and not _is_table_separator(lines[i])
    ):
        row_line = _ESCAPED_LINK_RE.sub(_rewrite_escaped_link, lines[i])
        row = _split_table_row(row_line)
        out.append(
            row_line
            if len(row) == width
            else _format_pipe_row((row + [""] * width)[:width])
        )
        i += 1
    return i


def _normalize_passage_text(text: str) -> str:
    """Shared cleanup for passage-bearing markdown (summary + section bodies).

    - ``\\[text\\](url)`` escaped links → anchor text (plus ``(url)`` for
      informative external urls).
    - Genuine pipe tables: pad/truncate every row (and the separator row) to
      the header's column count so renderers never see a ragged table.
    - Non-table lines with high ``|`` density: strip the pipes into readable
      ``;``-separated text.
    Fenced code blocks pass through untouched.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    in_fence = False
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        line = _ESCAPED_LINK_RE.sub(_rewrite_escaped_link, line)
        if (
            i + 1 < len(lines)
            and "|" in line
            and _is_table_separator(lines[i + 1])
        ):
            i = _normalize_table_block(lines, i, line, out)
            continue
        if _is_pseudo_table_line(line):
            cells = [c for c in _split_table_row(line) if c]
            out.append("; ".join(cells))
            i += 1
            continue
        out.append(line)
        i += 1
    result = "\n".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _normalized_report(report: ReportEvent) -> ReportEvent:
    """Return the report with summary + section bodies cleaned for export.

    This is the shared serialization seam: serialize_markdown and
    _build_pdf_html both call it first, so fmt=md and fmt=pdf render the same
    cleaned text. Returns the original object untouched when nothing changed
    (keeps the byte-parity contract for clean reports trivially intact).
    """
    summary = _normalize_passage_text(report.summary or "")
    sections = [
        s.model_copy(update={"markdown": _normalize_passage_text(s.markdown)})
        for s in report.sections
    ]
    if summary == (report.summary or "") and all(
        ns.markdown == s.markdown for ns, s in zip(sections, report.sections, strict=True)
    ):
        return report
    return report.model_copy(update={"summary": summary, "sections": sections})
