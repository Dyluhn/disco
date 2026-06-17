"""Slide generation tool — writes Marp-rendered slide decks (HTML/PDF/PPTX) to
the workspace. The model authors the markdown deck source, and this tool
renders it via Marp CLI.

Format support:
  - html: always works (Marp CLI render or self-contained fallback).
  - pdf, pptx: requires marp + Chromium (ships in the sandbox image); returns
    a clean failure when the toolchain is absent.
  - marp --pptx output is image-based slides — editable PPTX is v2.
"""

from __future__ import annotations

import html
import re
import shlex
from textwrap import dedent

from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

# ---- args model --------------------------------------------------------------


class SlidesGenerateArgs(BaseModel):
    """Arguments for the slides_generate tool."""

    markdown: str = Field(
        description=(
            "Marp-compatible markdown source for the slide deck. "
            "Use CommonMark with '---' (three dashes on their own line) to "
            "separate slides. Front matter directives (theme, paginate, etc.) "
            "are supported."
        )
    )
    filename: str = Field(
        description=(
            "Base filename WITHOUT extension (e.g. 'my-deck'). The tool appends "
            ".html, .pdf, or .pptx based on the format argument."
        )
    )
    format: str = Field(
        default="html",
        description="Output format: 'html', 'pdf', or 'pptx'. Default: 'html'.",
    )
    theme: str | None = Field(
        default=None,
        description="Optional Marp theme CSS to include inline (e.g. custom colors, fonts).",
    )


# ---- HTML fallback (used when marp CLI is unavailable) -----------------------

_MARP_MINIMAL_THEME = dedent("""\
/* Minimal built-in theme fallback */
section {
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  font-size: 1.5em;
  padding: 2em;
  background: #fff;
  color: #222;
}
h1 { font-size: 2em; margin-bottom: 0.3em; }
h2 { font-size: 1.6em; margin-bottom: 0.3em; }
h3 { font-size: 1.3em; margin-bottom: 0.2em; }
code { background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.85em; }
pre { background: #f0f0f0; padding: 1em; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
ul, ol { text-align: left; padding-left: 1.5em; }
li { margin-bottom: 0.3em; }
blockquote {
  border-left: 4px solid #ccc; margin: 0.5em 0; padding: 0.3em 1em;
  color: #555; font-style: italic;
}
table { border-collapse: collapse; margin: 0.5em auto; }
th, td { border: 1px solid #ddd; padding: 0.4em 0.8em; text-align: left; }
th { background: #f5f5f5; }
img { max-width: 100%; height: auto; }
a { color: #0366d6; }
""")


