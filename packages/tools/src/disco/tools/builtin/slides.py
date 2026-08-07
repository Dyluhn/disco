"""Slide generation tool — writes rendered slide decks (HTML/PDF/PPTX) to
the workspace.

Primary path (C2): when a ``goal`` is supplied, the staged C2 pipeline
(outline → fill → assets → lower_deck → C3 render) generates a structured
AuthoredDeck and renders it to native editable .pptx / brand HTML.  On parse
failure after one retry the Marp fallback is taken automatically — no crash.
Provider-declared truncation is different: it fails explicitly without rendering
an incomplete or plain substitute deck.

Marp fallback: when only ``markdown`` is supplied, or when a completed C2
response is malformed after correction, the tool falls back to Marp CLI (or
the pure-HTML fallback when Marp is absent).

Format support:
  - html: always works (C3 brand HTML, or Marp CLI, or self-contained fallback).
  - pdf: requires marp + Chromium or LibreOffice in the sandbox image.
  - pptx: C3 native editable PPTX (real text boxes) via python-pptx; or
          Marp --pptx (image-based) when the C2 path is not used.

Internal layout
----------------
The C1-deck-render path, the Marp-CLI-in-sandbox helpers, the dependency-free
markdown-fallback renderer, and the export-render fact stamping all live in
the private `slides_parts` subpackage (split by real responsibility — see
`slides_parts/__init__.py`). This module remains the sole public
compatibility/export facade: it re-imports every name unchanged, so every
existing import path (production code and tests) keeps working.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from .image_gen import ImageGenNotConfigured, select_image_backend
from .slides_parts._c1_render import _render_c1_deck_impl as _render_c1_deck_impl

# --- compatibility re-exports (PKG-10-MEDIA) --------------------------------
# Names this module exposed BEFORE the parts split. Consumers and the test
# suite reach several of them through this module object (including via
# monkeypatch), so the facade must keep exposing every one. Restored
# programmatically by diffing this module's top-level names against its
# parent commit; PKG-13-FACADES owns their eventual deletion.
from .slides_parts._export_stamp import (
    EXPORT_RENDER_KEY as EXPORT_RENDER_KEY,
)
from .slides_parts._export_stamp import _stamp_export_render_impl as _stamp_export_render_impl
from .slides_parts._export_stamp import (
    check_export_render as check_export_render,
)
from .slides_parts._markdown_fallback import (
    _BOLD_RE as _BOLD_RE,
)
from .slides_parts._markdown_fallback import (
    _FENCE_RE as _FENCE_RE,
)
from .slides_parts._markdown_fallback import (
    _IMAGE_RE as _IMAGE_RE,
)
from .slides_parts._markdown_fallback import (
    _INLINE_CODE_RE as _INLINE_CODE_RE,
)
from .slides_parts._markdown_fallback import (
    _ITALIC_RE as _ITALIC_RE,
)
from .slides_parts._markdown_fallback import (
    _LINK_RE as _LINK_RE,
)
from .slides_parts._markdown_fallback import (
    _MARP_MINIMAL_THEME as _MARP_MINIMAL_THEME,
)
from .slides_parts._markdown_fallback import (
    _basic_md_to_html as _basic_md_to_html,
)
from .slides_parts._markdown_fallback import (
    _fallback_html as _fallback_html,
)
from .slides_parts._markdown_fallback import (
    _inline_markdown_to_html as _inline_markdown_to_html,
)
from .slides_parts._markdown_fallback import (
    _split_slides as _split_slides,
)
from .slides_parts._markdown_fallback import (
    dedent as dedent,
)
from .slides_parts._markdown_fallback import (
    html as html,
)
from .slides_parts._marp_path import (
    _marp_available as _marp_available,
)
from .slides_parts._marp_path import (
    _marp_render_in_sandbox as _marp_render_in_sandbox,
)

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
        description=(
            "Approximate number of slides (used by the C2 pipeline; ignored for markdown path)."
        ),
    )
    mode: Literal["deck", "markdown"] = Field(
        default="deck",
        description=(
            "'deck' uses the C2 structured pipeline (default when goal supplied). "
            "'markdown' forces the Marp/fallback path regardless of goal."
        ),
    )


# ---- tool implementation -----------------------------------------------------


class SlidesTool:
    definition = ToolDef(
        name="slides_generate",
        description=(
            "Write a slide deck (HTML/PDF/PPTX) to the workspace.\n\n"
            "Primary path (recommended): supply ``goal`` (e.g. 'A 6-slide investor "
            "pitch for an EV battery startup') and the C2 pipeline generates a "
            "structured, brand-themed deck automatically (native editable PPTX + "
            "brand HTML). Completed malformed output falls back to Marp after one "
            "correction attempt.\n\n"
            "Markdown path (backward-compat): supply ``markdown`` with '---' slide "
            "separators; rendered via Marp CLI (image-based PPTX) or the HTML "
            "fallback. Provider-truncated output fails explicitly instead of being "
            "misclassified as malformed JSON.\n\n"
            "HTML always works.  PDF and native PPTX require the sandbox toolchain."
        ),
        args_model=SlidesGenerateArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=None,
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
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

        # Filename hygiene (gauntlet e-web 2026-07-07): the driver passed
        # filename='>residential-…' and every artifact landed with a literal '>'
        # prefix — shell-hostile and invalid on Windows. Strip OS/shell-hostile
        # characters and stray whitespace; '.' and '/' are deliberately left
        # alone so the sandbox jail still sees (and rejects) traversal attempts.
        clean = re.sub(r'[<>:"|?*\\\x00-\x1f]', "", args.filename).strip().strip("-")
        if clean != args.filename:
            args = args.model_copy(update={"filename": clean})
        if not clean:
            return ToolOutcome(
                success=False,
                content=(
                    f"filename {args.filename!r} is empty after removing invalid "
                    "characters (<>:\"|?* and control chars). Provide a plain base "
                    "name like 'my-deck'."
                ),
                error="invalid filename",
            )

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
                    f"slides_generate (mode={args.mode!r}) has no deck content to "
                    "render. Provide `goal` — a "
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

    async def _stamp_export_render(self, outcome: ToolOutcome, ctx: ToolContext) -> ToolOutcome:
        """Read the produced deck file back from the sandbox and add ExportRenderFacts
        to the tool result's structured payload. Best-effort: on any read/parse
        failure the payload is left unstamped (the gate then falls through — absence
        is honest, never a fabricated verdict)."""
        return await _stamp_export_render_impl(outcome, ctx)

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
        """Run C2; only completed malformed output may fall back to Marp."""
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

        c1_deck, fallback_md, err, authored_sidecar, image_stats = await generate_deck(
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
                c1_deck,
                args,
                ctx,
                fmt,
                editable_source=authored_sidecar,
                image_stats=image_stats,
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
        image_stats=None,
    ) -> ToolOutcome:
        """Render a C1 Deck to the sandbox and return a ToolOutcome.

        ``editable_source`` is the ``{name}.authored.json`` path IFF this generation
        freshly wrote it (from generate_deck). It gates the in-app editor: a swallowed
        sidecar-write failure → None → no "Edit Slides" affordance, and never an
        affordance pointing at a stale leftover sidecar (no false affordance).
        ``image_stats`` (ImageGenStats) carries the honest image outcome — its note
        goes in the CONTENT (so the agent knows images failed / were unconfigured)
        and its numbers in `structured.images`."""
        return await _render_c1_deck_impl(
            deck, args, ctx, fmt, editable_source=editable_source, image_stats=image_stats
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
