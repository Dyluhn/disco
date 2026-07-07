"""Slide generation tool — writes rendered slide decks (HTML/PDF/PPTX) to
the workspace.

Primary path (C2): when a ``goal`` is supplied, the staged C2 pipeline
(outline → fill → assets → lower_deck → C3 render) generates a structured
AuthoredDeck and renders it to native editable .pptx / brand HTML.  On parse
failure after one retry the Marp fallback is taken automatically — no crash.

Marp fallback: when only ``markdown`` is supplied, or when C2 fails, the
tool falls back to Marp CLI (or the pure-HTML fallback when Marp is absent).

Format support:
  - html: always works (C3 brand HTML, or Marp CLI, or self-contained fallback).
  - pdf: requires marp + Chromium or LibreOffice in the sandbox image.
  - pptx: C3 native editable PPTX (real text boxes) via python-pptx; or
          Marp --pptx (image-based) when the C2 path is not used.
"""

from __future__ import annotations

import html
import re
import shlex
from textwrap import dedent
from typing import Literal

from disco.core.contract.export_render import (
    EXPORT_RENDER_KEY,
    check_export_render,
)
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from .image_gen import ImageGenNotConfigured, select_image_backend

# ---- args model --------------------------------------------------------------


class SlidesGenerateArgs(BaseModel):
    """Arguments for the slides_generate tool."""

    goal: str | None = Field(
        default=None,
        description=(
            "REQUIRED for deck generation (unless you supply ``markdown``). The deck's "
            "content AND intent, in natural language — e.g. 'A 7-slide technical brief "
            "on post-training quantization for LLMs: executive summary, the methods "
            "landscape, weight-only vs weight+activation, bit-width as the dominant "
            "degradation driver, model-scale effects, a cross-source comparison, and "
            "limitations'. The C2 pipeline builds a structured, themed, image-bearing "
            "deck from this. This tool CANNOT see the conversation or any research "
            "report — the content must be in ``goal`` (``theme``, ``slide_count``, and "
            "``format`` carry no content). Takes priority over ``markdown``."
        ),
    )
    markdown: str = Field(
        default="",
        description=(
            "Marp-compatible markdown source for the slide deck (backward-compat "
            "path). Use CommonMark with '---' (three dashes on their own line) to "
            "separate slides. Ignored when ``goal`` is supplied."
        ),
    )
    filename: str = Field(
        description=(
            "Base filename WITHOUT extension (e.g. 'my-deck'). The tool appends "
            ".html, .pdf, or .pptx based on the format argument."
        )
    )
    format: str = Field(
        default="pptx",
        description=(
            "Output format: 'pptx' (default — a presentable, editable deck), 'pdf', or "
            "'html'. Prefer 'pptx' for a deliverable the user opens/presents; 'html' is a "
            "self-contained web deck."
        ),
    )
    theme: str | None = Field(
        default=None,
        description="Optional Marp theme CSS to include inline (e.g. custom colors, fonts).",
    )
    slide_count: int = Field(
        default=5,
        ge=2,
        le=30,
        description="Approximate number of slides (used by the C2 pipeline; ignored for markdown path).",
    )
    mode: Literal["deck", "markdown"] = Field(
        default="deck",
        description=(
            "'deck' uses the C2 structured pipeline (default when goal supplied). "
            "'markdown' forces the Marp/fallback path regardless of goal."
        ),
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
    separators and CRLF line endings. A leading YAML/Marp frontmatter block is
    stripped first so it is never counted or rendered as a slide (Codex P10-7: it
    otherwise inflated the declared slide count → false truncation at the gate)."""
    # Normalize line endings
    md = markdown.replace("\r\n", "\n")
    # Strip a leading Marp/YAML frontmatter block: `---` on the VERY first line (no
    # leading separator) whose body is NOT a markdown heading (so a real leading
    # `---\n# Slide\n---` separator+content is left intact — that guard is what the
    # empty-filtered test pins).
    md = re.sub(r"\A---\n(?!#).*?\n---[ \t]*(?:\n|\Z)", "", md, count=1, flags=re.DOTALL)
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
            "Write a slide deck (HTML/PDF/PPTX) to the workspace.\n\n"
            "Primary path (recommended): supply ``goal`` (e.g. 'A 6-slide investor "
            "pitch for an EV battery startup') and the C2 pipeline generates a "
            "structured, brand-themed deck automatically (native editable PPTX + "
            "brand HTML).  On failure, automatically falls back to Marp.\n\n"
            "Markdown path (backward-compat): supply ``markdown`` with '---' slide "
            "separators; rendered via Marp CLI (image-based PPTX) or the HTML "
            "fallback.\n\n"
            "HTML always works.  PDF and native PPTX require the sandbox toolchain."
        ),
        args_model=SlidesGenerateArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=None,
        runs_in="sandbox",
        read_only=False,
        # The C2 path is two LLM stages (outline + fill) + one image generation PER
        # SLIDE (~10-30s each on a remote backend) + a render. On a real image-rich
        # deck that legitimately runs into minutes; the generic 300s executor cap was
        # killing it three times over → silent plain-Marp fallback. 15 min headroom.
        timeout_s=900,
    )

    def execution_scope(self, args: SlidesGenerateArgs) -> str:
        if args.goal and args.mode != "markdown":
            return "in_process"
        return "sandbox"

    async def run(self, args: SlidesGenerateArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None  # sandbox tools always receive an instance

        fmt = args.format.lower()
        if fmt not in ("html", "pdf", "pptx"):
            return ToolOutcome(
                success=False,
                content=f"Unsupported format: {fmt!r}. Use 'html', 'pdf', or 'pptx'.",
                error=f"Unsupported format: {fmt!r}.",
            )

        # NO-CONTENT GUARD (gauntlet root-cause 2026-07-07): the tool has TWO content
        # sources — `goal` (→ C2 structured pipeline) and `markdown` (→ Marp path) —
        # and it cannot see the conversation/report. A capable driver sometimes calls
        # slides_generate with only theme/slide_count/format (deck INTENT) but omits
        # `goal`; the old `use_c2 = bool(args.goal)` gate then silently fell through to
        # the Marp path with EMPTY content → a 1-slide, zero-text deck reported as
        # success (the media-only export-render heuristic passed it). Fail LOUDLY and
        # actionably instead so the driver re-calls with `goal` populated (→ real deck),
        # rather than shipping a blank deliverable. Covers mode="markdown" with empty
        # markdown too — there is genuinely nothing to render either way.
        if not (args.goal and args.goal.strip()) and not args.markdown.strip():
            return ToolOutcome(
                success=False,
                content=(
                    "slides_generate has no deck content to render. Provide `goal` — a "
                    "natural-language description AND the source content for the deck "
                    "(the C2 pipeline builds a structured, themed, image-bearing deck "
                    "from it) — OR `markdown` (Marp source with '---' slide separators). "
                    "You supplied neither, so there is nothing to turn into slides. This "
                    "tool CANNOT see the conversation or the research report: you must "
                    "pass the report's key content/instructions in `goal` (theme, "
                    "slide_count, and format do not carry any content on their own)."
                ),
                error="slides_generate called with neither goal nor markdown content",
            )

        # ---- C2 deck pipeline (primary path when goal is supplied) ----
        use_c2 = bool(args.goal) and args.mode != "markdown"
        if use_c2:
            outcome = await self._run_c2_pipeline(args, ctx, fmt)
        else:
            # ---- Marp / markdown path (fallback or explicit mode="markdown") ----
            outcome = await self._run_marp_path(args, ctx, fmt)

        # ROOT-4 (slides spiral): completion recognition. A generated deck is a
        # finished BINARY deliverable — once it is written + delivered the agent must
        # finish, not "verify"/rewrite it. Append the REAL on-disk path(s) (so it does
        # not hunt for /workspace) plus an explicit done/delivered/call-finish signal.
        if outcome.success and outcome.artifacts:
            outcome = outcome.model_copy(
                update={"content": outcome.content + self._delivery_note(ctx, outcome.artifacts)}
            )
            # [P10] Stamp export render-correctness facts by reading the ACTUAL
            # persisted artifact back — path-agnostic (covers C1/Marp/fallback) and
            # honest (validates the real file, not in-memory bytes). The finish gate
            # reads these to refuse a blank/truncated/corrupt deck.
            outcome = await self._stamp_export_render(outcome, ctx)
        return outcome

    async def _stamp_export_render(
        self, outcome: ToolOutcome, ctx: ToolContext
    ) -> ToolOutcome:
        """Read the produced deck file back from the sandbox and add ExportRenderFacts
        to the tool result's structured payload. Best-effort: on any read/parse
        failure the payload is left unstamped (the gate then falls through — absence
        is honest, never a fabricated verdict)."""
        s = outcome.structured or {}
        filename = s.get("filename")
        fmt = str(s.get("format") or "")
        declared = s.get("slide_count") if isinstance(s.get("slide_count"), int) else None
        # The C1/native renderers set slide_count = len(deck.slides) (EXACT); the
        # markdown/Marp paths derive it from a separator split (HEURISTIC, can be
        # off by one) — so truncation is strict for the former, tolerant for the latter.
        declared_exact = str(s.get("renderer") or "") not in ("marp", "fallback")
        if not isinstance(filename, str) or not filename or ctx.sandbox is None:
            return outcome
        try:
            data = await ctx.sandbox.read_file(filename)
        except Exception:
            return outcome
        if isinstance(data, str):
            data = data.encode("utf-8")
        source_text: str | None = None
        # A deck PDF is LibreOffice-converted from a FRESH sibling .pptx written this
        # same run (renderer=="libreoffice"); gate on that so a stale leftover .pptx
        # from an earlier run can't judge the current PDF. Use the pptx's own
        # chrome/placeholder/media-aware verdict, not its raw text: "" forces the PDF
        # content check to refuse a blank deck, while None lets the byte-floor pass a
        # real deck (incl. a media-only visual deck whose text is empty/placeholder).
        if fmt == "pdf" and str(s.get("renderer") or "") == "libreoffice":
            base_name = s.get("base_name")
            if isinstance(base_name, str) and base_name:
                try:
                    pptx_data = await ctx.sandbox.read_file(f"{base_name}.pptx")
                    if isinstance(pptx_data, str):
                        pptx_data = pptx_data.encode("utf-8")
                    if pptx_data.startswith(b"PK"):
                        pf = check_export_render("pptx", pptx_data)
                        source_text = None if pf.non_blank else ""
                except Exception:
                    source_text = None
        facts = check_export_render(
            fmt,
            data,
            text=source_text,
            declared_units=declared,
            declared_exact=declared_exact,
        )
        return outcome.model_copy(
            update={"structured": {**s, EXPORT_RENDER_KEY: facts.model_dump(mode="json")}}
        )

    @staticmethod
    def _delivery_note(ctx: ToolContext, artifacts: list[str]) -> str:
        """A self-sufficient 'done + delivered + here are the paths' footer so the
        model finishes instead of verify-looping. Uses the sandbox's real workspace
        root for absolute paths on the process backend (so a shell ``ls`` is never
        needed); container backends expose None → list the workspace-relative names."""
        ws: str | None = None
        try:
            ws = ctx.sandbox.workspace_path if ctx.sandbox is not None else None
        except Exception:  # noqa: BLE001 — path resolution must never break the result
            ws = None
        if ws:
            import posixpath

            paths = "\n".join(f"  - {posixpath.join(ws, a)}" for a in artifacts)
        else:
            paths = "\n".join(f"  - {a}" for a in artifacts)
        return (
            "\n\nDeck generated AND delivered. Files on disk:\n"
            f"{paths}\n"
            "This is a finished binary deliverable (NOT a web app) — no further "
            "verification is needed. Do NOT re-open, re-read, re-verify, or file_write "
            "into these files; they are already produced and delivered to the user. "
            "Call finish to complete the task."
        )

    async def _run_c2_pipeline(
        self, args: SlidesGenerateArgs, ctx: ToolContext, fmt: str
    ) -> ToolOutcome:
        """Run the C2 staged generation pipeline.  Falls back to Marp on failure."""
        from disco.tools.builtin._slides_pipeline import generate_deck

        assert args.goal is not None  # caller-checked
        # W-50: image generation is OPTIONAL for a deck. When no real image backend is
        # configured, select_image_backend() raises — DEGRADE to image-less slides
        # (text-only) rather than crashing the whole deck generation. The pipeline
        # treats backend=None as "omit images".
        try:
            backend = select_image_backend()
        except ImageGenNotConfigured:
            backend = None

        c1_deck, fallback_md, err, authored_sidecar = await generate_deck(
            args.goal,
            args.filename,
            ctx,
            backend,
            slide_count=args.slide_count,
        )

        if c1_deck is not None:
            # C2 succeeded — render via C3. authored_sidecar is the fresh editable
            # source (or None if the sidecar write failed) → gates the editor.
            return await self._render_c1_deck(
                c1_deck, args, ctx, fmt, editable_source=authored_sidecar
            )

        # C2 failed → fall back to Marp/html fallback with the generated markdown.
        # This path is a DEGRADED deck: no theme/brand, no layout variety, no
        # embedded images. Report that LOUDLY (content + structured) so neither the
        # user nor the agent mistakes a plain fallback for the real styled deck and
        # silently "finishes" on it. (The dominant live cause of landing here —
        # OpenRouter driver origin-not-approved — was fixed in 90828654; when that
        # is resolved the authored pipeline succeeds and this branch is rare.)
        if fallback_md:
            marp_args = SlidesGenerateArgs(
                goal=None,
                markdown=fallback_md,
                filename=args.filename,
                format=args.format,
                theme=args.theme,
                mode="markdown",
            )
            outcome = await self._run_marp_path(marp_args, ctx, fmt)
            note = (
                "\n\n⚠️ DEGRADED: this deck came out of the PLAIN fallback renderer — the "
                f"styled deck author didn't complete ({err}), so it has no theme, no "
                "images, and no layout variety. To get the full branded deck with "
                "images: make sure a capable driver model is selected and an image "
                "backend is configured in Settings → Image generation, then regenerate."
            )
            degraded_meta = dict(outcome.structured or {})
            degraded_meta.update(
                {"degraded": True, "degraded_reason": err, "renderer": "marp_fallback"}
            )
            return ToolOutcome(
                success=outcome.success,
                content=outcome.content + note,
                error=outcome.error,
                artifacts=outcome.artifacts,
                structured=degraded_meta,
            )

        return ToolOutcome(
            success=False,
            content=f"Deck generation failed: {err}",
            error=err or "deck_generation_failed",
        )

    async def _render_c1_deck(
        self,
        deck,
        args: SlidesGenerateArgs,
        ctx: ToolContext,
        fmt: str,
        *,
        editable_source: str | None = None,
    ) -> ToolOutcome:
        """Render a C1 Deck to the sandbox and return a ToolOutcome.

        ``editable_source`` is the ``{name}.authored.json`` path IFF this generation
        freshly wrote it (from generate_deck). It gates the in-app editor: a swallowed
        sidecar-write failure → None → no "Edit Slides" affordance, and never an
        affordance pointing at a stale leftover sidecar (no false affordance)."""
        from disco.tools.builtin._pptx_render import convert_to_pdf, render_html, render_pptx

        if ctx.sandbox is None:
            return ToolOutcome(success=False, content="No sandbox available to write the slide deck.")
        sbx = ctx.sandbox
        out_filename = f"{args.filename}.{fmt}"

        # A2: advertise the in-app editor ONLY when THIS run wrote a fresh sidecar.
        editable: dict[str, str] = (
            {"editable_source": editable_source} if editable_source else {}
        )

        if fmt == "html":
            html_str = render_html(deck)
            await sbx.write_file(out_filename, html_str.encode("utf-8"))
            return ToolOutcome(
                success=True,
                content=(
                    f"Slide deck '{args.filename}' written to {out_filename}\n"
                    f"Format: HTML (C3 brand renderer)\n"
                    f"Slides: {len(deck.slides)}"
                ),
                artifacts=[out_filename],
                structured={
                    "filename": out_filename,
                    "base_name": args.filename,
                    "format": "html",
                    "slide_count": len(deck.slides),
                    "renderer": "c3-brand",
                    # A2.0/A2.2: editable_source (the AuthoredDeck sidecar) is present
                    # ONLY when the sidecar write actually succeeded — its presence is
                    # what gates the in-app deck editor tab.
                    **editable,
                    "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
                },
            )

        if fmt == "pptx":
            pptx_bytes = render_pptx(deck)
            await sbx.write_file(out_filename, pptx_bytes)

            # Also write brand HTML alongside
            html_name = f"{args.filename}.html"
            try:
                html_str = render_html(deck)
                await sbx.write_file(html_name, html_str.encode("utf-8"))
                artifacts = [out_filename, html_name]
            except Exception:
                artifacts = [out_filename]

            # Attempt PDF via LibreOffice
            pdf_note = ""
            pdf_ok, pdf_err = await convert_to_pdf(ctx, out_filename)
            if pdf_ok:
                pdf_name = f"{args.filename}.pdf"
                artifacts.append(pdf_name)
            else:
                pdf_note = f"\nPDF: {pdf_err}"

            return ToolOutcome(
                success=True,
                content=(
                    f"Slide deck '{args.filename}' written to {out_filename}\n"
                    f"Format: PPTX (C3 native editable — real text boxes)\n"
                    f"Slides: {len(deck.slides)}{pdf_note}"
                ),
                artifacts=artifacts,
                structured={
                    "filename": out_filename,
                    "base_name": args.filename,
                    "format": "pptx",
                    "slide_count": len(deck.slides),
                    "renderer": "pptx-native",
                    # A2.0/A2.2: present only when the sidecar write actually succeeded.
                    **editable,
                    "slides": [{"type": s.type, "layout": s.layout} for s in deck.slides],
                },
            )

        if fmt == "pdf":
            # Render PPTX first, then convert
            pptx_bytes = render_pptx(deck)
            pptx_name = f"{args.filename}.pptx"
            await sbx.write_file(pptx_name, pptx_bytes)
            pdf_ok, pdf_err = await convert_to_pdf(ctx, pptx_name)
            if not pdf_ok:
                return ToolOutcome(
                    success=False,
                    content=f"PDF conversion failed: {pdf_err}",
                    error=pdf_err,
                )
            return ToolOutcome(
                success=True,
                content=(
                    f"Slide deck '{args.filename}' written to {out_filename}\n"
                    f"Format: PDF (via LibreOffice + C3 PPTX)\n"
                    f"Slides: {len(deck.slides)}"
                ),
                artifacts=[out_filename, pptx_name],
                structured={
                    "filename": out_filename,
                    "base_name": args.filename,
                    "format": "pdf",
                    "slide_count": len(deck.slides),
                    "renderer": "libreoffice",
                    # A2: a fresh sidecar makes even a pdf-format deck editable (the
                    # editor re-renders html/pptx; the pdf is flagged stale on save).
                    **editable,
                },
            )

        return ToolOutcome(
            success=False,
            content=f"Unsupported format: {fmt!r}",
            error=f"Unsupported format: {fmt!r}",
        )

    async def _run_marp_path(
        self, args: SlidesGenerateArgs, ctx: ToolContext, fmt: str
    ) -> ToolOutcome:
        """Run the Marp/markdown rendering path."""
        out_filename = f"{args.filename}.{fmt}"

        # Prepare markdown: inject theme as frontmatter if provided
        markdown = args.markdown
        if args.theme:
            theme_block = f"<!-- theme: custom -->\n<style>\n{args.theme}\n</style>\n\n"
            markdown = theme_block + markdown

        have_marp = await _marp_available(ctx)

        if fmt == "html":
            if have_marp:
                return await self._render_with_marp(markdown, out_filename, fmt, ctx, args)
            return await self._render_html_fallback(markdown, out_filename, ctx, args)

        if not have_marp:
            # codex P1 / compat: now that the default format is pptx, a no-goal markdown
            # caller in a Marp-less sandbox must NOT hard-fail (the old default was html,
            # which always worked). Degrade to the always-available pure-HTML render so the
            # default-format change stays compatibility-neutral — a deck is still produced.
            html_name = out_filename.rsplit(".", 1)[0] + ".html"
            outcome = await self._render_html_fallback(markdown, html_name, ctx, args)
            # codex round-2 honesty: make the format DOWNGRADE explicit — the caller asked for
            # pptx/pdf but Marp is absent, so an HTML deck was produced instead of silently
            # implying the requested format succeeded.
            return outcome.model_copy(
                update={
                    "content": (
                        f"{outcome.content}\nNOTE: {fmt.upper()} was requested but the Marp "
                        f"CLI is unavailable in this sandbox — produced an HTML deck instead."
                    ),
                    "structured": {
                        **(outcome.structured or {}),
                        "requested_format": fmt,
                        "degraded_to": "html",
                    },
                }
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
                "\nNOTE: Marp PPTX output is image-based slides (each slide is a static image). "
                "Native editable PPTX with real text boxes is available via the C3 renderer "
                "(_pptx_render.py) when a structured deck is supplied (C2 pipeline)."
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