# Simple inline markup patterns applied BEFORE the html.escape pass.
# Order: protect code spans/spans first (they may contain other tokens),
# then block-level, then inline.
_FENCE_RE = re.compile(r"```(.*?)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline_markdown_to_html(text: str) -> str:
    """Convert inline markdown (bold, italic, code, links, images) to HTML.
    Safe to apply on already-HTML-escaped text — the regexes match literal
    markdown tokens that survive escaping."""
    text = _IMAGE_RE.sub(r'<img src="\2" alt="\1">', text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _INLINE_CODE_RE.sub(r"<code>\1</code>", text)
    return text


def _basic_md_to_html(md: str) -> str:
    """Minimal CommonMark-to-HTML converter for the fallback renderer.
    Handles headings, code fences, lists, blockquotes, paragraphs, and
    inline formatting. Deliberately simple — full Marp rendering is preferred."""
    lines = md.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block (```)
        if line.startswith("```"):
            lang = line[3:].strip()
            block_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block_lines.append(lines[i])
                i += 1
            code_html = html.escape("\n".join(block_lines))
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{code_html}</code></pre>")
            i += 1  # skip closing ```
            continue

        # Heading
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = min(len(heading_match.group(1)), 6)
            text = _inline_markdown_to_html(html.escape(heading_match.group(2)))
            out.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # Blockquote
        if line.startswith("> "):
            bq_lines: list[str] = []
            while i < len(lines) and lines[i].startswith("> "):
                bq_lines.append(lines[i][2:])
                i += 1
            bq_text = "<br>".join(
                _inline_markdown_to_html(html.escape(ln)) for ln in bq_lines
            )
            out.append(f"<blockquote>{bq_text}</blockquote>")
            continue

        # Unordered list
        if re.match(r"^[-*+]\s+", line):
            out.append("<ul>")
            while i < len(lines) and re.match(r"^[-*+]\s+", lines[i]):
                text = _inline_markdown_to_html(
                    html.escape(re.sub(r"^[-*+]\s+", "", lines[i]))
                )
                out.append(f"<li>{text}</li>")
                i += 1
            out.append("</ul>")
            continue

        # Ordered list
        if re.match(r"^\d+\.\s+", line):
            out.append("<ol>")
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i]):
                text = _inline_markdown_to_html(
                    html.escape(re.sub(r"^\d+\.\s+", "", lines[i]))
                )
                out.append(f"<li>{text}</li>")
                i += 1
            out.append("</ol>")
            continue

        # Horizontal rule (--- or *** on its own)
        if re.match(r"^[-*]{3,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # Empty line -> paragraph break
        if line.strip() == "":
            i += 1
            continue

        # Paragraph (collect consecutive non-empty, non-special lines)
        para_lines: list[str] = []
        while i < len(lines) and lines[i].strip() and not any(
            lines[i].startswith(p)
            for p in ("#", "```", "> ", "- ", "* ", "+ ")
        ) and not re.match(r"^\d+\.\s+", lines[i]) and not re.match(
            r"^[-*]{3,}\s*$", lines[i]
        ):
            para_lines.append(lines[i])
            i += 1
        if para_lines:
            text = "<br>".join(
                _inline_markdown_to_html(html.escape(ln)) for ln in para_lines
            )
            out.append(f"<p>{text}</p>")

    return "\n    ".join(out)


def _split_slides(markdown: str) -> list[str]:
    """Split markdown into slides on '---' separators. Handles leading/trailing
    separators and CRLF line endings."""
    # Normalize line endings
    md = markdown.replace("\r\n", "\n")
    # Split on \n---\n (slide separator on its own line)
    parts = re.split(r"\n---\n", md)
    # Filter empty slides
    return [p.strip() for p in parts if p.strip()]


def _fallback_html(markdown: str, theme: str | None = None) -> str:
    """Minimal self-contained HTML renderer. Splits on '---', wraps each
    slide in a <section> tag. Works without Marp — HTML always ships."""
    slides_raw = _split_slides(markdown)
    theme_css = theme or _MARP_MINIMAL_THEME

    sections = ""
    for i, slide_md in enumerate(slides_raw):
        html_body = _basic_md_to_html(slide_md)
        sections += (
            f'  <section class="slide" id="slide-{i + 1}">\n'
            f"    {html_body}\n"
            f"  </section>\n"
        )

    return dedent(
        f"""\
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Slide Deck</title>
      <style>
    {theme_css}
        body {{ margin: 0; padding: 0; }}
        .slide {{
          min-height: 100vh;
          box-sizing: border-box;
          display: flex;
          flex-direction: column;
          justify-content: center;
          page-break-after: always;
        }}
      </style>
    </head>
    <body>
    {sections}
    </body>
    </html>"""
    )


# ---- marp CLI — run INSIDE the sandbox (not the agent-server host) -----------
# The deck markdown is agent-generated (injection-tainted). Marp drives a full
# Chromium to render PDF/PPTX; running that on the host would let a crafted deck
# (file:// refs, local URLs) exfiltrate host files during render. So marp runs in
# the jailed sandbox via exec_shell — the binary + Chromium ship in the image
# (deploy/sandbox/Dockerfile, RP-10 layer). The output lands directly in the
# workspace, so there is no host temp file and no read-back.


async def _marp_available(ctx: ToolContext) -> bool:
    """True if the marp CLI is present INSIDE the sandbox."""
    assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
    try:
        res = await ctx.sandbox.exec_shell("command -v marp", timeout_s=10)
    except Exception:
        return False
    return res.exit_code == 0


async def _marp_render_in_sandbox(
    ctx: ToolContext,
    src_name: str,
    out_name: str,
    fmt: str,
    *,
    timeout_s: int = 180,
) -> tuple[bool, str]:
    """Run `marp <src> -o <out>` INSIDE the sandbox (workdir = workspace). Both
    paths are workspace-relative names. Returns (ok, error_message)."""
    assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
    pptx = "--pptx " if fmt == "pptx" else ""
    cmd = f"marp {pptx}{shlex.quote(src_name)} -o {shlex.quote(out_name)}"
    res = await ctx.sandbox.exec_shell(cmd, timeout_s=timeout_s)
    if res.timed_out:
        return False, f"marp render timed out after {timeout_s}s"
    if res.exit_code != 0:
        return False, (res.stderr or "").strip() or f"marp exited with code {res.exit_code}"
    return True, ""


# ---- tool implementation -----------------------------------------------------


class SlidesTool:
    definition = ToolDef(
        name="slides_generate",
        description=(
            "Write a slide deck (HTML/PDF/PPTX) to the workspace. Provide markdown "
            "source with '---' (three dashes on their own line) to separate slides. "
            "Front matter directives (theme, paginate, etc.) are supported. "
            "HTML always works; PDF and PPTX require the Marp+Chromium toolchain "
            "in the sandbox image. PPTX output is image-based slides (editable "
            "PPTX is v2)."
        ),
        args_model=SlidesGenerateArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=None,  # file write — sandbox-jailed, no network needed
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: SlidesGenerateArgs, ctx: ToolContext) -> ToolOutcome:
        # This tool declares runs_in="sandbox": all I/O MUST go through the
        # sandbox instance, which jails the path and lands the file in the
        # conversation's workspace.
        assert ctx.sandbox is not None  # sandbox tools always receive an instance

        fmt = args.format.lower()
        if fmt not in ("html", "pdf", "pptx"):
            return ToolOutcome(
                success=False,
                content=f"Unsupported format: {fmt!r}. Use 'html', 'pdf', or 'pptx'.",
                error=f"Unsupported format: {fmt!r}.",
            )

        out_filename = f"{args.filename}.{fmt}"

        # Prepare markdown: inject theme as frontmatter if provided
        markdown = args.markdown
        if args.theme:
            # Prepend theme as a <style> directive if not already present
            theme_block = f"<!-- theme: custom -->\n<style>\n{args.theme}\n</style>\n\n"
            markdown = theme_block + markdown

        have_marp = await _marp_available(ctx)

        # ---- HTML path ----
        if fmt == "html":
            if have_marp:
                return await self._render_with_marp(
                    markdown, out_filename, fmt, ctx, args
                )
            else:
                return await self._render_html_fallback(
                    markdown, out_filename, ctx, args
                )

        # ---- PDF / PPTX paths (require marp + Chromium in the sandbox image) ----
        if not have_marp:
            return ToolOutcome(
                success=False,
                content=(
                    f"{fmt.upper()} export needs the Marp CLI + Chromium in the sandbox "
                    f"image, which isn't present in this sandbox. HTML export works now "
                    f"as a fallback — re-run with format='html' for a rendered deck."
                ),
                error=f"marp CLI not found in the sandbox — {fmt.upper()} export unavailable.",
            )

        return await self._render_with_marp(markdown, out_filename, fmt, ctx, args)

    async def _render_with_marp(
        self,
        markdown: str,
        out_filename: str,
        fmt: str,
        ctx: ToolContext,
        args: SlidesGenerateArgs,
    ) -> ToolOutcome:
        """Render via marp INSIDE the sandbox: write the markdown source into the
        jailed workspace, run marp in-box (output lands directly in the
        workspace), then clean up the source. No host temp files, no read-back."""
        assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
        # A hidden workspace-relative source file marp reads (workdir = workspace).
        src_name = f".{args.filename}.marp-src.md"
        await ctx.sandbox.write_file(src_name, markdown.encode("utf-8"))
        try:
            ok, err = await _marp_render_in_sandbox(ctx, src_name, out_filename, fmt)
        finally:
            # Best-effort cleanup of the transient source inside the sandbox.
            try:
                await ctx.sandbox.exec_shell(f"rm -f {shlex.quote(src_name)}", timeout_s=10)
            except Exception:
                pass

        if not ok:
            return ToolOutcome(
                success=False,
                content=f"Marp render failed: {err}",
                error=f"Marp render failed: {err}",
            )
        # The output already lives in the workspace at out_filename (marp wrote it
        # there) — jailed by construction; nothing to read back.

        note = ""
        if fmt == "pptx":
            note = (
                "\nNOTE: PPTX output is image-based slides (each slide is a static image). "
                "Editable PPTX is v2."
            )

        slide_count = len(_split_slides(args.markdown))
        return ToolOutcome(
            success=True,
            content=(
                f"Slide deck '{args.filename}' written to {out_filename}\n"
                f"Format: {fmt.upper()}\n"
                f"Slides: {slide_count}{note}"
            ),
            artifacts=[out_filename],
            structured={
                "filename": out_filename,
                "base_name": args.filename,
                "format": fmt,
                "slide_count": slide_count,
                "renderer": "marp",
            },
        )

    async def _render_html_fallback(
        self,
        markdown: str,
        out_filename: str,
        ctx: ToolContext,
        args: SlidesGenerateArgs,
    ) -> ToolOutcome:
        """Fallback HTML renderer: produces a self-contained HTML deck without
        Marp. Used when marp CLI is not installed."""
        assert ctx.sandbox is not None  # slides tool declares runs_in="sandbox"
        html_content = _fallback_html(markdown, args.theme)
        html_bytes = html_content.encode("utf-8")

        # Write THROUGH the sandbox.
        await ctx.sandbox.write_file(out_filename, html_bytes)

        slide_count = len(_split_slides(args.markdown))
        return ToolOutcome(
            success=True,
            content=(
                f"Slide deck '{args.filename}' written to {out_filename}\n"
                f"Format: HTML (fallback renderer — marp CLI not found)\n"
                f"Slides: {slide_count}"
            ),
            artifacts=[out_filename],
            structured={
                "filename": out_filename,
                "base_name": args.filename,
                "format": "html",
                "slide_count": slide_count,
                "renderer": "fallback",
            },
        )
