"""Report export serializers — MD / PDF / DOCX (RP-07).

Exports a finished deep-research ReportEvent to three formats:
  - Markdown (ported verbatim from deepResearch.ts:88-127 — byte-identical)
  - PDF via WeasyPrint (md → HTML → PDF)
  - DOCX via pandoc subprocess (md → docx)

pandoc and WeasyPrint are imported/invoked lazily so the module imports
even when the system binaries/libs are absent. The markdown path works
end-to-end now; PDF/DOCX live generation needs the rebuilt sandbox image.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from perpleximanus.core import ReportEvent

logger = logging.getLogger(__name__)

# ---- Markdown serializer (ported verbatim from deepResearch.ts:88-127) ----


def serialize_markdown(report: ReportEvent) -> str:
    """Serialize a ReportEvent to markdown.

    Ported VERBATIM from `serializeReportToMarkdown` in
    frontend/src/api/deepResearch.ts:88-127. Same heading levels, citation
    rendering, section order, and bounded_by honesty footer. The byte-parity
    test asserts this against a captured sample from the client-side output.
    """
    lines: list[str] = []
    lines.append(f"# Deep Research: {report.query}")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(report.summary or "*(no summary)*")
    lines.append("")
    for s in report.sections:
        lines.append(f"## {s.title}")
        lines.append("")
        if s.disputed_notes and len(s.disputed_notes) > 0:
            lines.append(f"_Conflicts noted: {'; '.join(s.disputed_notes)}_")
            lines.append("")
        lines.append(s.markdown)
        lines.append("")
    if report.bounded_by:
        lines.append("---")
        lines.append("")
        lines.append(
            f"_This run was bounded by **{report.bounded_by}**. Some planned "
            f"sub-questions were not covered. Consider running the EXHAUSTIVE "
            f"tier or assigning a faster driver model for deeper coverage._"
        )
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"Passages cited ({len(report.passages)}):")
    lines.append("")
    for p in report.passages:
        pid = str(p.get("id", "?"))
        title = str(p.get("source_title", ""))
        url = str(p.get("source_url", ""))
        lines.append(f"- [{pid}] {title} — {url}")
    return "\n".join(lines)


# ---- Markdown → HTML helper (shared by PDF) -------------------------------


def _markdown_to_html(md: str, title: str = "Deep Research Report") -> str:
    """Convert a markdown document to a self-contained HTML page.

    WeasyPrint needs valid HTML5. We produce a minimal, print-safe
    HTML document from the markdown — no external assets, CSS embedded,
    a clean print layout.
    """
    # LIMITATION (must fix before the PDF button goes live — tracked with the
    # sandbox image rebuild): this is a NAIVE line-by-line converter. It styles
    # ATX headings and `---` rules, but section BODIES (`s.markdown`) are merely
    # HTML-escaped and wrapped in <p> — so **bold**, _italic_, [links](url),
    # lists, and especially the RP-02/RP-03 tables/charts render as LITERAL
    # markdown text in the PDF, not formatted. (DOCX via pandoc renders them
    # correctly; only this PDF path is degraded.) Before enabling the PDF button
    # (DeepResearchSurface EXPORT_BINARY_FORMATS_READY), replace this with a real
    # markdown→HTML pass — the pure-Python `markdown` library, no system libs.
    # The button is DISABLED in the UI today, so no user reaches this path yet.
    import html

    body_lines: list[str] = []
    for line in md.split("\n"):
        escaped_line = html.escape(line)
        if escaped_line.startswith("# ") and not escaped_line.startswith("## "):
            body_lines.append(f'<h1>{escaped_line[2:]}</h1>')
        elif escaped_line.startswith("## "):
            body_lines.append(f'<h2>{escaped_line[3:]}</h2>')
        elif escaped_line.startswith("---"):
            body_lines.append('<hr class="section-divider">')
        elif escaped_line == "":
            body_lines.append("")
        else:
            # Preserve inline markdown hints: **bold**, _italic_, [links]
            body_lines.append(f"<p>{escaped_line}</p>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  @page {{ margin: 1in; size: A4; }}
  body {{
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #1a1a1a;
  }}
  h1 {{ font-size: 1.6em; margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 0.2em; }}
  h2 {{ font-size: 1.25em; margin-top: 1.4em; border-bottom: 1px solid #ccc; padding-bottom: 0.15em; }}
  p {{ margin: 0.5em 0; }}
  hr.section-divider {{ border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }}
  pre {{ white-space: pre-wrap; font-family: inherit; }}
</style>
</head>
<body>
{"".join(body_lines)}
</body>
</html>"""


