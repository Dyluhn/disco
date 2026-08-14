"""`design_lint` — the AST-light slop scanner (AppKit EPIC D3).

A generated web app betrays its LLM origin through a small, recurring set of
visual tells: Inter/Geist everywhere, an AI-purple primary, gradient-clipped hero
text, dark-neon-glow surfaces, pill-button monoculture, emoji-as-icons, animation
soup, and the centered-hero / three-cards / CTA section sequence. `design_lint`
SCANS the generated workspace (CSS + HTML/JSX/Vue/Svelte) plus the workspace's
`.disco/designspec.json` and returns a STRUCTURED verdict: a ranked list of
findings, each `{rule_id, severity, path, line, evidence, choice_key, message}`.

Two deliberate design choices:

* AST-light. No CSS/JS parser dependency (P0): rules are regex + string scanning
  over curated slop constants. Best-effort and conservative — a rule that can't be
  sure stays quiet rather than firing a false positive.
* Justification-aware, VALUE-AWARE SUPPRESSION. An off-default value is NOT
  automatically slop — it is slop only when it is UNJUSTIFIED. A finding is
  suppressed when `.disco/designspec.json` carries a SUBSTANTIVE justification for
  the finding's canonical `choice_key` (the same keys `recipes.to_design_spec`
  emits) AND — for the rules whose `choice_key` maps to a concrete DesignSpec
  field — the spec's ACTUAL field value equals the FLAGGED value. So justifying
  `palette.primary=#000000` does NOT excuse a `#7c3aed` finding, and justifying a
  Roboto heading does NOT excuse an Inter one: the justified value must BE the
  thing the implementation uses. A missing or invalid DesignSpec suppresses
  NOTHING — you cannot claim intent without the spec — so the relevant rules fire.

The pure engine (`lint_design`) takes an in-memory `{path: text}` map + an
optional `DesignSpec`; the `DesignLintTool` is a thin, READ-ONLY sandbox wrapper
that walks the workspace, loads the spec, and calls it. Read-only by contract so
Epic G can use it as a verification probe and re-linting is non-productive work.

Layering: tools -> core is allowed; this imports `DesignSpec` + the canonical
choice keys from `disco.core.appkit` (the single source of truth recipes write).

Package split (PKG-10C): the constants, finding model, scanning helpers, deck-
slide helpers, suppression logic, every rule family, and the pure engine driver
now live under `design_lint_parts/` — cohesive modules each well under the
module/class/callable size + complexity budgets. This module is the sole public
facade: every name it used to define locally is re-imported here unchanged
(including private ones — tests and production code reach several `_rule_*`
functions and internal constants through this module object, some via
monkeypatch), so nothing importing `disco.tools.builtin.design_lint` needs to
change. `design_lint_parts` is a private implementation detail; external code
must never import from it directly.
"""

from __future__ import annotations

import re as re
import shlex as shlex
from collections.abc import Mapping as Mapping
from dataclasses import dataclass as dataclass
from typing import TYPE_CHECKING as TYPE_CHECKING

from disco.core import SecurityRisk
from disco.core.appkit import (
    CHOICE_COMPONENT_BUTTON_RADIUS as CHOICE_COMPONENT_BUTTON_RADIUS,
)
from disco.core.appkit import (
    CHOICE_EFFECTS_GLOW as CHOICE_EFFECTS_GLOW,
)
from disco.core.appkit import (
    CHOICE_EFFECTS_GRADIENT_TEXT as CHOICE_EFFECTS_GRADIENT_TEXT,
)
from disco.core.appkit import (
    CHOICE_ICONS_STYLE as CHOICE_ICONS_STYLE,
)
from disco.core.appkit import (
    CHOICE_LAYOUT_SECTION_SEQUENCE as CHOICE_LAYOUT_SECTION_SEQUENCE,
)
from disco.core.appkit import (
    CHOICE_MOTION_DENSITY as CHOICE_MOTION_DENSITY,
)
from disco.core.appkit import (
    CHOICE_PALETTE_ACCENT as CHOICE_PALETTE_ACCENT,
)
from disco.core.appkit import (
    CHOICE_PALETTE_PRIMARY as CHOICE_PALETTE_PRIMARY,
)
from disco.core.appkit import (
    CHOICE_TYPOGRAPHY_BODY as CHOICE_TYPOGRAPHY_BODY,
)
from disco.core.appkit import (
    CHOICE_TYPOGRAPHY_HEADING as CHOICE_TYPOGRAPHY_HEADING,
)
from disco.core.appkit import (
    MAX_DESIGNSPEC_BYTES as MAX_DESIGNSPEC_BYTES,
)
from disco.core.appkit import (
    load_design_spec_from_bytes,
)
from disco.core.appkit.spec import DesignSpec
from disco.core.context import ArtifactMemoryStore as ArtifactMemoryStore
from disco.core.design import DesignDirection
from disco.core.design import direction_from_markdown as direction_from_markdown
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field
from pydantic import ConfigDict as ConfigDict

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ._outcomes import fail_outcome

