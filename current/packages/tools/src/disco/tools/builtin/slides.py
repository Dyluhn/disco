"""Slide generation tool — writes structured authored decks to the workspace.

Primary path (C2): when a ``goal`` is supplied, the staged C2 pipeline
(outline → fill → assets → lower_deck → C3 render) generates a structured
AuthoredDeck and renders it to native editable .pptx / brand HTML.  On parse
failure, it makes one correction attempt and then fails without shipping a
plain substitute under the authored-deck request.
Provider-declared truncation is different: it fails explicitly without rendering
an incomplete or plain substitute deck.

The model-facing contract is deliberately narrow: ``goal`` is grounded by the
host's authoritative report, and the structured pipeline emits native editable
PPTX plus authored JSON and branded HTML preview. The old Markdown splitter is
kept import-compatible for callers, but is not a selectable product path.

Internal layout
----------------
The C1-deck-render path and export-render fact stamping live in the private
`slides_parts` subpackage (split by real responsibility — see
`slides_parts/__init__.py`). This module remains the public compatibility
facade for the small set of legacy helpers still used by callers and tests.
"""

from __future__ import annotations

import re
from typing import Literal

from disco.core.effects import EffectCapability
from pydantic import BaseModel, ConfigDict, Field

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
    _split_slides as _split_slides,
)

# ---- args model --------------------------------------------------------------


class SlidesGenerateArgs(BaseModel):
    """Arguments for the slides_generate tool."""

    # Legacy markdown/mode inputs stay constructible for compatibility with old
    # callers, but are not in the generated tool schema and are rejected by run().
    model_config = ConfigDict(
        extra="allow",
        # Keep old direct Python callers diagnosable while preventing the
        # model-facing JSON schema from advertising arbitrary legacy inputs.
        json_schema_extra={"additionalProperties": False},
    )

    goal: str | None = Field(
        default=None,
        description=(
            "REQUIRED for deck generation. The deck's "
            "content AND intent, in natural language — e.g. 'A 7-slide technical brief "
            "on post-training quantization for LLMs: executive summary, the methods "
            "landscape, weight-only vs weight+activation, bit-width as the dominant "
            "degradation driver, model-scale effects, a cross-source comparison, and "
            "limitations'. The C2 pipeline builds a structured, themed, image-bearing "
            "deck from this. This tool CANNOT see the conversation or any research "
            "report — the content must be supplied by the typed host handoff (``theme``, "
            "``slide_count``, and ``format`` carry no factual content)."
        ),
    )
    filename: str = Field(
        description=(
            "Base filename WITHOUT extension (e.g. 'my-deck'). The tool writes a "
            ".pptx plus authored JSON and branded HTML preview."
        )
    )
    format: Literal["pptx"] = Field(
        default="pptx",
        description=(
            "Canonical output format: native editable 'pptx'. The tool also emits "
            "authored JSON and a branded HTML editor preview alongside it."
        ),
    )
    theme: str | None = Field(
        default=None,
        description="Optional branded theme name for the authored deck.",
    )
    slide_count: int = Field(
        default=5,
        ge=2,
        le=30,
        description=(
            "Approximate number of slides used by the structured authored-deck pipeline."
        ),
    )


# ---- tool implementation -----------------------------------------------------


class SlidesTool:
    definition = ToolDef(
        name="slides_generate",
        description=(
            "Write a structured authored slide deck (native editable PPTX plus "
            "authored JSON and branded HTML preview) to the workspace.\n\n"
            "Primary path (recommended): supply ``goal`` (e.g. 'A 6-slide investor "
            "pitch for an EV battery startup') and the C2 pipeline generates a "
            "structured, brand-themed deck automatically (native editable PPTX + "
            "brand HTML). Malformed authored output receives one correction attempt "
            "and then fails without delivering a plain substitute.\n\n"
            "Provider-truncated or unavailable structured generation fails explicitly; "
            "the tool never substitutes Markdown, Marp, or basic HTML."
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
        # killing it three times over → incomplete authored output. 15 min headroom.
        timeout_s=900,
    )

    def execution_scope(self, args: SlidesGenerateArgs) -> str:
        if args.goal:
            return "in_process"
        return "sandbox"

    async def run(self, args: SlidesGenerateArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None  # sandbox tools always receive an instance

        legacy = args.model_extra or {}
        if "markdown" in legacy or "mode" in legacy:
            return ToolOutcome(
                success=False,
                content=(
                    "Markdown/Marp slide inputs are retired; use the structured "
                    "authored-deck goal for native PPTX output."
                ),
                error="legacy_markdown_slide_path_disabled",
            )

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
                    'characters (<>:"|?* and control chars). Provide a plain base '
                    "name like 'my-deck'."
                ),
                error="invalid filename",
            )

        fmt = args.format.lower()
        if fmt != "pptx":
            return ToolOutcome(
                success=False,
                content=f"Unsupported format: {fmt!r}. Only native 'pptx' is supported.",
                error=f"Unsupported format: {fmt!r}.",
            )

        # The only model-facing content source is the structured goal. The host
        # supplies the authoritative report separately through the typed handoff.
        if not (args.goal and args.goal.strip()):
            return ToolOutcome(
                success=False,
                content=(
                    "slides_generate requires a structured `goal` grounded by the "
                    "authoritative report. No deck was written."
                ),
                error="slides_generate requires a structured goal",
            )

        # ---- structured authored-deck pipeline ----
        outcome = await self._run_c2_pipeline(args, ctx, fmt)

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
            "verification is needed: the slide renderer and export validator already "
            "checked the artifact. Do NOT run an index.html or web-app check, re-open, "
            "re-read, re-verify, or file_write "
            "into these files; they are already produced and delivered to the user. "
            "Call finish to complete the task."
        )

    async def _run_c2_pipeline(
        self, args: SlidesGenerateArgs, ctx: ToolContext, fmt: str
    ) -> ToolOutcome:
        """Run the authored C2/C3 path without substituting another product."""
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

        c1_deck, _fallback_md, err, authored_sidecar, image_stats = await generate_deck(
            args.goal,
            args.filename,
            ctx,
            backend,
            slide_count=args.slide_count,
            source_report=getattr(ctx, "source_report", None),
            completion=getattr(ctx, "provider_completion", None),
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

        # Never convert a structured failure into a successful lesser product.
        return ToolOutcome(
            success=False,
            content=(
                "Authored slide generation failed before delivery; no substitute deck "
                f"was written. {err or 'The structured author did not complete.'}"
            ),
            error=err or "deck_generation_failed",
            structured={
                "degraded": False,
                "renderer": "authored",
                "stage": "authoring",
            },
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