# ---- PDF serializer (md → HTML → WeasyPrint) ------------------------------


def _lazy_import_weasyprint():
    """Import WeasyPrint lazily. Raises ImportError if not installed."""
    import weasyprint

    return weasyprint


def serialize_pdf(report: ReportEvent) -> bytes:
    """Serialize a ReportEvent to PDF via WeasyPrint.

    WeasyPrint renders HTML+CSS to PDF. We convert the markdown to a
    styled HTML page and render it. The sandbox image must have
    WeasyPrint + libpango installed.

    Raises ImportError if weasyprint is not installed (test skips
    cleanly). Raises RuntimeError on rendering failure.
    """
    weasyprint = _lazy_import_weasyprint()
    md = serialize_markdown(report)
    html = _markdown_to_html(md, title=report.query)
    try:
        doc = weasyprint.HTML(string=html)
        return doc.write_pdf()
    except Exception as exc:
        raise RuntimeError(f"WeasyPrint PDF generation failed: {exc}") from exc


# ---- DOCX serializer (md → pandoc subprocess) -----------------------------


def _find_pandoc() -> str | None:
    """Return the pandoc binary path, or None if not found."""
    import shutil

    return shutil.which("pandoc")


def serialize_docx(report: ReportEvent) -> bytes:
    """Serialize a ReportEvent to DOCX via pandoc subprocess.

    Converts markdown → docx using the pandoc system binary (NOT pypandoc).
    The sandbox image must have pandoc installed.

    Raises FileNotFoundError if pandoc is not on PATH (test skips cleanly).
    Raises RuntimeError on conversion failure.
    """
    pandoc = _find_pandoc()
    if pandoc is None:
        raise FileNotFoundError(
            "pandoc not found on PATH. "
            "PDF/DOCX export rides the sandbox image rebuild (BP-08/BP-04 VM-201)."
        )

    md = serialize_markdown(report)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tmp_md:
        tmp_md.write(md)
        md_path = tmp_md.name

    try:
        result = subprocess.run(
            [pandoc, md_path, "-f", "markdown", "-t", "docx", "-o", "-"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"pandoc failed (exit code {result.returncode}): {stderr}"
            )
        return result.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError("pandoc timed out after 60s")
    finally:
        Path(md_path).unlink(missing_ok=True)


# ---- Format dispatch -------------------------------------------------------


_EXPORT_FORMATS = frozenset({"md", "pdf", "docx"})

MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown;charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

EXTENSIONS: dict[str, str] = {
    "md": ".md",
    "pdf": ".pdf",
    "docx": ".docx",
}


def export_report(report: ReportEvent, fmt: str) -> tuple[bytes, str, str]:
    """Export a ReportEvent to the requested format.

    Returns (payload_bytes, media_type, filename_suffix).
    Raises ValueError for unknown formats (caller converts to 400).
    """
    if fmt not in _EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {fmt!r}. Valid: md, pdf, docx")

    if fmt == "md":
        payload = serialize_markdown(report).encode("utf-8")
    elif fmt == "pdf":
        payload = serialize_pdf(report)
    elif fmt == "docx":
        payload = serialize_docx(report)
    else:
        raise ValueError(f"Unknown export format: {fmt!r}")  # pragma: no cover

    media_type = MEDIA_TYPES[fmt]
    ext = EXTENSIONS[fmt]
    return payload, media_type, ext