# ---- compatibility re-exports (PKG-10C) ---------------------------------------
# Names this module defined BEFORE the design_lint_parts split. Consumers and the
# test suite reach several of them through this module object (including via
# monkeypatch — see `_engine.lint_design`, called by bare name from `run()` below
# so a `monkeypatch.setattr(design_lint_module, "lint_design", ...)` is honored),
# so the facade must keep exposing every one, unchanged.
from .design_lint_parts._constants import (
    _AI_PURPLE_HEXES as _AI_PURPLE_HEXES,
)
from .design_lint_parts._constants import (
    _AI_PURPLE_RGB as _AI_PURPLE_RGB,
)
from .design_lint_parts._constants import (
    _ANIMATION_THRESHOLD as _ANIMATION_THRESHOLD,
)
from .design_lint_parts._constants import (
    _BACKDROP_DECL_RE as _BACKDROP_DECL_RE,
)
from .design_lint_parts._constants import (
    _BACKGROUND_DECL_RE as _BACKGROUND_DECL_RE,
)
from .design_lint_parts._constants import (
    _BUTTON_SELECTOR_TOKENS as _BUTTON_SELECTOR_TOKENS,
)
from .design_lint_parts._constants import (
    _CHOICE_DECK_ARCHETYPE_MONOTONY as _CHOICE_DECK_ARCHETYPE_MONOTONY,
)
from .design_lint_parts._constants import (
    _CHOICE_DECK_GLASS as _CHOICE_DECK_GLASS,
)
from .design_lint_parts._constants import (
    _CHOICE_DECK_HANDWRITTEN_HTML as _CHOICE_DECK_HANDWRITTEN_HTML,
)
from .design_lint_parts._constants import (
    _CHOICE_DECK_IMAGERY as _CHOICE_DECK_IMAGERY,
)
from .design_lint_parts._constants import (
    _CHOICE_DECK_ORPHAN_SLIDE as _CHOICE_DECK_ORPHAN_SLIDE,
)
from .design_lint_parts._constants import (
    _CHOICE_DECK_TEXT_BUDGET as _CHOICE_DECK_TEXT_BUDGET,
)
from .design_lint_parts._constants import (
    _CHOICE_DECK_TYPE_FLOOR as _CHOICE_DECK_TYPE_FLOOR,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_COLOR_COUNT as _CHOICE_WEB_COLOR_COUNT,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_DEFAULT_HIDDEN_CONTENT as _CHOICE_WEB_DEFAULT_HIDDEN_CONTENT,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_FONT_COUNT as _CHOICE_WEB_FONT_COUNT,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_FONT_SIZE as _CHOICE_WEB_FONT_SIZE,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_GLASS as _CHOICE_WEB_GLASS,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_HOVER_A11Y as _CHOICE_WEB_HOVER_A11Y,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_HOVER_SCALE as _CHOICE_WEB_HOVER_SCALE,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_LINE_HEIGHT as _CHOICE_WEB_LINE_HEIGHT,
)
from .design_lint_parts._constants import (
    _CHOICE_WEB_TEXT_OVER_IMAGE as _CHOICE_WEB_TEXT_OVER_IMAGE,
)
from .design_lint_parts._constants import (
    _CLASS_ATTR_RE as _CLASS_ATTR_RE,
)
from .design_lint_parts._constants import (
    _COLOR_MERGE_DISTANCE as _COLOR_MERGE_DISTANCE,
)
from .design_lint_parts._constants import (
    _COLOR_SATURATION_FLOOR as _COLOR_SATURATION_FLOOR,
)
from .design_lint_parts._constants import (
    _COMMENT_RE as _COMMENT_RE,
)
from .design_lint_parts._constants import (
    _CSS_BLOCK_RE as _CSS_BLOCK_RE,
)
from .design_lint_parts._constants import (
    _CSS_EXTS as _CSS_EXTS,
)
from .design_lint_parts._constants import (
    _DECK_BASE_WIDTH_PX as _DECK_BASE_WIDTH_PX,
)
from .design_lint_parts._constants import (
    _DECK_BULLET_LIMIT as _DECK_BULLET_LIMIT,
)
from .design_lint_parts._constants import (
    _DECK_PIPELINE_GENERATOR_MARKER as _DECK_PIPELINE_GENERATOR_MARKER,
)
from .design_lint_parts._constants import (
    _DECK_TEXT_WORD_LIMIT as _DECK_TEXT_WORD_LIMIT,
)
from .design_lint_parts._constants import (
    _DECK_TYPE_FLOOR_PX as _DECK_TYPE_FLOOR_PX,
)
from .design_lint_parts._constants import (
    _DIRECTION_DRIFT_MIN_DISTANCE as _DIRECTION_DRIFT_MIN_DISTANCE,
)
from .design_lint_parts._constants import (
    _DIRECTION_GENERIC_FONTS as _DIRECTION_GENERIC_FONTS,
)
from .design_lint_parts._constants import (
    _DIRECTION_MIN_SATURATION as _DIRECTION_MIN_SATURATION,
)
from .design_lint_parts._constants import (
    _EMOJI_RE as _EMOJI_RE,
)
from .design_lint_parts._constants import (
    _EMOJI_THRESHOLD as _EMOJI_THRESHOLD,
)
from .design_lint_parts._constants import (
    _FONT_CONTEXT_RE as _FONT_CONTEXT_RE,
)
from .design_lint_parts._constants import (
    _FONT_SIZE_DECL_RE as _FONT_SIZE_DECL_RE,
)
from .design_lint_parts._constants import (
    _GENERIC_FONTS as _GENERIC_FONTS,
)
from .design_lint_parts._constants import (
    _HEADING_SELECTOR_TOKENS as _HEADING_SELECTOR_TOKENS,
)
from .design_lint_parts._constants import (
    _HEX_RE as _HEX_RE,
)
from .design_lint_parts._constants import (
    _LINE_HEIGHT_DECL_RE as _LINE_HEIGHT_DECL_RE,
)
from .design_lint_parts._constants import (
    _MARKUP_EXTS as _MARKUP_EXTS,
)
from .design_lint_parts._constants import (
    _MAX_BRAND_COLORS as _MAX_BRAND_COLORS,
)
from .design_lint_parts._constants import (
    _MAX_DEPTH as _MAX_DEPTH,
)
from .design_lint_parts._constants import (
    _MAX_FILE_BYTES as _MAX_FILE_BYTES,
)
from .design_lint_parts._constants import (
    _MAX_FILES as _MAX_FILES,
)
from .design_lint_parts._constants import (
    _MAX_FONT_FAMILIES as _MAX_FONT_FAMILIES,
)
from .design_lint_parts._constants import (
    _MAX_SPEC_BYTES,
    _SKIP_DIRS,
    SCAN_EXTS,
)
from .design_lint_parts._constants import (
    _MIN_FONT_SIZE_PX as _MIN_FONT_SIZE_PX,
)
from .design_lint_parts._constants import (
    _MIN_LINE_HEIGHT as _MIN_LINE_HEIGHT,
)
from .design_lint_parts._constants import (
    _NEON_HEXES as _NEON_HEXES,
)
from .design_lint_parts._constants import (
    _PILL_RADII as _PILL_RADII,
)
from .design_lint_parts._constants import (
    _SCRIPT_STYLE_RE as _SCRIPT_STYLE_RE,
)
from .design_lint_parts._constants import (
    _SECTION_OPEN_RE as _SECTION_OPEN_RE,
)
from .design_lint_parts._constants import (
    _SIZE_PROBE_TIMEOUT_S as _SIZE_PROBE_TIMEOUT_S,
)
from .design_lint_parts._constants import (
    _STYLE_ATTR_RE as _STYLE_ATTR_RE,
)
from .design_lint_parts._constants import (
    _TAG_RE as _TAG_RE,
)
from .design_lint_parts._constants import (
    _WEB_BANNED_DEFAULT_FONTS as _WEB_BANNED_DEFAULT_FONTS,
)
from .design_lint_parts._constants import (
    _WEB_CONTENT_SELECTOR_RE as _WEB_CONTENT_SELECTOR_RE,
)
from .design_lint_parts._constants import (
    _WEB_HEADING_SELECTOR_RE as _WEB_HEADING_SELECTOR_RE,
)
from .design_lint_parts._constants import (
    _WEB_HEADING_TARGET_RE as _WEB_HEADING_TARGET_RE,
)
from .design_lint_parts._constants import (
    _WEB_HEROISH_RE as _WEB_HEROISH_RE,
)
from .design_lint_parts._constants import (
    _WEB_HIDDEN_DECL_RE as _WEB_HIDDEN_DECL_RE,
)
from .design_lint_parts._constants import (
    _WEB_INTERACTIVE_SELECTOR_RE as _WEB_INTERACTIVE_SELECTOR_RE,
)
from .design_lint_parts._constants import (
    _WEB_JS_GATED_SELECTOR_RE as _WEB_JS_GATED_SELECTOR_RE,
)
from .design_lint_parts._constants import (
    _WEB_MEDIA_CONTAINER_RE as _WEB_MEDIA_CONTAINER_RE,
)
from .design_lint_parts._constants import (
    _WEB_TEXT_TAG_RE as _WEB_TEXT_TAG_RE,
)
from .design_lint_parts._constants import (
    _WORD_RE as _WORD_RE,
)
from .design_lint_parts._deck_slides import (
    _attr_value as _attr_value,
)
from .design_lint_parts._deck_slides import (
    _class_tokens as _class_tokens,
)
from .design_lint_parts._deck_slides import (
    _deck_image_count as _deck_image_count,
)
from .design_lint_parts._deck_slides import (
    _DeckSlide as _DeckSlide,
)
from .design_lint_parts._deck_slides import (
    _extract_deck_slides as _extract_deck_slides,
)
from .design_lint_parts._deck_slides import (
    _global_deck_backdrop_is_flat as _global_deck_backdrop_is_flat,
)
from .design_lint_parts._deck_slides import (
    _has_attr as _has_attr,
)
from .design_lint_parts._deck_slides import (
    _has_class as _has_class,
)
from .design_lint_parts._deck_slides import (
    _has_deck_wrapper as _has_deck_wrapper,
)
from .design_lint_parts._deck_slides import (
    _has_slide_nav_markers as _has_slide_nav_markers,
)
from .design_lint_parts._deck_slides import (
    _is_deck_section_candidate as _is_deck_section_candidate,
)
from .design_lint_parts._deck_slides import (
    _slide_has_flat_backdrop as _slide_has_flat_backdrop,
)
from .design_lint_parts._deck_slides import (
    _slide_has_media as _slide_has_media,
)
from .design_lint_parts._deck_slides import (
    _slide_text_blocks as _slide_text_blocks,
)
from .design_lint_parts._deck_slides import (
    _style_attrs as _style_attrs,
)
from .design_lint_parts._engine import (
    _bounded_read,
    lint_design,
)
from .design_lint_parts._engine import (
    load_committed_direction as load_committed_direction,
)
from .design_lint_parts._model import (
    _MIN_REASON_LEN as _MIN_REASON_LEN,
)
from .design_lint_parts._model import (
    _RULE_META as _RULE_META,
)
from .design_lint_parts._model import (
    _SEVERITY_RANK as _SEVERITY_RANK,
)
from .design_lint_parts._model import (
    SUPPRESSIBLE_CHOICE_KEYS as SUPPRESSIBLE_CHOICE_KEYS,
)
from .design_lint_parts._model import (
    DesignFinding as DesignFinding,
)
from .design_lint_parts._render import (
    _RENDER_FINDING_LIMIT as _RENDER_FINDING_LIMIT,
)
from .design_lint_parts._render import render_verdict as _render
from .design_lint_parts._rules_basic import (
    _rule_ai_purple as _rule_ai_purple,
)
from .design_lint_parts._rules_basic import (
    _rule_font_size_too_small as _rule_font_size_too_small,
)
from .design_lint_parts._rules_basic import (
    _rule_generic_font as _rule_generic_font,
)
from .design_lint_parts._rules_basic import (
    _rule_tight_line_height as _rule_tight_line_height,
)
from .design_lint_parts._rules_basic import (
    _rule_too_many_colors as _rule_too_many_colors,
)
from .design_lint_parts._rules_basic import (
    _rule_too_many_fonts as _rule_too_many_fonts,
)
from .design_lint_parts._rules_basic import (
    _rule_web_banned_default_font as _rule_web_banned_default_font,
)
from .design_lint_parts._rules_deck import (
    _rule_deck_bullet_monotony as _rule_deck_bullet_monotony,
)
from .design_lint_parts._rules_deck import (
    _rule_deck_glass as _rule_deck_glass,
)
from .design_lint_parts._rules_deck import (
    _rule_deck_handwritten_html as _rule_deck_handwritten_html,
)
from .design_lint_parts._rules_deck import (
    _rule_deck_missing_imagery as _rule_deck_missing_imagery,
)
from .design_lint_parts._rules_deck import (
    _rule_deck_orphan_slide as _rule_deck_orphan_slide,
)
from .design_lint_parts._rules_deck import (
    _rule_deck_text_budget as _rule_deck_text_budget,
)
from .design_lint_parts._rules_deck import (
    _rule_deck_type_floor as _rule_deck_type_floor,
)
from .design_lint_parts._rules_deck import (
    _slide_is_pure_bullet_list as _slide_is_pure_bullet_list,
)
from .design_lint_parts._rules_direction import (
    _direction_committed_families as _direction_committed_families,
)
from .design_lint_parts._rules_direction import (
    _direction_committed_rgbs as _direction_committed_rgbs,
)
from .design_lint_parts._rules_direction import (
    _rule_direction_conformance as _rule_direction_conformance,
)
from .design_lint_parts._rules_effects import (
    _rule_dark_neon_glow as _rule_dark_neon_glow,
)
from .design_lint_parts._rules_effects import (
    _rule_gradient_hero_text as _rule_gradient_hero_text,
)
from .design_lint_parts._rules_effects import (
    _rule_too_many_animations as _rule_too_many_animations,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_centered_hero_3_cards_cta as _rule_centered_hero_3_cards_cta,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_emoji_icons as _rule_emoji_icons,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_pill_buttons as _rule_pill_buttons,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_web_default_hidden_content as _rule_web_default_hidden_content,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_web_glass as _rule_web_glass,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_web_hover_only_interactivity as _rule_web_hover_only_interactivity,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_web_reflexive_hover_scale as _rule_web_reflexive_hover_scale,
)
from .design_lint_parts._rules_web_interaction import (
    _rule_web_text_over_image_no_scrim as _rule_web_text_over_image_no_scrim,
)
from .design_lint_parts._scan_helpers import (
    _backdrop_without_saturate as _backdrop_without_saturate,
)
from .design_lint_parts._scan_helpers import (
    _bullet_count as _bullet_count,
)
from .design_lint_parts._scan_helpers import (
    _class_has_focus_visible_utility as _class_has_focus_visible_utility,
)
from .design_lint_parts._scan_helpers import (
    _class_has_hover_utility as _class_has_hover_utility,
)
from .design_lint_parts._scan_helpers import (
    _ext_of,
)
from .design_lint_parts._scan_helpers import (
    _first_font_family as _first_font_family,
)
from .design_lint_parts._scan_helpers import (
    _font_size_px as _font_size_px,
)
from .design_lint_parts._scan_helpers import (
    _has_background_image_url as _has_background_image_url,
)
from .design_lint_parts._scan_helpers import (
    _has_colorful_backdrop as _has_colorful_backdrop,
)
from .design_lint_parts._scan_helpers import (
    _has_scrim_or_overlay as _has_scrim_or_overlay,
)
from .design_lint_parts._scan_helpers import (
    _inside_css_guard as _inside_css_guard,
)
from .design_lint_parts._scan_helpers import (
    _is_flat_background_value as _is_flat_background_value,
)
from .design_lint_parts._scan_helpers import (
    _is_generic_font_family as _is_generic_font_family,
)
from .design_lint_parts._scan_helpers import (
    _line_of as _line_of,
)
from .design_lint_parts._scan_helpers import (
    _luminance as _luminance,
)
from .design_lint_parts._scan_helpers import (
    _norm_color as _norm_color,
)
from .design_lint_parts._scan_helpers import (
    _norm_font_name as _norm_font_name,
)
from .design_lint_parts._scan_helpers import (
    _primary_families as _primary_families,
)
from .design_lint_parts._scan_helpers import (
    _resolve_rgb as _resolve_rgb,
)
from .design_lint_parts._scan_helpers import (
    _rgb_distance as _rgb_distance,
)
from .design_lint_parts._scan_helpers import (
    _rgb_saturation as _rgb_saturation,
)
from .design_lint_parts._scan_helpers import (
    _selector_targets_body_or_heading as _selector_targets_body_or_heading,
)
from .design_lint_parts._scan_helpers import (
    _selector_targets_interactive as _selector_targets_interactive,
)
from .design_lint_parts._scan_helpers import (
    _style_has_backdrop_filter as _style_has_backdrop_filter,
)
from .design_lint_parts._scan_helpers import (
    _style_has_flat_background as _style_has_flat_background,
)
from .design_lint_parts._scan_helpers import (
    _visible_text as _visible_text,
)
from .design_lint_parts._scan_helpers import (
    _word_count as _word_count,
)
from .design_lint_parts._scan_helpers import (
    html_lib as html_lib,
)
from .design_lint_parts._suppression import (
    _font_justified as _font_justified,
)
from .design_lint_parts._suppression import (
    _justified_keys as _justified_keys,
)
from .design_lint_parts._suppression import (
    _justified_palette_rgbs as _justified_palette_rgbs,
)
from .design_lint_parts._suppression import (
    _normalize_choice as _normalize_choice,
)

