"""Report export serializers — MD / PDF / DOCX (RP-07).

Exports a finished deep-research ReportEvent to three formats:
  - Markdown (ported verbatim from deepResearch.ts:88-127 — byte-identical)
  - PDF via WeasyPrint (md → HTML → PDF)
  - DOCX via pandoc subprocess (md → docx)

pandoc and WeasyPrint are imported/invoked lazily so the module imports even
when the binaries/libs are absent. These run in the AGENT-SERVER process (this is
an export endpoint over a stored, server-generated ReportEvent — there is no
sandbox in this path), so PDF needs `weasyprint` importable and DOCX needs
`pandoc` on PATH wherever the agent-server runs. `export_capabilities()` reports
which formats are actually usable so the UI never offers a button that 500s.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from disco.core import ReportEvent

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
    # Faithful markdown → HTML via the pure-Python `markdown` library (no system
    # libs): headings, **bold**, _italic_, [links], lists, and crucially the
    # RP-02/RP-03 TABLES render as real HTML, not literal markdown text. (An
    # earlier version was a naive line parser that dumped section bodies as
    # escaped <p> — degraded PDFs; this replaces it.) `markdown` escapes/structures
    # the input; the report markdown is server-generated, not arbitrary HTML.
    import html

    import markdown as _md

    # `markdown`'s typeshed stub is incomplete: it advertises
    # `Literal["xhtml", "html"]`, but the runtime also accepts `"html5"` (a
    # documented alias of `"html"`). Cast to the stub's union rather than
    # loosening the stub or adding a type-ignore — the runtime contract is
    # the same.
    body_html = _md.markdown(
        md,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format=cast("Literal['xhtml', 'html']", "html5"),
    )

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
  h2 {{ font-size: 1.25em; margin-top: 1.4em; border-bottom: 1px solid #ccc; }}
  p {{ margin: 0.5em 0; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }}
  pre {{ white-space: pre-wrap; font-family: ui-monospace, Menlo, monospace; font-size: 0.85em;
         background: #f6f6f6; padding: 0.8em; border-radius: 4px; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 0.9em; }}
  ul, ol {{ margin: 0.5em 0; padding-left: 1.4em; }}
  a {{ color: #0b5cad; }}
  table {{ border-collapse: collapse; margin: 0.8em 0; width: 100%; font-size: 0.95em; }}
  th, td {{ border: 1px solid #ccc; padding: 0.35em 0.6em; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


# ---- PDF serializer (md → HTML → WeasyPrint) ------------------------------


def _lazy_import_weasyprint():
    """Import WeasyPrint lazily. Raises ImportError if not installed."""
    import weasyprint

    return weasyprint


def serialize_pdf(report: ReportEvent) -> bytes:
    """Serialize a ReportEvent to PDF via WeasyPrint.

    WeasyPrint renders HTML+CSS to PDF. We convert the markdown to a styled HTML
    page and render it. Runs in the AGENT-SERVER process, so weasyprint (+ its
    pango/cairo libs) must be importable there.

    Raises ImportError if weasyprint is not installed (test skips
    cleanly). Raises RuntimeError on rendering failure.
    """
    weasyprint = _lazy_import_weasyprint()
    md = serialize_markdown(report)
    html = _markdown_to_html(md, title=report.query)
    try:
        doc = weasyprint.HTML(string=html)
        # WeasyPrint's `write_pdf(target=None)` returns `bytes` (the per its
        # docstring). The typeshed stub types it as `bytes | None` because
        # `target=<file>` would return `None`; we never pass a target.
        pdf = doc.write_pdf()
        assert pdf is not None  # target not provided → bytes
        return pdf
    except Exception as exc:
        raise RuntimeError(f"WeasyPrint PDF generation failed: {exc}") from exc


# ---- DOCX serializer (md → pandoc, INSIDE a sandbox) ----------------------


async def serialize_docx(report: ReportEvent, sandbox: Any) -> bytes:
    """Serialize a ReportEvent to DOCX via `pandoc` INSIDE a sandbox container.

    pandoc ships in the sandbox image (NOT the host), so the runtime creates a
    transient render sandbox and passes it here: the report markdown is written
    into the jailed workspace, pandoc renders it to .docx in-box, the bytes are
    read back, and the runtime destroys the throwaway sandbox. No host install,
    jailed like marp. Raises RuntimeError on failure.
    """
    md = serialize_markdown(report)
    await sandbox.write_file("_export.md", md.encode("utf-8"))
    res = await sandbox.exec_shell(
        "pandoc _export.md -f markdown -t docx -o _export.docx", timeout_s=60
    )
    if getattr(res, "timed_out", False):
        raise RuntimeError("pandoc timed out after 60s")
    if res.exit_code != 0:
        detail = (res.stderr or "").strip() or f"exit code {res.exit_code}"
        raise RuntimeError(f"pandoc failed in the sandbox: {detail}")
    return await sandbox.read_file("_export.docx")


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
    """Export a ReportEvent to MD or PDF (both rendered in-process).

    DOCX is NOT handled here — it renders inside a transient sandbox (pandoc ships
    in the sandbox image, not the host), so the runtime calls `serialize_docx`
    directly with a sandbox instance. Returns (payload, media_type, suffix).
    Raises ValueError for unknown formats (caller converts to 400)."""
    if fmt not in _EXPORT_FORMATS:
        raise ValueError(f"Unknown export format: {fmt!r}. Valid: md, pdf, docx")

    if fmt == "md":
        payload = serialize_markdown(report).encode("utf-8")
    elif fmt == "pdf":
        payload = serialize_pdf(report)
    elif fmt == "docx":
        raise ValueError("docx is rendered in a sandbox — call serialize_docx(report, sandbox)")
    else:
        raise ValueError(f"Unknown export format: {fmt!r}")  # pragma: no cover

    media_type = MEDIA_TYPES[fmt]
    ext = EXTENSIONS[fmt]
    return payload, media_type, ext


def pdf_available() -> bool:
    """True if PDF export can run in-process (weasyprint + its pango/cairo libs)."""
    try:
        _lazy_import_weasyprint()
        return True
    except Exception:
        return False
