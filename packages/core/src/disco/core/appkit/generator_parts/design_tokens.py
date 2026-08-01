"""Section-variant resolution + DesignSpec → CSS design tokens.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

from ..section_catalog import get_variant, variants_for
from ..spec import DesignSpec, Section
from .ids import _css_font_name, _font_url_family

# ---- variant resolution -------------------------------------------------------


def _variant_layout(section: Section) -> str:
    """The layout slug to render this section with. If `variant_id` is set it MUST
    be a catalog variant OF this section's kind (referential integrity the tools
    also enforce on patch). Absent → the first catalog variant for the kind
    (deterministic); `custom`/unknown kinds → 'generic'."""
    if section.variant_id is not None:
        variant = get_variant(section.variant_id)
        if variant is None:
            raise ValueError(
                f"section {section.id!r} references unknown variant_id {section.variant_id!r}"
            )
        if variant.kind != section.kind:
            raise ValueError(
                f"section {section.id!r} variant {section.variant_id!r} is a "
                f"{variant.kind!r} layout, not {section.kind!r}"
            )
        return variant.layout
    options = variants_for(section.kind)
    return options[0].layout if options else "generic"


# ---- design tokens (DesignSpec → CSS) -----------------------------------------

# component_style → a non-pill button/border radius. NEVER a fully-rounded pill
# (that trips design_lint's pill-monoculture rule); each is a small, deliberate
# value so the buttons read as a designed component, not a framework default.
_RADIUS_BY_STYLE: dict[str, str] = {
    "underlined-flat": "0px",
    "outlined": "2px",
    "hairline": "0px",
    "soft-bordered": "8px",
    "soft-shadow": "10px",
    "flat": "4px",
    "soft": "8px",
}
_DEFAULT_RADIUS = "6px"

# density → a base spacing rhythm.
_SPACE_BY_DENSITY: dict[str, str] = {
    "compact": "0.75rem",
    "comfortable": "1rem",
    "airy": "1.5rem",
}
_DEFAULT_SPACE = "1rem"


def _font_stack(family: str, *, serifish: bool) -> str:
    """`"Family", <generic fallback>` — the real declared family FIRST (so it is
    the chosen primary design_lint reads) and a bare generic-CSS fallback after
    (a fallback family is never flagged)."""
    fallback = "serif" if serifish else "sans-serif"
    # Quote the family and strip it to the safe font charset (belt-and-suspenders
    # over `_FONT_RE`): a hostile family can never carry a `"`/`;`/`}` that breaks
    # out of the quoted CSS value into a new declaration or rule.
    return f'"{_css_font_name(family)}", {fallback}'


def _looks_serif(family: str) -> bool:
    low = family.lower()
    serif_tokens = (
        "serif",
        "garamond",
        "fraunces",
        "newsreader",
        "lora",
        "domine",
        "playfair",
        "cormorant",
        "georgia",
        "times",
        "merriweather",
    )
    if "sans" in low:
        return False
    return any(tok in low for tok in serif_tokens)


def _design_tokens(design: DesignSpec) -> dict[str, str]:
    pal = design.palette
    typ = design.typography
    tokens = {
        "--font-heading": _font_stack(typ.heading_font, serifish=_looks_serif(typ.heading_font)),
        "--font-body": _font_stack(typ.body_font, serifish=_looks_serif(typ.body_font)),
        "--color-primary": pal.primary,
        "--color-surface": pal.surface,
        "--color-text": pal.text,
        "--radius": _RADIUS_BY_STYLE.get(design.component_style, _DEFAULT_RADIUS),
        "--space": _SPACE_BY_DENSITY.get(design.density, _DEFAULT_SPACE),
    }
    if pal.accent is not None:
        tokens["--color-accent"] = pal.accent
    else:
        # Fall back to the primary so `var(--color-accent)` always resolves; not a
        # neon/violet, so still lint-clean.
        tokens["--color-accent"] = pal.primary
    return tokens


def _google_fonts_href(design: DesignSpec) -> str:
    """A deterministic Google Fonts URL declaring the two real families. Uses the
    `family=` form design_lint reads; both families are real (recipe fonts), so
    this is clean."""
    fams = []
    for fam in (design.typography.heading_font, design.typography.body_font):
        enc = _font_url_family(fam)
        if enc not in fams:
            fams.append(enc)
    query = "&".join(f"family={f}:wght@400;600;700" for f in fams)
    return f"https://fonts.googleapis.com/css2?{query}&display=swap"


# ---- the per-file emitters ----------------------------------------------------


def _emit_styles_css(design: DesignSpec) -> str:
    tokens = _design_tokens(design)
    token_lines = "\n".join(f"  {k}: {v};" for k, v in tokens.items())
    # NOTE on lint-cleanliness: real fonts (primary family), palette colors (no
    # AI-purple), exactly two transitions (well under the motion-soup threshold),
    # non-pill radius, no gradient-on-heading, no neon/glow, no `grid-cols-3` /
    # `repeat(3,` and no `.card` selector. The features grid is auto-fit.
    return (
        "/* Design tokens lowered from .disco/designspec.json (Epic E). */\n"
        ":root {\n" + token_lines + "\n}\n"
        "\n"
        "* { box-sizing: border-box; }\n"
        "html, body { margin: 0; padding: 0; }\n"
        "body {\n"
        "  font-family: var(--font-body);\n"
        "  color: var(--color-text);\n"
        "  background: var(--color-surface);\n"
        "  line-height: 1.6;\n"
        "}\n"
        "h1, h2, h3 { font-family: var(--font-heading); line-height: 1.15; }\n"
        ".app-main { max-width: 1080px; margin: 0 auto; padding: 0 var(--space); }\n"
        ".section { padding: calc(var(--space) * 3) 0; }\n"
        ".section .eyebrow {\n"
        "  text-transform: uppercase; letter-spacing: 0.08em;\n"
        "  font-size: 0.8rem; color: var(--color-accent);\n"
        "}\n"
        ".section h2 { font-size: 2rem; margin: 0 0 var(--space); color: var(--color-primary); }\n"
        ".section p { max-width: 60ch; }\n"
        ".feature-grid {\n"
        "  display: grid; gap: var(--space);\n"
        "  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));\n"
        "}\n"
        ".feature-item {\n"
        "  border: 1px solid color-mix(in srgb, var(--color-text) 14%, transparent);\n"
        "  border-radius: var(--radius); padding: var(--space);\n"
        "}\n"
        ".stacked-list { list-style: none; margin: 0; padding: 0; display: grid;"
        " gap: var(--space); }\n"
        ".stacked-list li { padding: var(--space) 0;"
        " border-bottom: 1px solid color-mix(in srgb, var(--color-text) 12%, transparent); }\n"
        ".btn {\n"
        "  display: inline-block; border: 1px solid var(--color-primary);\n"
        "  background: var(--color-primary); color: var(--color-surface);\n"
        "  border-radius: var(--radius); padding: 0.6rem 1.1rem;\n"
        "  text-decoration: none; font-weight: 600; cursor: pointer;\n"
        "  transition: opacity 120ms ease;\n"
        "}\n"
        ".btn:hover { opacity: 0.9; }\n"
        ".btn:disabled { cursor: not-allowed; opacity: 0.62; }\n"
        ".btn:focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }\n"
        ".btn-secondary {\n"
        "  background: transparent; color: var(--color-primary);\n"
        "  border-radius: var(--radius);\n"
        "}\n"
        ".hero { padding: calc(var(--space) * 4) 0; }\n"
        ".hero.variant-asymmetric-editorial { text-align: left; }\n"
        ".hero h1 { font-size: 3rem; margin: 0 0 var(--space); color: var(--color-primary); }\n"
        ".lead-form { display: grid; gap: var(--space); max-width: 32rem; }\n"
        ".lead-form label { display: grid; gap: 0.35rem; font-weight: 600; }\n"
        ".lead-form input, .lead-form textarea {\n"
        "  font: inherit; padding: 0.6rem; border-radius: var(--radius);\n"
        "  border: 1px solid color-mix(in srgb, var(--color-text) 30%, transparent);\n"
        "  background: var(--color-surface); color: var(--color-text);\n"
        "  transition: border-color 120ms ease;\n"
        "}\n"
        ".lead-form input:focus, .lead-form textarea:focus"
        " { border-color: var(--color-primary); }\n"
        ".form-status { font-size: 0.9rem; color: var(--color-accent); }\n"
        ".form-status-success {\n"
        "  border-left: 3px solid var(--color-accent); padding-left: 0.75rem;\n"
        "}\n"
        ".form-status-error, .field-error { color: var(--color-primary); }\n"
        ".field-error { font-size: 0.85rem; font-weight: 600; }\n"
        ".form-feedback { display: grid; gap: 0.75rem; }\n"
        ".recent-submissions {\n"
        "  border-top: 1px solid color-mix(in srgb, var(--color-text) 14%, transparent);\n"
        "  padding-top: 0.75rem;\n"
        "}\n"
        ".recent-submissions h3 { margin: 0 0 0.5rem; font-size: 1rem; }\n"
        ".recent-submissions ul { list-style: none; margin: 0; padding: 0; "
        "display: grid; gap: 0.5rem; }\n"
        ".recent-submissions li {\n"
        "  border: 1px solid color-mix(in srgb, var(--color-text) 12%, transparent);\n"
        "  border-radius: var(--radius); padding: 0.65rem;\n"
        "}\n"
        ".recent-submission-fields { display: flex; flex-wrap: wrap; gap: 0.4rem 0.8rem; }\n"
        ".recent-submission-field { font-size: 0.9rem; }\n"
        ".site-footer {\n"
        "  padding: calc(var(--space) * 2) 0;\n"
        "  border-top: 1px solid color-mix(in srgb, var(--color-text) 14%, transparent);\n"
        "  color: color-mix(in srgb, var(--color-text) 70%, transparent);\n"
        "}\n"
    )