# ---- the tool wrapper ---------------------------------------------------------


class DesignLintArgs(BaseModel):
    root: str = Field(
        default=".",
        description="Workspace-relative directory to scan (defaults to the workspace root).",
    )


class DesignLintTool:
    """[CONTRACT boundary] READ-ONLY design-slop scanner. Walks the workspace's
    CSS + HTML/JSX and `.disco/designspec.json` and returns a structured verdict
    of ranked findings. Observes only — makes no change — so it is safe for the
    planner and counts as a verification probe (not productive work)."""

    definition = ToolDef(
        name="design_lint",
        description=(
            "Scan the generated web app for design SLOP (the LLM-median tells: Inter/Geist "
            "fonts, AI-purple primary, gradient-clipped hero text, dark-neon-glow, pill-button "
            "monoculture, emoji-as-icons, animation soup, centered-hero/3-cards/CTA) and return "
            "a STRUCTURED verdict. For static sites, also checks banned default first fonts on "
            "body/headings, reflexive hover scale, hover-only interactivity without focus-visible, "
            "text over imagery without a scrim, and glass sanity. For HTML deck artifacts, also "
            "checks the slide type floor, text budget, bullet-list monotony, missing imagery, and "
            "glass sanity. Findings are ranked rows with rule_id, severity, path, line, evidence, "
            "choice_key, and a fix message. Web off-default values are suppressed only when "
            ".disco/designspec.json carries a substantive justification for the choice_key. "
            "Read-only heuristic advice, not semantic proof or completion authority. One use "
            "spends one of the TWO debug calls shared with verify_web_app and "
            "verify_appkit_app for the current artifact bytes; only a real successful "
            "artifact mutation resets that allowance. Scans the workspace root unless "
            "`root` is given."
        ),
        args_model=DesignLintArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner + a non-productive probe
        behavior=declares(EffectCapability.ARTIFACT_VERIFY, planner_safe=True),
    )

    async def run(self, args: DesignLintArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            root = (args.root or ".").strip() or "."
            files = await self._collect_files(ctx, root)
            design_spec, spec_present, spec_valid = await self._load_design_spec(ctx)
            direction = await self._load_direction(ctx)
            verdict = lint_design(
                files,
                design_spec,
                spec_present=spec_present,
                spec_valid=spec_valid,
                direction=direction,
            )
            return ToolOutcome(
                success=True,  # the scan ran; pass/findings live in `structured`
                content=_render(verdict),
                structured=verdict,
            )
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a scan error
            return fail_outcome(f"design_lint error: {e}")

    async def _load_direction(self, ctx: ToolContext) -> DesignDirection | None:
        """Compatibility seam over the shared committed-direction loader."""

        return await load_committed_direction(ctx)

    async def _collect_files(self, ctx: ToolContext, root: str) -> dict[str, str]:
        """Walk the workspace via the sandbox `list_dir`, reading only scannable
        files (bounded by file/depth/size caps). Directories are distinguished
        without relying on backend-specific error types: an entry whose name has
        a non-scannable extension is treated as a leaf file and skipped; an
        extension-less entry is probed as a directory (a failed `list_dir` →
        it was a leaf, skip it)."""
        assert ctx.sandbox is not None
        sandbox = ctx.sandbox
        files: dict[str, str] = {}

        async def walk(rel: str, depth: int) -> None:
            if depth > _MAX_DEPTH or len(files) >= _MAX_FILES:
                return
            try:
                entries = await sandbox.list_dir(rel)
            except Exception:  # noqa: BLE001 — unreadable/non-dir → nothing to scan here
                return
            for name in sorted(entries):
                if len(files) >= _MAX_FILES:
                    return
                # Skip dep/build dirs and ALL dotfiles/dotdirs (incl. `.disco`,
                # which is loaded separately as the spec — never scanned for slop).
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                child = name if rel in (".", "") else f"{rel.rstrip('/')}/{name}"
                ext = _ext_of(name)
                if ext in SCAN_EXTS:
                    # Size-check IN the sandbox before reading: an over-cap (or
                    # unmeasurable) source file is skipped without ever pulling its
                    # bytes into this process — no read-then-discard memory bomb.
                    data = await _bounded_read(sandbox, child, _MAX_FILE_BYTES)
                    if data is None:
                        continue
                    files[child] = data.decode("utf-8", errors="replace")
                elif "." not in name:
                    await walk(child, depth + 1)

        await walk(root, 0)
        return files

    async def _load_design_spec(self, ctx: ToolContext) -> tuple[DesignSpec | None, bool, bool]:
        """Read + parse `.disco/designspec.json`. Returns (spec, present, valid).
        Absent → (None, False, False); present-but-bad → (None, True, False)."""
        assert ctx.sandbox is not None
        path = ".disco/designspec.json"
        try:
            exists = await ctx.sandbox.file_exists(path)
        except Exception:  # noqa: BLE001 — treat a probe failure as absent
            exists = False
        if not exists:
            return None, False, False
        # Size-check IN the sandbox BEFORE read: an over-cap (or unmeasurable)
        # `.disco/designspec.json` is never pulled into this process — `_bounded_read`
        # returns None, which we treat as present-but-invalid (rules fire, nothing
        # suppressed). A multi-GB hostile spec is therefore not a memory/time bomb.
        data = await _bounded_read(ctx.sandbox, path, _MAX_SPEC_BYTES)
        if data is None:
            return None, True, False
        try:
            # Defense-in-depth: the Epic C airtight loader re-enforces the same
            # `MAX_DESIGNSPEC_BYTES` cap before json.loads and validates the schema.
            spec = load_design_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — present but malformed/invalid
            return None, True, False
        return spec, True, True


__all__ = [
    "SUPPRESSIBLE_CHOICE_KEYS",
    "DesignFinding",
    "DesignLintArgs",
    "DesignLintTool",
    "lint_design",
]
