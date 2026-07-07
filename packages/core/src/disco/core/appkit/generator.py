"""AppKit EPIC E1 — the PURE lead-gen Cloudflare generator.

`generate(app_spec, design_spec)` is a DETERMINISTIC, side-effect-free function
from two specs to a `{path: contents}` file tree: same specs in → byte-identical
sorted tree out. It emits a complete lead-gen app:

* a React/Vite SPA — `index.html`, `src/main.tsx`, `src/App.tsx`, ONE component
  per `Section` (driven by `Section.kind` + the Epic D `variant_id` layout),
  `src/styles.css` (DESIGN TOKENS lowered from the `DesignSpec`: real fonts, the
  palette as CSS custom properties, layout/density), `src/generated/content.ts`
  (the section copy from `Section.content`, so content is regenerable FROM the
  spec), and a `src/generated/manifest.ts` digest;
* a Cloudflare Worker — `worker/index.ts` (POST /api/leads → validate JSON +
  insert into D1; GET /api/leads + /admin read-back; static-asset serving),
  `schema.sql` (the ONE lead entity → a D1 table), and `wrangler.toml` (Workers
  Static Assets SPA + a `[[d1_databases]]` binding).

The output is design_lint-CLEAN by construction: it uses the DesignSpec's real
fonts (never Inter/Geist), the DesignSpec palette (no AI-purple), solid type (no
gradient-clipped headings), restrained motion, non-pill button radii, no emoji,
and a non-"centered-hero / 3-cards / CTA" composition — so
`lint_design(generate(...), design_spec)` returns ZERO findings for any recipe.

PURITY / LAYERING: data → data, no IO, no LLM, no clock/random. `disco.core` is
the leaf package (.importlinter); this imports ONLY the stdlib + the sibling
core modules (`.spec`, `.section_catalog`). The TOOL layer (Epic E2/E3) is what
writes the tree + the `.disco/` specs into the sandbox and runs the lint gate.
"""

from __future__ import annotations

import hashlib
import html
import importlib
import json
import re
from urllib.parse import quote

from . import semantic_metadata as _md
from .primitives import (
    DIRECTORY_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    PrimitiveDefinition,
    register_primitive,
    resolve_primitive,
)
from .recipes import SiteRecipe
from .section_catalog import get_variant, variants_for
from .spec import (
    Action,
    AppSpec,
    DesignSpec,
    Entity,
    EntityField,
    Page,
    Section,
    SectionContent,
)

# ---- lead entity --------------------------------------------------------------

# The synthesized default lead entity when the AppSpec declares none — the P0
# minimum a lead-gen app needs to capture a contact.
_DEFAULT_LEAD_ID = "lead"
_DEFAULT_LEAD_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("name", "str", True),
    ("email", "str", True),
    ("message", "text", False),
)


def synthesized_lead_entity() -> Entity:
    """The sensible default lead entity (name/email/message) used when the AppSpec
    declares none. Deterministic — a fixed shape so the generated tree is stable."""
    return Entity(
        id=_DEFAULT_LEAD_ID,
        name="Lead",
        fields=tuple(EntityField(name=n, type=t, required=r) for n, t, r in _DEFAULT_LEAD_FIELDS),
    )


def _form_target_ids(app_spec: AppSpec) -> frozenset[str]:
    """Entity ids targeted by a `form` section's `content_ref` — the F3.1 form
    primitive's fold marker. Such entities are FORM-SUBMISSION planes, never lead
    candidates: without this skip, a folded form's `submit` action could flip
    `resolve_lead_entity` onto the form entity and silently re-shape the whole
    lead data plane. Empty for every pre-F3.1 spec (no form section sets
    `content_ref`), so lead resolution there is byte-for-byte unchanged."""
    return frozenset(
        section.content_ref
        for page in app_spec.pages
        for section in page.sections
        if section.kind == "form" and section.content_ref is not None
    )


def ensure_lead_entity(app_spec: AppSpec) -> AppSpec:
    """Return an AppSpec GUARANTEED to declare the lead entity it persists.

    If the spec already resolves a lead entity (a non-form `submit` target or an
    entity id `lead`) it is returned unchanged; otherwise the synthesized
    name/email/message entity is appended and the whole spec is re-validated.
    `app_create` calls this so the on-disk `appspec.json` always contains the
    entity the generated schema.sql / worker target — spec ⇄ tree never disagree
    about the lead shape. Form-entity submit targets (`_form_target_ids`) never
    satisfy the lead requirement."""
    by_id = {e.id: e for e in app_spec.entities}
    form_targets = _form_target_ids(app_spec)
    submit_targets = [a.target.strip() for a in app_spec.primary_actions if a.type == "submit"]
    if (
        any(t in by_id and t not in form_targets for t in submit_targets)
        or _DEFAULT_LEAD_ID in by_id
    ):
        return app_spec
    data = app_spec.model_dump(mode="json")
    data["entities"] = [*data["entities"], synthesized_lead_entity().model_dump(mode="json")]
    return AppSpec.model_validate(data)


def resolve_lead_entity(app_spec: AppSpec) -> Entity:
    """The ONE lead entity the app persists. P0 derivation, in order:

    1. the entity targeted by a `submit` primary action — SKIPPING form-submission
       entities (`_form_target_ids`), which own their own POST plane;
    2. an entity whose id is `lead`;
    3. otherwise the synthesized name/email/message default.

    Pure + deterministic — no spec mutation; `app_create` is what writes a
    synthesized entity back into the persisted AppSpec so spec ⇄ tree stay in sync."""
    submit_targets = [a.target.strip() for a in app_spec.primary_actions if a.type == "submit"]
    by_id = {e.id: e for e in app_spec.entities}
    form_targets = _form_target_ids(app_spec)
    for target in submit_targets:
        if target in form_targets:
            continue
        if target in by_id:
            return by_id[target]
    if _DEFAULT_LEAD_ID in by_id:
        return by_id[_DEFAULT_LEAD_ID]
    return synthesized_lead_entity()


# ---- identifiers / slugs ------------------------------------------------------


def _pascal(raw: str) -> str:
    """A stable PascalCase identifier from an arbitrary id. Non-alphanumerics are
    word separators; a leading digit is prefixed so the result is a valid JS/TS
    identifier. Empty → 'X'."""
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", raw) if p]
    ident = "".join(p[:1].upper() + p[1:] for p in parts)
    if not ident:
        ident = "X"
    if ident[0].isdigit():
        ident = "X" + ident
    return ident


def _slug(raw: str) -> str:
    """A deterministic kebab slug (lowercase alphanumerics joined by '-')."""
    parts = [p for p in re.split(r"[^0-9a-z]+", raw.lower()) if p]
    return "-".join(parts) or "app"


#: Length of the namespace token appended to Cloudflare resource names (8 hex chars
#: = 32 bits, ample to make slug-collisions between distinct apps astronomically
#: unlikely on one account). Kept short so the namespaced name stays well inside the
#: SEC-9 63-char Cloudflare bound.
_NS_LEN = 8
#: SEC-9 (appkit_cloudflare) Cloudflare resource-name bound: a leading alphanumeric
#: then letters/digits/'-'/'_', at most 63 chars total. The generator emits names
#: that are valid BY CONSTRUCTION so a perfectly valid AppSpec never trips the deploy
#: gate's name refusal.
_CF_NAME_MAXLEN = 63


def _namespace(app: AppSpec) -> str:
    """A short, stable namespace token that disambiguates THIS app's Cloudflare
    resource names from any OTHER app's on the same account (audit SEC-10
    defense-in-depth).

    Without a namespace the Worker / D1 names are pure slugs of `app.name`, so two
    DIFFERENT apps whose names SLUG-COLLIDE — "My Site!" / "My Site" / "my  site"
    all normalize to ``my-site`` — would map to the SAME Cloudflare resource names
    and clobber one another on one account. The token is the first `_NS_LEN` hex
    digits of a SHA-256 over the app's STABLE IDENTITY — its raw (pre-slug) ``name``
    and ``app_kind`` — canonicalized as sorted-key JSON (the same canonicalization
    `snapshot.spec_digest` uses).

    Identity, NOT content: the token is derived ONLY from the fields that define
    *which app this is*, never from the mutable page/section/entity CONTENT. So it
    is INVARIANT across content edits, section add/remove, and design re-themes — an
    app's live Worker/D1 must keep their names across every such edit (a content
    tweak must never orphan a deployed resource by renaming it). Two raw names that
    slug-collide differ here (distinct → distinct names); the same app always
    regenerates the same names (idempotent deploys / safe re-runs). Two genuinely
    distinct apps that share an identical raw name + kind remain indistinguishable
    at this pure layer — that residual case is backstopped by the deploy gate's
    SEC-10 ``unrelated_resource_adopt_refused`` ownership check."""
    canonical = json.dumps(
        {"name": app.name, "app_kind": app.app_kind},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_NS_LEN]


def _namespaced(base: str, ns: str) -> str:
    """Append the stable namespace token to a slug base, keeping the result a valid
    Cloudflare resource name (SEC-9: leading alphanumeric, then [a-z0-9_-], ≤63
    chars). The base is truncated (and any dangling separator trimmed) so the
    namespace suffix ALWAYS survives — distinctness can never be lost to truncation."""
    suffix = f"-{ns}"
    head = base[: _CF_NAME_MAXLEN - len(suffix)].rstrip("-_") or "app"
    return f"{head}{suffix}"


def _component_names(app: AppSpec) -> dict[tuple[str, str], str]:
    """Deterministic, COLLISION-FREE map (page.id, section.id) → component name.

    `_pascal` is NOT injective: distinct-but-valid page/section ids that normalize to
    the SAME PascalCase (`about-us` / `about_us` / `about us` → `AboutUs`, or section
    titles differing only by punctuation) would otherwise emit two components/files
    with the same name → a duplicate declaration / overwritten file → invalid TSX from
    a perfectly VALID AppSpec (the spec only guarantees the RAW page/section ids are
    unique, not their normalized forms). So we build the name map ONCE, in document
    order, and DISAMBIGUATE any collision by inserting the smallest unused integer
    before the `Section` suffix (`AboutUsSection`, then `AboutUs2Section`, …). Document
    order is fixed for a given AppSpec, so the result is deterministic (same spec →
    byte-identical tree) and every component name (hence every file path) is unique.
    Every reference site (App.tsx imports, the file path, content.ts keys, the
    manifest) reads this ONE map, so the refs can never drift."""
    used: set[str] = set()
    mapping: dict[tuple[str, str], str] = {}
    for page, section in _iter_sections(app):
        stem = f"{_pascal(page.id)}{_pascal(section.id)}"
        name = f"{stem}Section"
        suffix = 2
        while name in used:
            name = f"{stem}{suffix}Section"
            suffix += 1
        used.add(name)
        mapping[(page.id, section.id)] = name
    return mapping


def _comp_name(names: dict[tuple[str, str], str], page: Page, section: Section) -> str:
    """The unique component name for a section, read from the prebuilt collision-free
    `_component_names` map (built once per `generate`)."""
    return names[(page.id, section.id)]


def _ts(value: object) -> str:
    """A JSON literal — valid TS — for safe interpolation into generated TS. A JSON
    string literal escapes `"`, backslash and control chars, so an arbitrary spec
    string (a `</script>`, a backtick, a quote, a newline) lands as an inert TS
    string and can never break out of the literal into executable code."""
    return json.dumps(value, ensure_ascii=False)


def _html_text(value: str) -> str:
    """Escape a spec string for HTML TEXT context (`<title>`, headings baked into
    server-emitted/static HTML). `&<>"'` → entities, so a hostile `AppSpec.name`
    like `</title><script>…` becomes inert text, never executable markup."""
    return html.escape(value, quote=True)


def _css_font_name(family: str) -> str:
    """A CSS-string-safe font family: keep only the safe font charset
    (letters/digits/spaces — what `_FONT_RE` in the spec already guarantees) and
    drop anything else, so the family can never carry a `"`/`;`/`}` that breaks out
    of the quoted `font-family` value. Belt-and-suspenders over the spec validator;
    collapses to nothing only for an (impossible, schema-rejected) all-symbol name."""
    return re.sub(r"[^A-Za-z0-9 ]", "", family).strip()


def _font_url_family(family: str) -> str:
    """URL-encode a font family for the Google Fonts `family=` query. Spaces → `+`
    (the css2 convention) and EVERY other non-safe byte percent-encoded, so a
    hostile family can't break out of the `href="…"` attribute or inject another
    URL param/tag. `_FONT_RE` already constrains the family; this is the
    context-correct encoder regardless (so the generator is safe on its own)."""
    return quote(family, safe="").replace("%20", "+")


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


# ---- AppKit EPIC J: semantic edit metadata ------------------------------------
#
# Additive `data-disco-*` attributes that let a UI click on a rendered element map
# back to the AppSpec slot it came from, so the host can steer an `app_update_content`
# patch (the in-frame selection agent reads these; see selection_agent.js / appResolver).
# They never change layout/content and are design_lint-inert (the linter scans for
# slop tells, not data-attributes). The section root carries file/section/screen-label;
# a content-bearing element carries the AppSpec slot name (`data-disco-field`), and an
# item row carries its collection id + 0-based index + item kind.
#
# The file is ALWAYS `.disco/appspec.json` (where `app_update_content` mutates), and
# the field names are AppSpec slot names — `cta_label` (NOT the `ctaLabel` content.ts
# key), `heading`, `subheading`, `body`, `items`, `success_message` — exactly the
# keys the tool accepts.

_DISCO_SPEC_FILE = ".disco/appspec.json"


def _disco_attr(a: _md.DataDiscoAttr, value: object) -> str:
    """A JSX-safe static `data-disco-*` attribute fragment in canonical attr order."""
    return f" {a.value}={_ts(value)}"


def _disco_expr_attr(a: _md.DataDiscoAttr, expr: str) -> str:
    """A JSX expression-valued `data-disco-*` attribute fragment."""
    return f" {a.value}={{{expr}}}"


def _disco_field_attr(field: str) -> str:
    return _disco_attr(_md.DataDiscoAttr.FIELD, field)


def _disco_section_attrs(section: Section) -> str:
    """The semantic-edit attrs stamped on a section root through the canonical
    vocabulary: the spec file, section id, and stable screen label."""
    return (
        _disco_attr(_md.DataDiscoAttr.FILE, _DISCO_SPEC_FILE)
        + _disco_attr(_md.DataDiscoAttr.SECTION, section.id)
        + _disco_attr(
            _md.DataDiscoAttr.SCREEN_LABEL,
            _md.screen_label_value(section.id),
        )
    )


def _disco_item_attrs(section: Section, *, index_expr: str, item_kind: str) -> str:
    """Semantic metadata for one generated `content.items` row."""
    return (
        _disco_attr(_md.DataDiscoAttr.COLLECTION, f"{section.id}.items")
        + _disco_expr_attr(_md.DataDiscoAttr.INDEX, index_expr)
        + _disco_attr(_md.DataDiscoAttr.ITEM_KIND, item_kind)
    )


def _render_body(comp_id: str) -> str:
    """Generic content body shared by non-form sections: renders ONLY the slots
    that are set at runtime, reading from content.ts by id (so updating content
    never requires regenerating the component). Each content-bearing element carries
    a `data-disco-field` slot tag (Epic J) so a UI click maps back to the spec slot."""
    return (
        '      {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"      {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f'      {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        f"      {{c.body ? <p{_disco_field_attr('body')}>{{c.body}}</p> : null}}\n"
    )


def _emit_component(
    comp: str, page: Page, section: Section, lead: Entity, post_path: str = "/api/leads"
) -> str:
    comp_id = comp
    layout = _variant_layout(section)
    kind = section.kind
    classes = f"section kind-{kind} variant-{layout}"

    if kind == "form":
        return _emit_form_component(comp, comp_id, page, section, lead, classes, post_path)

    disco_attrs = _disco_section_attrs(section)

    header = (
        "/* Auto-generated section component — do NOT hand-edit; regenerated from "
        ".disco/appspec.json. */\n"
        'import { CONTENT } from "../generated/content";\n\n'
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp_id)}] ?? {{}};\n"
    )

    if kind == "hero":
        body = (
            f"  return (\n"
            f"    <section className={_ts('hero ' + classes)} id={_ts(section.id)}"
            f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
            f'      <div className="app-main">\n'
            '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
            f"        {{c.heading ? <h1{_disco_field_attr('heading')}>{{c.heading}}</h1> : null}}\n"
            f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
            "{c.subheading}</p> : null}\n"
            f"        {{c.body ? <p{_disco_field_attr('body')}>{{c.body}}</p> : null}}\n"
            "        {c.ctaLabel ? (\n"
            f'          <p><a className="btn" href="#lead-form"{_disco_field_attr("cta_label")}>'
            "{c.ctaLabel}</a></p>\n"
            "        ) : null}\n"
            "      </div>\n"
            "    </section>\n"
            "  );\n}\n"
        )
        return header + body

    if kind in ("features", "list", "gallery", "pricing", "testimonials", "faq"):
        list_cls = "feature-grid" if kind in ("features", "gallery", "pricing") else "stacked-list"
        wrap_open = "<ul" if list_cls == "stacked-list" else "<div"
        wrap_close = "</ul>" if list_cls == "stacked-list" else "</div>"
        item_tag = "li" if list_cls == "stacked-list" else "div"
        body = (
            f"  return (\n"
            f"    <section className={_ts(classes)} id={_ts(section.id)}"
            f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
            f'      <div className="app-main">\n'
            + "  "
            + _render_body(comp_id)
            + f"        {wrap_open} className={_ts(list_cls)}>\n"
            "          {(c.items ?? []).map((item, i) => (\n"
            f'            <{item_tag} className="feature-item" key={{i}}'
            f"{_disco_field_attr('items')}"
            f"{_disco_item_attrs(section, index_expr='i', item_kind='item')}>"
            f"{{item}}</{item_tag}>\n"
            "          ))}\n"
            f"        {wrap_close}\n"
            "      </div>\n"
            "    </section>\n"
            "  );\n}\n"
        )
        return header + body

    if kind == "cta":
        body = (
            f"  return (\n"
            f"    <section className={_ts(classes)} id={_ts(section.id)}"
            f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
            f'      <div className="app-main">\n'
            + "  "
            + _render_body(comp_id)
            + "        {c.ctaLabel ? (\n"
            f'          <p><a className="btn" href="#lead-form"{_disco_field_attr("cta_label")}>'
            "{c.ctaLabel}</a></p>\n"
            "        ) : null}\n"
            "      </div>\n"
            "    </section>\n"
            "  );\n}\n"
        )
        return header + body

    if kind == "footer":
        body = (
            f"  return (\n"
            f"    <footer className={_ts('site-footer ' + classes)} id={_ts(section.id)}"
            f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
            f'      <div className="app-main">\n'
            f"        {{c.heading ? <p{_disco_field_attr('heading')}>{{c.heading}}</p> : null}}\n"
            f"        {{c.body ? <p{_disco_field_attr('body')}>{{c.body}}</p> : null}}\n"
            "      </div>\n"
            "    </footer>\n"
            "  );\n}\n"
        )
        return header + body

    # custom / table / anything else: a generic block. (Epic J: the generic branch
    # now also carries the data-appkit-section marker — previously MISSING here — so
    # verify_appkit_app section-coverage + click-to-edit work for custom sections too.)
    body = (
        f"  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n' + "  " + _render_body(comp_id) + "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )
    return header + body


def _input_for(field: EntityField) -> str:
    """A labeled input for one lead field. `text`/`message` → textarea; `email` name
    → email input; everything else → a text input. Required maps to `required`."""
    label = field.name.replace("_", " ").title()
    is_textarea = field.type.lower() in ("text", "message") or field.name.lower() == "message"
    input_type = "email" if field.name.lower() == "email" else "text"
    key = _ts(field.name)
    field_id = f"lead-field-{field.name.replace('_', '-')}"
    error_id = f"{field_id}-error"
    required = " required" if field.required else ""
    described_by = (
        f" aria-describedby={{fieldErrors[{key}] ? {_ts(error_id)} : undefined}}"
        if field.required
        else ""
    )
    invalid = f' aria-invalid={{fieldErrors[{key}] ? "true" : undefined}}'
    if is_textarea:
        control = (
            f"          <textarea id={_ts(field_id)} name={_ts(field.name)}"
            f' value={{form[{key}] ?? ""}}{required}{invalid}{described_by}\n'
            f"            onChange={{(e) => updateField({key}, e.target.value)}} />"
        )
    else:
        control = (
            f"          <input id={_ts(field_id)} type={_ts(input_type)}"
            f' name={_ts(field.name)} value={{form[{key}] ?? ""}}{required}'
            f"{invalid}{described_by}\n"
            f"            onChange={{(e) => updateField({key}, e.target.value)}} />"
        )
    error = (
        f"\n"
        f"          {{fieldErrors[{key}] ? (\n"
        f'            <span className="field-error" id={_ts(error_id)}>'
        f"{{fieldErrors[{key}]}}</span>\n"
        "          ) : null}"
        if field.required
        else ""
    )
    return f"        <label htmlFor={_ts(field_id)}>{label}\n{control}{error}\n        </label>"


def _emit_api_client_ts() -> str:
    return (
        "export type ApiResult<T> =\n"
        "  | { ok: true; data: T }\n"
        "  | { ok: false; status: number; error: string };\n\n"
        "function errorFromBody(body: unknown): string | null {\n"
        '  if (typeof body !== "object" || body === null || !("error" in body)) {\n'
        "    return null;\n"
        "  }\n"
        "  const value = (body as { error: unknown }).error;\n"
        '  return typeof value === "string" && value.trim() ? value : null;\n'
        "}\n\n"
        "async function readError(response: Response): Promise<string> {\n"
        "  try {\n"
        "    const body: unknown = await response.json();\n"
        '    return (errorFromBody(body) ?? response.statusText) || "Request failed";\n'
        "  } catch {\n"
        '    return response.statusText || "Request failed";\n'
        "  }\n"
        "}\n\n"
        "export async function postJson<T>(path: string, body: unknown): Promise<ApiResult<T>> {\n"
        "  try {\n"
        "    const response = await fetch(path, {\n"
        '      method: "POST",\n'
        '      headers: { "Content-Type": "application/json" },\n'
        "      body: JSON.stringify(body),\n"
        "    });\n"
        "    if (!response.ok) {\n"
        "      return { ok: false, status: response.status, error: await readError(response) };\n"
        "    }\n"
        "    const data = (await response.json()) as T;\n"
        "    return { ok: true, data };\n"
        "  } catch (error: unknown) {\n"
        "    return {\n"
        "      ok: false,\n"
        "      status: 0,\n"
        '      error: error instanceof Error ? error.message : "Network error",\n'
        "    };\n"
        "  }\n"
        "}\n"
    )


def _emit_submit_hook_ts() -> str:
    return (
        'import { useCallback, useRef, useState } from "react";\n'
        'import { postJson } from "../api/client";\n\n'
        "export type SubmitState =\n"
        '  | { kind: "idle" }\n'
        '  | { kind: "submitting" }\n'
        '  | { kind: "success" }\n'
        '  | { kind: "error"; message: string };\n\n'
        "export interface SubmittedEntry {\n"
        "  id: string;\n"
        "  values: Record<string, string>;\n"
        "}\n\n"
        "function entryId(): string {\n"
        "  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;\n"
        "}\n\n"
        "export function useSubmit(path: string) {\n"
        '  const [state, setState] = useState<SubmitState>({ kind: "idle" });\n'
        "  const [submitted, setSubmitted] = useState<SubmittedEntry[]>([]);\n"
        "  const inFlight = useRef(false);\n\n"
        "  const submit = useCallback(\n"
        "    async (values: Record<string, string>): Promise<boolean> => {\n"
        "      if (inFlight.current) return false;\n"
        "      inFlight.current = true;\n"
        '      setState({ kind: "submitting" });\n'
        "      const entry: SubmittedEntry = { id: entryId(), values: { ...values } };\n"
        "      setSubmitted((current) => [entry, ...current]);\n\n"
        "      const result = await postJson<{ ok: true }>(path, values);\n"
        "      inFlight.current = false;\n"
        "      if (result.ok) {\n"
        '        setState({ kind: "success" });\n'
        "        return true;\n"
        "      }\n"
        "      setSubmitted((current) => current.filter((item) => item.id !== entry.id));\n"
        '      setState({ kind: "error", message: result.error });\n'
        "      return false;\n"
        "    },\n"
        "    [path]\n"
        "  );\n\n"
        "  const reset = useCallback(() => {\n"
        '    setState({ kind: "idle" });\n'
        "  }, []);\n\n"
        "  return { state, submitted, submit, reset };\n"
        "}\n"
    )


def _emit_form_component(
    comp: str,
    comp_id: str,
    page: Page,
    section: Section,
    lead: Entity,
    classes: str,
    post_path: str,
) -> str:
    inputs = "\n".join(_input_for(f) for f in lead.fields)
    disco_attrs = _disco_section_attrs(section)
    required_fields = [f.name for f in lead.fields if f.required]
    labels = {f.name: f.name.replace("_", " ").title() for f in lead.fields}
    recent_fields = [f.name for f in lead.fields[:2]]
    success = section.content.success_message if section.content is not None else None
    if success is None:
        success_const = ""
        success_feedback = (
            '              <p className="form-status form-status-success">'
            "Thanks — we will be in touch.</p>\n"
        )
    else:
        success_const = f"const DEFAULT_SUCCESS_MESSAGE = {_ts(success)};\n\n"
        success_feedback = (
            '              <p className="form-status form-status-success"'
            f"{_disco_field_attr('success_message')}>"
            "{c.successMessage ?? DEFAULT_SUCCESS_MESSAGE}</p>\n"
        )
    return (
        "/* Auto-generated lead-capture component — do NOT hand-edit; regenerated from "
        ".disco/appspec.json. */\n"
        'import type { FormEvent } from "react";\n'
        'import { useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        'import { useSubmit } from "../hooks/useSubmit";\n\n'
        f"const REQUIRED_FIELDS = {_ts(required_fields)} as const;\n"
        f"const RECENT_FIELDS = {_ts(recent_fields)} as const;\n"
        f"const FIELD_LABELS: Record<string, string> = {_ts(labels)};\n\n"
        f"{success_const}"
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp_id)}] ?? {{}};\n"
        "  const [form, setForm] = useState<Record<string, string>>({});\n"
        "  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});\n"
        f"  const {{ state, submitted, submit, reset }} = useSubmit({_ts(post_path)});\n\n"
        "  function updateField(field: string, value: string) {\n"
        "    setForm((current) => ({ ...current, [field]: value }));\n"
        "    setFieldErrors((current) => {\n"
        "      if (!current[field]) return current;\n"
        "      const next = { ...current };\n"
        "      delete next[field];\n"
        "      return next;\n"
        "    });\n"
        '    if (state.kind === "success" || state.kind === "error") reset();\n'
        "  }\n\n"
        "  function validateRequired(): boolean {\n"
        "    const nextErrors: Record<string, string> = {};\n"
        "    for (const field of REQUIRED_FIELDS) {\n"
        '      if (!(form[field] ?? "").trim()) {\n'
        "        nextErrors[field] = `${FIELD_LABELS[field] ?? field} is required.`;\n"
        "      }\n"
        "    }\n"
        "    setFieldErrors(nextErrors);\n"
        "    return Object.keys(nextErrors).length === 0;\n"
        "  }\n\n"
        "  async function onSubmit(e: FormEvent<HTMLFormElement>) {\n"
        "    e.preventDefault();\n"
        '    if (state.kind === "submitting") return;\n'
        "    if (!validateRequired()) return;\n"
        "    const saved = await submit(form);\n"
        "    if (saved) setForm({});\n"
        "  }\n"
        f"  return (\n"
        f'    <section className={_ts(classes)} id="lead-form"'
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        f'      <div className="app-main">\n'
        '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"        {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        '        <form className="lead-form" onSubmit={onSubmit} noValidate>\n' + inputs + "\n"
        f'          <button className="btn" type="submit" disabled={{state.kind === "submitting"}}'
        f"{_disco_field_attr('cta_label')}>\n"
        '            {state.kind === "submitting" ? "Sending…" : c.ctaLabel ?? "Submit"}\n'
        "          </button>\n"
        '          <div className="form-feedback" aria-live="polite">\n'
        '            {state.kind === "success" ? (\n'
        f"{success_feedback}"
        "            ) : null}\n"
        '            {state.kind === "error" ? (\n'
        '              <p className="form-status form-status-error">{state.message}</p>\n'
        "            ) : null}\n"
        "            {submitted.length > 0 ? (\n"
        '              <div className="recent-submissions">\n'
        "                <h3>Recently submitted</h3>\n"
        "                <ul>\n"
        "                  {submitted.map((entry) => (\n"
        "                    <li key={entry.id}>\n"
        '                      <div className="recent-submission-fields">\n'
        "                        {RECENT_FIELDS.map((field) => (\n"
        '                          <span className="recent-submission-field" key={field}>\n'
        "                            <strong>{FIELD_LABELS[field] ?? field}:</strong> "
        '{entry.values[field] ?? ""}\n'
        "                          </span>\n"
        "                        ))}\n"
        "                      </div>\n"
        "                    </li>\n"
        "                  ))}\n"
        "                </ul>\n"
        "              </div>\n"
        "            ) : null}\n"
        "          </div>\n"
        "        </form>\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _iter_sections(app: AppSpec) -> list[tuple[Page, Section]]:
    return [(page, section) for page in app.pages for section in page.sections]


def _emit_content_ts(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    entries: list[str] = []
    has_success_message = any(
        section.content is not None and section.content.success_message is not None
        for _page, section in _iter_sections(app)
    )
    for page, section in _iter_sections(app):
        comp_id = _comp_name(names, page, section)
        content = section.content
        slot: dict[str, object] = {}
        if content is not None:
            if content.heading is not None:
                slot["heading"] = content.heading
            if content.subheading is not None:
                slot["subheading"] = content.subheading
            if content.body is not None:
                slot["body"] = content.body
            if content.cta_label is not None:
                slot["ctaLabel"] = content.cta_label
            if content.items:
                slot["items"] = list(content.items)
            if has_success_message and content.success_message is not None:
                slot["successMessage"] = content.success_message
        entries.append(f"  {_ts(comp_id)}: {_ts(slot)},")
    body = "\n".join(entries)
    success_message_field = "  successMessage?: string;\n" if has_success_message else ""
    return (
        "/* Auto-generated section content — regenerated from .disco/appspec.json. */\n"
        "export interface SectionContent {\n"
        "  eyebrow?: string;\n"
        "  heading?: string;\n"
        "  subheading?: string;\n"
        "  body?: string;\n"
        "  ctaLabel?: string;\n"
        "  items?: string[];\n"
        f"{success_message_field}"
        "}\n\n"
        "export const CONTENT: Record<string, SectionContent> = {\n" + body + "\n};\n"
    )


def _emit_app_tsx(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    imports: list[str] = []
    renders: list[str] = []
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        imports.append(f'import {comp} from "./components/{comp}";')
        renders.append(f"      <{comp} />")
    return (
        "/* Auto-generated app shell — regenerated from .disco/appspec.json. */\n"
        + "\n".join(imports)
        + "\n\n"
        "export default function App() {\n"
        "  return (\n"
        '    <div className="app-main">\n' + "\n".join(renders) + "\n"
        "    </div>\n"
        "  );\n}\n"
    )


def _emit_main_tsx() -> str:
    return (
        'import React from "react";\n'
        'import { createRoot } from "react-dom/client";\n'
        'import App from "./App";\n'
        'import "./styles.css";\n\n'
        'const el = document.getElementById("root");\n'
        "if (el) {\n"
        "  createRoot(el).render(\n"
        "    <React.StrictMode>\n"
        "      <App />\n"
        "    </React.StrictMode>\n"
        "  );\n"
        "}\n"
    )


def _seo_abs_url(base_url: str, route: str) -> str:
    """Join a validated http(s) base URL and a spec-validated ABSOLUTE route
    (always starts with '/') with exactly one slash between them — a trailing-
    slash base_url can never produce a '//' in sitemap/OG URLs."""
    return base_url.rstrip("/") + route


def _seo_head_extras(app: AppSpec) -> str:
    """The Epic F5.3 `<head>` additions: meta description, the OG tags, and a
    JSON-LD ``WebSite`` block. Returns the EMPTY STRING when `app.seo` is None —
    `_emit_index_html` interpolates this directly, so the no-seo index.html is
    PROVABLY byte-identical to the pre-F5.3 output (the hard constraint).

    Every user-provided value lands attribute-escaped (`_html_text`). The JSON-LD
    payload is json.dumps output (never hand-built JSON) with `&`/`<`/`>` forced
    to \\uXXXX escapes afterwards — a legal transform inside JSON string
    literals — so a hostile description can never close the `<script>` tag or
    open markup inside it."""
    seo = app.seo
    if seo is None:
        return ""
    site_name = seo.site_name or app.name
    canonical = _seo_abs_url(seo.base_url, "/")
    lines = [
        f'    <meta name="description" content="{_html_text(seo.site_description)}" />\n',
        f'    <meta property="og:title" content="{_html_text(site_name)}" />\n',
        f'    <meta property="og:description" content="{_html_text(seo.site_description)}" />\n',
        '    <meta property="og:type" content="website" />\n',
        f'    <meta property="og:url" content="{_html_text(canonical)}" />\n',
    ]
    if seo.social_image_url is not None:
        lines.append(
            f'    <meta property="og:image" content="{_html_text(seo.social_image_url)}" />\n'
        )
    ld_payload = (
        json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": site_name,
                "url": canonical,
                "description": seo.site_description,
            },
            ensure_ascii=False,
        )
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    lines.append(f'    <script type="application/ld+json">{ld_payload}</script>\n')
    return "".join(lines)


def _seo_files(app: AppSpec) -> dict[str, str]:
    """The Epic F5.3 crawler files — an EMPTY dict when `app.seo` is None (the
    no-seo tree gains no files; the tree builders `files.update(...)` this).

    Emitted under `public/` because Vite copies `publicDir` verbatim into
    `dist/`, which is what wrangler's `[assets]` layer serves — a root-level
    robots.txt would never reach production (a false affordance). sitemap.xml
    carries one `<url>` per AppSpec page, absolute via `_seo_abs_url` (no
    double-slash joins); `<loc>` values are escaped (routes are already
    charset-constrained by the spec — belt-and-suspenders for the base URL)."""
    seo = app.seo
    if seo is None:
        return {}
    routes = [page.route for page in app.pages]
    if app.blog is not None:
        from .blog_primitive import blog_routes_for

        routes.extend(blog_routes_for(app))
    entries = "".join(
        "  <url>\n"
        f"    <loc>{_html_text(_seo_abs_url(seo.base_url, route))}</loc>\n"
        "  </url>\n"
        for route in routes
    )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}"
        "</urlset>\n"
    )
    robots = f"User-agent: *\nAllow: /\n\nSitemap: {_seo_abs_url(seo.base_url, '/sitemap.xml')}\n"
    return {"public/robots.txt": robots, "public/sitemap.xml": sitemap}


def _emit_index_html(app: AppSpec, design: DesignSpec) -> str:
    href = _google_fonts_href(design)
    ver = _md.attr(_md.DataDiscoAttr.VERSION, _md.METADATA_VERSION)
    return (
        "<!doctype html>\n"
        f'<html lang="en"{ver}>\n'
        "  <head>\n"
        '    <meta charset="utf-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"    <title>{_html_text(app.name)}</title>\n"
        '    <link rel="preconnect" href="https://fonts.googleapis.com" />\n'
        '    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\n'
        f'    <link rel="stylesheet" href="{href}" />\n'
        # F5.3: "" when app.seo is None — the no-seo output is byte-identical.
        f"{_seo_head_extras(app)}"
        "  </head>\n"
        "  <body>\n"
        '    <div id="root"></div>\n'
        '    <script type="module" src="/src/main.tsx"></script>\n'
        "  </body>\n"
        "</html>\n"
    )


# ---- the D1 / Worker side -----------------------------------------------------

_SQL_TYPE: dict[str, str] = {
    "str": "TEXT",
    "string": "TEXT",
    "text": "TEXT",
    "datetime": "TEXT",
    "date": "TEXT",
    "email": "TEXT",
    "url": "TEXT",
    "int": "INTEGER",
    "integer": "INTEGER",
    "number": "INTEGER",
    "bool": "INTEGER",
    "boolean": "INTEGER",
    "float": "REAL",
}


def _sql_type(field_type: str) -> str:
    return _SQL_TYPE.get(field_type.strip().lower(), "TEXT")


def _table_name(lead: Entity) -> str:
    base = _slug(lead.id)
    # plural-ish table name; keep deterministic and SQL-safe.
    ident = base.replace("-", "_")
    return ident if ident.endswith("s") else ident + "s"


def _emit_schema_sql(lead: Entity) -> str:
    # Field names are spec-validated SAFE identifiers (EntityField._name_is_safe_
    # identifier), so the generator can trust them; we STILL double-quote every
    # identifier (belt-and-suspenders) so a reserved word can never break the DDL.
    table = _table_name(lead)
    cols = ['  "id" INTEGER PRIMARY KEY AUTOINCREMENT']
    for field in lead.fields:
        nullable = " NOT NULL" if field.required else ""
        cols.append(f'  "{field.name}" {_sql_type(field.type)}{nullable}')
    cols.append("  \"created_at\" TEXT NOT NULL DEFAULT (datetime('now'))")
    body = ",\n".join(cols)
    return (
        "-- Auto-generated D1 schema (Epic E). The ONE lead entity -> one table.\n"
        "-- Keep this migration in sync with src/db/schema.ts. Both files are generated\n"
        "-- from the same resolved lead entity, so they cannot drift by construction.\n"
        f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
        f"{body}\n"
        ");\n"
    )


def _drizzle_factory(field_type: str) -> str:
    """The drizzle-orm/sqlite-core column factory matching `_sql_type`.

    This intentionally lowers through the SAME SQL type map as `schema.sql` so the
    typed Drizzle table and the D1 migration are two renderings of one entity model.
    """
    sql_type = _sql_type(field_type)
    if sql_type == "INTEGER":
        return "integer"
    if sql_type == "REAL":
        return "real"
    return "text"


def _emit_drizzle_schema_ts(lead: Entity) -> str:
    """`src/db/schema.ts` - the typed Drizzle source of truth for the lead table."""
    table = _table_name(lead)
    cols = ['  id: integer("id").primaryKey({ autoIncrement: true }),']
    for field in lead.fields:
        chain = ".notNull()" if field.required else ""
        cols.append(f"  {field.name}: {_drizzle_factory(field.type)}({_ts(field.name)}){chain},")
    cols.append("  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),")
    return (
        "/* Auto-generated Drizzle schema - regenerated from .disco/appspec.json.\n"
        "   This typed table and schema.sql are lowered from the same resolved lead\n"
        "   entity, so the D1 migration and Drizzle layer cannot drift. */\n"
        'import { sql } from "drizzle-orm";\n'
        'import { integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";\n'
        "\n"
        f"export const leads = sqliteTable({_ts(table)}, {{\n" + "\n".join(cols) + "\n"
        "});\n"
    )


def _emit_drizzle_config_ts() -> str:
    return (
        'import { defineConfig } from "drizzle-kit";\n'
        "\n"
        "export default defineConfig({\n"
        '  dialect: "sqlite",\n'
        '  schema: "./src/db/schema.ts",\n'
        '  out: "./drizzle",\n'
        "});\n"
    )


def _worker_name(app: AppSpec) -> str:
    """The deterministic Cloudflare Worker name. NAMESPACED with `_namespace(app)` so
    two distinct apps that slug-collide on `app.name` get DISTINCT Worker names (no
    cross-app clobber on one account — SEC-10 defense-in-depth), while the same app
    always regenerates the same name (idempotent deploys)."""
    return _namespaced(_slug(app.name), _namespace(app))


def _db_name(app: AppSpec, lead: Entity) -> str:
    """The deterministic D1 database name shared by wrangler.toml, the package.json
    `db:*` scripts, and the OWNER_GUIDE — derived once so the binding and the owner's
    deploy commands can never disagree about which database to target. NAMESPACED
    with `_namespace(app)` (same rationale as `_worker_name`) so distinct apps never
    collide on one account, yet the same app stays idempotent."""
    return _namespaced(f"{_slug(app.name)}-{_table_name(lead)}", _namespace(app))


def _emit_wrangler_toml(app: AppSpec, lead: Entity) -> str:
    name = _worker_name(app)
    db_name = _db_name(app, lead)
    return (
        f'name = "{name}"\n'
        'compatibility_date = "2024-09-23"\n'
        'main = "worker/index.ts"\n'
        "\n"
        "# Workers Static Assets: serve the built SPA, falling back to index.html for\n"
        "# client routes (single-page-application not_found handling).\n"
        "[assets]\n"
        'directory = "./dist"\n'
        'binding = "ASSETS"\n'
        'not_found_handling = "single-page-application"\n'
        "# Worker-FIRST routing for the dynamic paths (Epic I): the lead API and the\n"
        "# admin read-back are handled by worker/index.ts, NOT the static-asset/SPA\n"
        "# layer. Without this, single-page-application not_found_handling can shadow a\n"
        "# navigation to /admin (or an /api/* read) with index.html so the Worker never\n"
        "# runs. Requires Wrangler v4.20+ (the array form of run_worker_first).\n"
        'run_worker_first = ["/api/*", "/admin"]\n'
        "\n"
        "# The D1 database the Worker inserts leads into. Replace database_id after\n"
        "# `wrangler d1 create`.\n"
        "[[d1_databases]]\n"
        'binding = "DB"\n'
        f'database_name = "{db_name}"\n'
        'database_id = "REPLACE_WITH_D1_DATABASE_ID"\n'
        "\n"
        "# OWNER ACTION REQUIRED — admin read-back auth.\n"
        "# The lead READS (GET /api/leads and /admin) are gated behind a Bearer token.\n"
        "# Set it as a Cloudflare SECRET (never hardcode it, never commit it):\n"
        "#   wrangler secret put ADMIN_TOKEN\n"
        "# Until ADMIN_TOKEN is set the worker FAILS CLOSED: every read is denied (401),\n"
        "# so leads are never exposed. POST /api/leads (public lead submission) needs\n"
        "# no token.\n"
    )


def _emit_worker_ts(lead: Entity) -> str:
    cols = [f.name for f in lead.fields]
    required = [f.name for f in lead.fields if f.required]
    email_fields = [
        f.name
        for f in lead.fields
        if f.name.lower() == "email" or f.type.strip().lower() == "email"
    ]
    required_lit = _ts(required)
    cols_lit = _ts(cols)
    email_lit = _ts(email_fields)
    lead_values = "\n".join(f"    {_ts(c)}: rec[{_ts(c)}] ?? null," for c in cols)
    return (
        "/* Auto-generated Cloudflare Worker (Epic E): public lead capture + "
        "AUTH-GATED admin read-back.\n"
        "   READS (GET /api/leads, GET /admin) require `Authorization: Bearer "
        "<env.ADMIN_TOKEN>`;\n"
        "   a missing ADMIN_TOKEN FAILS CLOSED (reads denied). POST /api/leads "
        "(submission) stays public. */\n"
        'import { desc } from "drizzle-orm";\n'
        'import { drizzle } from "drizzle-orm/d1";\n'
        'import { leads } from "../src/db/schema";\n'
        "\n"
        "export interface Env {\n"
        "  DB: D1Database;\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "  // Admin read-back secret. Set with `wrangler secret put ADMIN_TOKEN`.\n"
        "  // Optional in the type so a MISSING token fails closed (denies reads)\n"
        "  // rather than throwing — never give this a hardcoded default.\n"
        "  ADMIN_TOKEN?: string;\n"
        "}\n\n"
        f"const REQUIRED: string[] = {required_lit};\n"
        f"const COLUMNS: string[] = {cols_lit};\n"
        f"const EMAIL_FIELDS: string[] = {email_lit};\n"
        "const MAX_FIELD_LEN = 2000;\n"
        "const EMAIL_RE = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;\n\n"
        "type LeadInsert = typeof leads.$inferInsert;\n\n"
        "function json(data: unknown, status = 200): Response {\n"
        "  return new Response(JSON.stringify(data), {\n"
        "    status,\n"
        '    headers: { "Content-Type": "application/json" },\n'
        "  });\n"
        "}\n\n"
        "// Escape EVERY dynamic value before it goes into HTML (stored-XSS defense).\n"
        "function escapeHtml(value: unknown): string {\n"
        "  return String(value)\n"
        '    .replace(/&/g, "&amp;")\n'
        '    .replace(/</g, "&lt;")\n'
        '    .replace(/>/g, "&gt;")\n'
        '    .replace(/"/g, "&quot;")\n'
        '    .replace(/\'/g, "&#39;");\n'
        "}\n\n"
        "// Constant-token check; a missing ADMIN_TOKEN denies all reads (fail closed).\n"
        "function isAuthorized(request: Request, env: Env): boolean {\n"
        "  const expected = env.ADMIN_TOKEN;\n"
        "  if (!expected) return false;\n"
        '  const header = request.headers.get("Authorization") ?? "";\n'
        '  const prefix = "Bearer ";\n'
        "  if (!header.startsWith(prefix)) return false;\n"
        "  return header.slice(prefix.length) === expected;\n"
        "}\n\n"
        "type LeadCheck =\n"
        "  | { ok: true; rec: Record<string, unknown> }\n"
        "  | { ok: false; error: string };\n\n"
        "// Validate the submission: a JSON OBJECT, known keys only, required present,\n"
        "// every value a bounded string, email-shaped where expected.\n"
        "function validateLead(body: unknown): LeadCheck {\n"
        '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
        '    return { ok: false, error: "request body must be a JSON object" };\n'
        "  }\n"
        "  const rec = body as Record<string, unknown>;\n"
        "  for (const key of Object.keys(rec)) {\n"
        "    if (!COLUMNS.includes(key)) {\n"
        "      return { ok: false, error: `unknown field: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of REQUIRED) {\n"
        "    const v = rec[key];\n"
        '    if (v === undefined || v === null || v === "") {\n'
        "      return { ok: false, error: `missing required field: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of COLUMNS) {\n"
        "    const v = rec[key];\n"
        "    if (v === undefined || v === null) continue;\n"
        '    if (typeof v !== "string") {\n'
        "      return { ok: false, error: `field must be a string: ${key}` };\n"
        "    }\n"
        "    if (v.length > MAX_FIELD_LEN) {\n"
        "      return { ok: false, error: `field too long: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of EMAIL_FIELDS) {\n"
        "    const v = rec[key];\n"
        '    if (typeof v === "string" && v !== "" && !EMAIL_RE.test(v)) {\n'
        "      return { ok: false, error: `invalid email: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  return { ok: true, rec };\n"
        "}\n\n"
        "function leadValues(rec: Record<string, unknown>): LeadInsert {\n"
        "  return {\n"
        f"{lead_values}\n"
        "  } as LeadInsert;\n"
        "}\n\n"
        "async function insertLead(env: Env, body: unknown): Promise<Response> {\n"
        "  const check = validateLead(body);\n"
        "  if (!check.ok) {\n"
        "    return json({ error: check.error }, 400);\n"
        "  }\n"
        "  const rec = check.rec;\n"
        "  try {\n"
        "    const db = drizzle(env.DB);\n"
        "    await db.insert(leads).values(leadValues(rec)).run();\n"
        "  } catch {\n"
        '    return json({ error: "could not save lead" }, 500);\n'
        "  }\n"
        "  return json({ ok: true }, 201);\n"
        "}\n\n"
        "async function listLeads(env: Env): Promise<Record<string, unknown>[]> {\n"
        "  const db = drizzle(env.DB);\n"
        "  const rows = await db.select().from(leads).orderBy(desc(leads.id)).limit(200).all();\n"
        "  return rows as Record<string, unknown>[];\n"
        "}\n\n"
        "// Server-rendered leads table — EVERY cell escaped via escapeHtml (no raw\n"
        "// interpolation of lead values into HTML).\n"
        "function adminTable(rows: Record<string, unknown>[]): Response {\n"
        '  const head = COLUMNS.map((c) => `<th>${escapeHtml(c)}</th>`).join("");\n'
        "  const body = rows\n"
        "    .map(\n"
        "      (r) =>\n"
        "        `<tr>${COLUMNS.map((c) => "
        '`<td>${escapeHtml(r[c] ?? "")}</td>`).join("")}</tr>`\n'
        "    )\n"
        '    .join("");\n'
        "  const html =\n"
        '    "<!doctype html><html><head><meta charset=\\"utf-8\\"><title>Leads</title>" +\n'
        '    "</head><body><h1>Leads</h1><table border=\\"1\\" cellpadding=\\"6\\">" +\n'
        "    `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></body></html>`;\n"
        '  return new Response(html, { headers: { "Content-Type": "text/html" } });\n'
        "}\n\n"
        "// 401 sign-in shell: NO lead data, just a token prompt that re-requests\n"
        "// /admin with the Bearer header (token never embedded in the HTML).\n"
        "function adminLoginPage(): Response {\n"
        "  const html =\n"
        '    "<!doctype html><html><head><meta charset=\\"utf-8\\">" +\n'
        '    "<title>Admin sign-in</title></head><body><h1>Admin sign-in</h1>" +\n'
        '    "<p>Enter the admin token to view leads.</p>" +\n'
        '    "<form id=\\"login\\"><input id=\\"token\\" type=\\"password\\" " +\n'
        '    "placeholder=\\"Admin token\\" autocomplete=\\"off\\" />" +\n'
        '    "<button type=\\"submit\\">View leads</button></form><script>" +\n'
        '    "var f=document.getElementById(\\"login\\");" +\n'
        '    "f.addEventListener(\\"submit\\",async function(e){" +\n'
        '    "e.preventDefault();" +\n'
        '    "var t=document.getElementById(\\"token\\").value;" +\n'
        '    "var r=await fetch(\\"/admin\\",{headers:{Authorization:\\"Bearer \\"+t}});" +\n'
        '    "var h=await r.text();" +\n'
        '    "document.open();document.write(h);document.close();" +\n'
        '    "});</script></body></html>";\n'
        '  return new Response(html, { status: 401, headers: { "Content-Type": "text/html" } });\n'
        "}\n\n"
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    const url = new URL(request.url);\n"
        '    if (url.pathname === "/api/leads" && request.method === "POST") {\n'
        "      let body: unknown;\n"
        "      try {\n"
        "        body = await request.json();\n"
        "      } catch {\n"
        '        return json({ error: "invalid JSON" }, 400);\n'
        "      }\n"
        "      return insertLead(env, body);\n"
        "    }\n"
        '    if (url.pathname === "/api/leads" && request.method === "GET") {\n'
        "      if (!isAuthorized(request, env)) {\n"
        '        return json({ error: "unauthorized" }, 401);\n'
        "      }\n"
        "      const rows = await listLeads(env);\n"
        "      return json({ leads: rows });\n"
        "    }\n"
        '    if (url.pathname === "/admin" && request.method === "GET") {\n'
        "      if (!isAuthorized(request, env)) {\n"
        "        return adminLoginPage();\n"
        "      }\n"
        "      const rows = await listLeads(env);\n"
        "      return adminTable(rows);\n"
        "    }\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_package_json(app: AppSpec, db_name: str) -> str:
    pkg = {
        "name": _slug(app.name),
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview",
            # Epic I — Cloudflare local/remote D1 init + local CF dev. `db:local`
            # applies schema.sql to the on-disk Miniflare D1 used by `wrangler dev`;
            # `db:remote` applies it to the deployed D1; `cf:dev` runs the Worker +
            # built assets locally (workerd) so the owner can verify before deploy.
            "db:local": f"wrangler d1 execute {db_name} --local --file=./schema.sql",
            "db:remote": f"wrangler d1 execute {db_name} --remote --file=./schema.sql",
            "cf:dev": "wrangler dev",
            "deploy": "wrangler deploy",
        },
        "dependencies": {
            "drizzle-orm": "^0.44.2",
            "react": "^18.3.1",
            "react-dom": "^18.3.1",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.1",
            "drizzle-kit": "^0.31.4",
            "typescript": "^5.5.4",
            "vite": "^5.4.2",
            # v4.20+ for the array form of assets.run_worker_first (see wrangler.toml).
            "wrangler": "^4.20.0",
        },
    }
    return json.dumps(pkg, indent=2, ensure_ascii=False) + "\n"


def _emit_tsconfig() -> str:
    cfg = {
        "compilerOptions": {
            "target": "ES2020",
            "useDefineForClassFields": True,
            "lib": ["ES2020", "DOM", "DOM.Iterable"],
            "module": "ESNext",
            "skipLibCheck": True,
            "moduleResolution": "bundler",
            "jsx": "react-jsx",
            "strict": True,
            "noEmit": True,
        },
        "include": ["src", "worker"],
    }
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def _emit_vite_config() -> str:
    return (
        'import { defineConfig } from "vite";\n'
        'import react from "@vitejs/plugin-react";\n\n'
        "export default defineConfig({\n"
        "  plugins: [react()],\n"
        '  build: { outDir: "dist" },\n'
        "});\n"
    )


def _emit_manifest_ts(app: AppSpec, design: DesignSpec, names: dict[tuple[str, str], str]) -> str:
    """A deterministic digest of BOTH specs + the section inventory. Any spec change
    (structure, content, or design) changes the digest, so the manifest is part of
    the touched set on every mutation — a cheap, single 'specs ⇄ tree are in sync'
    fingerprint the tool diff can rely on."""
    payload = json.dumps(
        {
            "app": app.model_dump(mode="json"),
            "design": design.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    sections = [
        {"page": page.id, "section": section.id, "component": _comp_name(names, page, section)}
        for page, section in _iter_sections(app)
    ]
    return (
        "/* Auto-generated build manifest — a digest of the App + Design specs. */\n"
        f"export const SPEC_DIGEST = {_ts(digest)};\n"
        f"export const SECTIONS = {_ts(sections)} as const;\n"
    )


# ---- Cloudflare export deliverables (Epic I) ----------------------------------
#
# Epic E already emits worker/index.ts, schema.sql, and wrangler.toml. Epic I adds
# the CONFIG-COMPLETENESS + owner-deploy layer on top of that single generated tree:
# a secret TEMPLATE (never a real secret), a .gitignore that keeps the real secret
# out of git, and an OWNER_GUIDE that documents the exact local-verify + deploy flow
# the owner runs (Disco never deploys). All deterministic, all design_lint-inert
# (none of these extensions are scanned by the linter).


def _emit_dev_vars_example() -> str:
    """`.dev.vars.example` — the template the owner copies to `.dev.vars` (gitignored)
    so `wrangler dev` has the admin secret locally. Carries NO real secret: ADMIN_TOKEN
    is a placeholder. `.dev.vars` itself is NEVER generated (a real secret must never
    land in the export tree); the verifier fails if a real `.dev.vars` is present."""
    return (
        "# Cloudflare local dev secrets — TEMPLATE ONLY (no real secret here).\n"
        "# Copy this file to .dev.vars (which .gitignore excludes — NEVER commit it) and\n"
        "# replace the value. `wrangler dev` loads .dev.vars to inject secrets locally;\n"
        "# in production set the SAME secret with `wrangler secret put ADMIN_TOKEN`.\n"
        "#\n"
        "# ADMIN_TOKEN gates the admin read-back (GET /api/leads and /admin). Until it is\n"
        "# set the Worker FAILS CLOSED — every read is denied — so leads are never exposed.\n"
        "ADMIN_TOKEN=replace-me\n"
    )


def _emit_gitignore() -> str:
    """A `.gitignore` that keeps build output, dependencies, and — critically — the
    real `.dev.vars` secret file out of version control. `.dev.vars.example` (the
    template) is intentionally NOT ignored so the contract is documented in git."""
    return (
        "# Dependencies\n"
        "node_modules/\n"
        "\n"
        "# Build output\n"
        "dist/\n"
        "\n"
        "# Cloudflare\n"
        ".wrangler/\n"
        "\n"
        "# Local secrets — NEVER commit real secrets. The .dev.vars.example template\n"
        "# IS committed (it documents the contract); the real .dev.vars is not.\n"
        ".dev.vars\n"
    )


def _emit_owner_guide_md(app: AppSpec, lead: Entity, db_name: str) -> str:
    """`OWNER_GUIDE.md` — owner-run deploy INSTRUCTIONS (Epic I3). This is documentation,
    NOT execution: Disco generates + locally verifies the app, but the owner runs every
    Cloudflare command. It states what was verified locally vs what the owner deploys,
    the prerequisites, the local-CF verify path, the remote deploy path, and the secret/
    rollback notes. Deterministic for a given (app, lead)."""
    app_name = _html_text(app.name)
    return (
        f"# Deploy guide — {app_name}\n"
        "\n"
        "This is a Cloudflare-ready lead-gen app: a Vite/React SPA served by Cloudflare\n"
        "Workers Static Assets, with a Worker (`worker/index.ts`) that captures leads\n"
        "into a D1 database and serves an auth-gated admin read-back.\n"
        "\n"
        "**Disco generated and LOCALLY verified this app** (design-clean; valid D1\n"
        "schema; a structurally-correct, parameterized, fail-closed lead/admin worker\n"
        "contract). Disco does **not** deploy — deploying to your Cloudflare account is\n"
        "the steps below, which **you** run. Nothing here sends data anywhere until you\n"
        "do.\n"
        "\n"
        "## What Disco verified locally vs what you deploy\n"
        "\n"
        "- **Verified locally (no Cloudflare account needed):** the design lint is clean;\n"
        "  `schema.sql` is valid SQLite that round-trips a lead row and enforces NOT NULL;\n"
        "  the Worker's structure enforces the lead/admin contract (public POST insert is\n"
        "  parameterized; reads are Bearer-gated and fail closed when `ADMIN_TOKEN` is\n"
        "  unset); the SPA routes and sections render.\n"
        "- **Drizzle schema layer:** `src/db/schema.ts` is the typed Drizzle source of\n"
        "  truth used by the Worker; `schema.sql` remains the D1 migration applied by\n"
        "  `wrangler d1 execute`. Both are generated from the same lead entity, so the\n"
        "  typed schema and migration stay in sync by construction.\n"
        "- **You deploy:** create the real D1 database, load the schema, set the\n"
        "  `ADMIN_TOKEN` secret, build the SPA, and publish the Worker.\n"
        "\n"
        "Actual end-to-end runtime behaviour on Cloudflare's edge is only proven once you\n"
        "run the local CF emulation (`npm run cf:dev`) and/or deploy — see below.\n"
        "\n"
        "## Prerequisites\n"
        "\n"
        "- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (the free plan\n"
        "  covers Workers + D1).\n"
        "- [Node.js](https://nodejs.org/) 18+ and npm.\n"
        "- Wrangler (installed as a dev dependency): `npm install`, then `npx wrangler\n"
        "  login` to authenticate.\n"
        "\n"
        "## 1. Create the D1 database\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 create {db_name}\n"
        "```\n"
        "\n"
        "Copy the printed `database_id` into `wrangler.toml`, replacing\n"
        "`REPLACE_WITH_D1_DATABASE_ID`.\n"
        "\n"
        "## 2. Verify locally with the Cloudflare emulator (recommended before deploy)\n"
        "\n"
        "This runs the real Worker + built assets against a local Miniflare D1 — the\n"
        "closest you can get to production without deploying.\n"
        "\n"
        "```sh\n"
        "cp .dev.vars.example .dev.vars     # then edit ADMIN_TOKEN in .dev.vars\n"
        "npm install\n"
        "npm run build                      # build the SPA into ./dist\n"
        "npm run db:local                   # apply schema.sql to the local D1\n"
        "npm run cf:dev                     # serve the Worker + assets locally\n"
        "```\n"
        "\n"
        "With `wrangler dev` running, smoke-test the lead flow:\n"
        "\n"
        "```sh\n"
        "# public lead submission → 201\n"
        "curl -i -X POST http://localhost:8787/api/leads \\\n"
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"name":"Ada","email":"ada@example.com","message":"hi"}\'\n'
        "\n"
        "# admin read-back WITHOUT the token → 401 (fails closed)\n"
        "curl -i http://localhost:8787/api/leads\n"
        "\n"
        "# admin read-back WITH the token → 200 + the lead\n"
        'curl -i http://localhost:8787/api/leads -H "Authorization: Bearer <ADMIN_TOKEN>"\n'
        "```\n"
        "\n"
        "## 3. Deploy to Cloudflare\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 execute {db_name} --remote --file=./schema.sql  # load the schema\n"
        "npx wrangler secret put ADMIN_TOKEN                 # set the admin read-back secret\n"
        "npm run build                                       # build the SPA\n"
        "npx wrangler deploy                                 # publish the Worker + assets\n"
        "```\n"
        "\n"
        "After deploy, visit `/admin` on your `*.workers.dev` URL (or your custom domain)\n"
        "and enter the `ADMIN_TOKEN` to view captured leads.\n"
        "\n"
        "## Secrets, rotation, and rollback\n"
        "\n"
        "- **Never commit secrets.** `.dev.vars` (your real local secret) is gitignored;\n"
        "  only `.dev.vars.example` (the placeholder template) belongs in git. The\n"
        "  production secret lives only in Cloudflare (`wrangler secret put`).\n"
        "- **Rotate** the admin token by re-running `wrangler secret put ADMIN_TOKEN`\n"
        "  with a new value; the old token stops working immediately.\n"
        "- **Roll back** a bad deploy from the Cloudflare dashboard\n"
        "  (Workers & Pages → your Worker → Deployments → roll back) or by re-running\n"
        "  `npx wrangler deploy` from a known-good checkout.\n"
        "- If `ADMIN_TOKEN` is ever unset, the Worker denies all reads (fail closed) —\n"
        "  set it again to restore admin access. Public lead submission is unaffected.\n"
    )


# ---- the entry point ----------------------------------------------------------


def generate(app_spec: AppSpec, design_spec: DesignSpec) -> dict[str, str]:
    """Lower the two specs into a complete AppKit Cloudflare app tree.

    DETERMINISTIC: identical specs → byte-identical sorted `{path: contents}`. No
    IO, no clock, no randomness. DISPATCHES on the resolved PRIMITIVE
    (`AppSpec.app_kind`): `lead_gen` (the default + the fallback for any
    unrecognized kind) lowers to the lead-capture app EXACTLY as Epic E did;
    `directory` lowers to the static multi-route directory site (Epic N). The
    per-primitive `generate` callable owns the shape; the shared emitters below are
    reused across primitives."""
    return resolve_primitive(app_spec.app_kind).generate(app_spec, design_spec)


def _generate_lead_gen(app_spec: AppSpec, design_spec: DesignSpec) -> dict[str, str]:
    """Lower the two specs into a complete LEAD-GEN Cloudflare app tree (Epic E).

    The lead entity is resolved (not mutated) from the AppSpec; `app_create` is
    responsible for persisting a synthesized entity back into the spec so the
    on-disk spec and this tree never disagree.

    F3.1: folded FORM entities (form sections wired via `content_ref` — see
    `form_primitive`) extend the schema/worker/drizzle output through the
    `lower_form_*` wrappers and swap the wired form sections to the app-form
    component. With no forms the wrappers return the base emitters' output
    UNCHANGED, so every pre-F3.1 spec lowers byte-identically."""
    # Lazy import (records-style): form_primitive is force-imported at the end of
    # this module, so it is always loaded by the time generate() runs.
    from .analytics_primitive import (
        emit_analytics_dashboard_component,
        is_analytics_dashboard_section,
        lower_analytics_app_tsx,
        lower_analytics_main_tsx,
        lower_analytics_schema_sql,
        lower_analytics_worker_ts,
    )
    from .blog_primitive import emit_app_tsx_with_blog_routes, emit_blog_files, has_blog
    from .feature_flags_primitive import (
        emit_feature_flags_admin_component,
        emit_feature_flags_hook_ts,
        feature_flags_for,
        is_feature_flags_admin_section,
        lower_feature_flags_drizzle_ts,
        lower_feature_flags_schema_sql,
        lower_feature_flags_styles_css,
        lower_feature_flags_worker_ts,
    )
    from .form_primitive import (
        emit_app_form_component,
        form_entities_for,
        lower_form_drizzle_ts,
        lower_form_schema_sql,
        lower_form_worker_ts,
    )

    lead = resolve_lead_entity(app_spec)
    forms = form_entities_for(app_spec, lead)
    flags = feature_flags_for(app_spec)
    form_by_id = {e.id: e for e in forms}
    db_name = _db_name(app_spec, lead)
    # ONE collision-free (page, section) → component-name map, shared by every emitter
    # so the imports / file paths / content keys / manifest never disagree.
    names = _component_names(app_spec)
    files: dict[str, str] = {
        "index.html": _emit_index_html(app_spec, design_spec),
        "package.json": _emit_package_json(app_spec, db_name),
        "drizzle.config.ts": _emit_drizzle_config_ts(),
        "tsconfig.json": _emit_tsconfig(),
        "vite.config.ts": _emit_vite_config(),
        "wrangler.toml": _emit_wrangler_toml(app_spec, lead),
        "schema.sql": lower_analytics_schema_sql(
            lower_feature_flags_schema_sql(lower_form_schema_sql(lead, forms), flags),
            app_spec,
        ),
        "worker/index.ts": lower_analytics_worker_ts(
            lower_feature_flags_worker_ts(lower_form_worker_ts(lead, forms), flags),
            app_spec,
        ),
        "src/main.tsx": lower_analytics_main_tsx(_emit_main_tsx(), app_spec),
        # ONE route-aware shell builder wins: with blog present its shell already
        # routes every host page (the analytics fold appends /analytics as a real
        # Page); without blog the analytics lowering upgrades the plain shell.
        "src/App.tsx": (
            emit_app_tsx_with_blog_routes(app_spec, names)
            if has_blog(app_spec)
            else lower_analytics_app_tsx(_emit_app_tsx(app_spec, names), app_spec, names)
        ),
        "src/api/client.ts": _emit_api_client_ts(),
        "src/hooks/useSubmit.ts": _emit_submit_hook_ts(),
        "src/styles.css": lower_feature_flags_styles_css(
            _emit_styles_css(design_spec), flags
        ),
        "src/db/schema.ts": lower_feature_flags_drizzle_ts(
            lower_form_drizzle_ts(lead, forms), flags
        ),
        "src/generated/content.ts": _emit_content_ts(app_spec, names),
        "src/generated/manifest.ts": _emit_manifest_ts(app_spec, design_spec, names),
        # Epic I — Cloudflare export deliverables (config completeness + owner guide).
        "OWNER_GUIDE.md": _emit_owner_guide_md(app_spec, lead, db_name),
        ".dev.vars.example": _emit_dev_vars_example(),
        ".gitignore": _emit_gitignore(),
    }
    if flags:
        files["src/hooks/useFlag.ts"] = emit_feature_flags_hook_ts(flags)
    for page, section in _iter_sections(app_spec):
        comp = _comp_name(names, page, section)
        form_entity = (
            form_by_id.get(section.content_ref)
            if section.kind == "form" and section.content_ref is not None
            else None
        )
        if is_analytics_dashboard_section(section):
            files[f"src/components/{comp}.tsx"] = emit_analytics_dashboard_component(
                comp, section
            )
        elif form_entity is not None:
            files[f"src/components/{comp}.tsx"] = emit_app_form_component(
                comp, section, form_entity
            )
        elif is_feature_flags_admin_section(section):
            files[f"src/components/{comp}.tsx"] = emit_feature_flags_admin_component(
                comp, section, flags
            )
        else:
            files[f"src/components/{comp}.tsx"] = _emit_component(comp, page, section, lead)
    # F5.3: {} when app_spec.seo is None — the no-seo tree is byte-identical.
    files.update(emit_blog_files(app_spec))
    files.update(_seo_files(app_spec))
    return dict(sorted(files.items()))


def default_lead_gen_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """A sensible DEFAULT lead-gen AppSpec for `app_create` when only a brief is
    given (no explicit AppSpec): one landing page — hero → features → lead form →
    footer — plus the synthesized lead entity and a submit action. Section
    `variant_id`s are taken from the recipe's preferred layouts where the kind
    matches (so the default app already reflects the recipe's composition).
    Deterministic for a given (name, recipe)."""
    title = name.strip() or "Your Brand"
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    sections = (
        Section(
            id="hero",
            kind="hero",
            variant_id=prefs.get("hero"),
            content=SectionContent(
                heading=title,
                subheading=recipe.summary,
                cta_label="Get started",
            ),
        ),
        Section(
            id="features",
            kind="features",
            variant_id=prefs.get("features"),
            content=SectionContent(
                heading="What we offer",
                items=("Thoughtful design", "Reliable delivery", "Real support"),
            ),
        ),
        Section(
            id="contact",
            kind="form",
            variant_id=prefs.get("form"),
            content=SectionContent(
                heading="Get in touch",
                subheading="Tell us about your project and we'll be in touch.",
                cta_label="Submit",
            ),
        ),
        Section(
            id="footer",
            kind="footer",
            variant_id=prefs.get("footer"),
            content=SectionContent(heading=title),
        ),
    )
    return AppSpec(
        schema_version=1,
        app_kind="lead_gen",
        name=title,
        pages=(Page(id="home", route="/", title="Home", sections=sections),),
        entities=(synthesized_lead_entity(),),
        primary_actions=(
            Action(id="submit_lead", label="Submit", type="submit", target=_DEFAULT_LEAD_ID),
        ),
    )


# ============================ EPIC N — the DIRECTORY primitive =================
#
# A directory site is a genuinely DIFFERENT shape from lead-gen: a multi-route
# static site (a home page + a `/directory` listing page), a searchable/filterable
# listing section, and NO server-side data plane (no /api/leads, no D1, no admin
# read-back). It reuses the SHARED, shape-agnostic emitters (design tokens/CSS,
# index/main/tsconfig/vite, the section components, content.ts, manifest) and adds
# only the directory-specific pieces below — a route-aware App shell, a searchable
# listing component, a static-only Worker, and a D1-free wrangler/package/owner
# guide. Deliberately explicit + small (the anti-over-generalization guardrail): no
# generic CRUD/API/auth DSL — that waits until a second primitive needs the same
# mechanism.


def _identity_app_spec(app: AppSpec) -> AppSpec:
    """A no-op `prepare_app_spec` for primitives that persist the spec verbatim (the
    directory primitive has no synthesized-entity step like lead-gen's lead entity)."""
    return app


def _emit_static_worker_ts() -> str:
    """A STATIC-ONLY Cloudflare Worker: every request falls through to the built-asset
    layer. A directory site has no server data plane — no /api/leads, no D1, no admin
    read-back — so the worker is the minimal SPA-asset passthrough. Emitted (rather
    than omitted) so the `worker/index.ts` path is present after a same-path overwrite
    of a prior lead-gen app, leaving no stale lead worker behind."""
    return (
        "/* Auto-generated Cloudflare Worker (Epic N): STATIC directory site — serves the\n"
        "   built assets only. No lead API, no D1 database, no admin read-back: a\n"
        "   directory primitive has no server-side data plane, so every request falls\n"
        "   through to the static-asset layer (the SPA index.html for client routes). */\n"
        "export interface Env {\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "}\n\n"
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_directory_schema_sql() -> str:
    """A table-free `schema.sql` placeholder. The directory primitive is fully static
    (no D1), but the path is kept present so re-generating over a previous lead-gen app
    at the same workspace path leaves NO stale lead schema behind."""
    return (
        "-- Auto-generated (Epic N): the directory primitive is a STATIC site — it has\n"
        "-- NO D1 tables. This file is intentionally present (and table-free) so\n"
        "-- regenerating over a previous lead-gen app at the same path leaves no stale\n"
        "-- schema behind.\n"
    )


def _emit_directory_wrangler_toml(app: AppSpec) -> str:
    """`wrangler.toml` for the static directory site: Workers Static Assets only — no
    `[[d1_databases]]` binding and no `run_worker_first` (there are no dynamic
    Worker-first routes), so the SPA asset layer serves everything."""
    name = _slug(app.name)
    return (
        f'name = "{name}"\n'
        'compatibility_date = "2024-09-23"\n'
        'main = "worker/index.ts"\n'
        "\n"
        "# Workers Static Assets: serve the built SPA, falling back to index.html for\n"
        "# client routes (single-page-application not_found handling). A directory site\n"
        "# is fully STATIC — there is no Worker-first dynamic route and no D1 binding.\n"
        "[assets]\n"
        'directory = "./dist"\n'
        'binding = "ASSETS"\n'
        'not_found_handling = "single-page-application"\n'
    )


def _emit_directory_package_json(app: AppSpec) -> str:
    """`package.json` for the static directory site — the same SPA build glue as
    lead-gen MINUS the D1 (`db:local` / `db:remote`) scripts, which have no meaning
    without a database."""
    pkg = {
        "name": _slug(app.name),
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview",
            # Local CF emulation (workerd) of the static worker + built assets, so the
            # owner can verify the SPA + static serving before deploying.
            "cf:dev": "wrangler dev",
            "deploy": "wrangler deploy",
        },
        "dependencies": {
            "react": "^18.3.1",
            "react-dom": "^18.3.1",
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.1",
            "typescript": "^5.5.4",
            "vite": "^5.4.2",
            "wrangler": "^4.20.0",
        },
    }
    return json.dumps(pkg, indent=2, ensure_ascii=False) + "\n"


def _emit_directory_owner_guide_md(app: AppSpec) -> str:
    """`OWNER_GUIDE.md` for the static directory site — owner-run deploy INSTRUCTIONS
    (Disco generates + locally verifies; the owner runs every Cloudflare command).
    No D1 / secret steps: a directory site is fully static, so the flow is just build
    + deploy."""
    app_name = _html_text(app.name)
    return (
        f"# Deploy guide — {app_name}\n"
        "\n"
        "This is a Cloudflare-ready STATIC directory site: a Vite/React SPA served by\n"
        "Cloudflare Workers Static Assets. It has no server-side data plane — no lead\n"
        "API, no D1 database, no admin read-back — so deploying is just build + publish.\n"
        "\n"
        "**Disco generated and LOCALLY verified this site** (design-clean; route + section\n"
        "coverage; a static-only Worker that serves the built assets). Disco does **not**\n"
        "deploy — the steps below are the ones **you** run.\n"
        "\n"
        "## Prerequisites\n"
        "\n"
        "- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (the free plan\n"
        "  covers Workers Static Assets).\n"
        "- [Node.js](https://nodejs.org/) 18+ and npm.\n"
        "- Wrangler (installed as a dev dependency): `npm install`, then `npx wrangler\n"
        "  login` to authenticate.\n"
        "\n"
        "## 1. Verify locally with the Cloudflare emulator (recommended before deploy)\n"
        "\n"
        "```sh\n"
        "npm install\n"
        "npm run build                      # build the SPA into ./dist\n"
        "npm run cf:dev                     # serve the Worker + built assets locally\n"
        "```\n"
        "\n"
        "Open the printed local URL and click through `/` and `/directory` — the listing\n"
        "page's search box filters the entries client-side.\n"
        "\n"
        "## 2. Deploy to Cloudflare\n"
        "\n"
        "```sh\n"
        "npm run build                                       # build the SPA\n"
        "npx wrangler deploy                                 # publish the Worker + assets\n"
        "```\n"
        "\n"
        "After deploy, visit your `*.workers.dev` URL (or your custom domain); the static\n"
        "assets and the SPA's client-side routes are served by the Workers asset layer.\n"
        "\n"
        "## Rollback\n"
        "\n"
        "- **Roll back** a bad deploy from the Cloudflare dashboard (Workers & Pages →\n"
        "  your Worker → Deployments → roll back) or by re-running `npx wrangler deploy`\n"
        "  from a known-good checkout.\n"
    )


def _emit_directory_listing_component(comp: str, page: Page, section: Section) -> str:
    """A SEARCHABLE/filterable directory listing component (the directory primitive's
    distinguishing section): a controlled search input filters the section's `items`
    (from content.ts) client-side. Reads copy from CONTENT by id like every other
    section, so updating content never regenerates this component."""
    layout = _variant_layout(section)
    classes = f"section kind-list variant-{layout} directory"
    disco_attrs = _disco_section_attrs(section)
    return (
        "/* Auto-generated directory listing component — do NOT hand-edit; regenerated "
        "from .disco/appspec.json. */\n"
        'import { useMemo, useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp)}] ?? {{}};\n"
        "  const items = c.items ?? [];\n"
        '  const [query, setQuery] = useState<string>("");\n'
        "  const filtered = useMemo(\n"
        "    () => items.filter((item) => item.toLowerCase().includes(query.toLowerCase())),\n"
        "    [items, query]\n"
        "  );\n"
        "  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        '      <div className="app-main">\n'
        '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"        {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{_disco_field_attr("subheading")}>'
        "{c.subheading}</p> : null}\n"
        '        <label className="directory-search">\n'
        "          <span>Search</span>\n"
        '          <input type="text" name="q" value={query}\n'
        '            placeholder="Filter listings"\n'
        "            onChange={(e) => setQuery(e.target.value)} />\n"
        "        </label>\n"
        '        <ul className="stacked-list">\n'
        "          {filtered.map((item, i) => (\n"
        f'            <li className="feature-item" key={{i}}{_disco_field_attr("items")}'
        f"{_disco_item_attrs(section, index_expr='i', item_kind='item')}>{{item}}</li>\n"
        "          ))}\n"
        "        </ul>\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n}\n"
    )


def _emit_directory_app_tsx(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    """A ROUTE-AWARE app shell: each page renders ONLY its own sections, selected by
    `window.location.pathname`. A directory site is multi-route (`/` + `/directory`),
    so unlike the single-page lead-gen shell this dispatches per route. (Lead-gen
    keeps its flat single-page `_emit_app_tsx` — byte-identical — so its output is
    unchanged.)"""
    imports: list[str] = []
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        imports.append(f'import {comp} from "./components/{comp}";')
    page_funcs: list[str] = []
    route_entries: list[str] = []
    for i, page in enumerate(app.pages):
        fn = f"Page{i}"
        renders = "\n".join(
            f"      <{_comp_name(names, page, section)} />" for section in page.sections
        )
        body = (renders + "\n") if renders else ""
        page_funcs.append(
            f"function {fn}(): ReactElement {{\n  return (\n    <>\n{body}    </>\n  );\n}}\n"
        )
        # Normalize the ROUTES key the SAME way the browser path is normalized below
        # (trim trailing slashes except root `/`), so a schema-valid trailing-slash
        # route like `/directory/` resolves to its page instead of falling through to
        # the fallback. `rstrip("/") or "/"` mirrors the JS `replace(/\/+$/, "") || "/"`.
        norm_route = page.route.rstrip("/") or "/"
        route_entries.append(f"  {_ts(norm_route)}: {fn},")
    fallback = "Page0" if app.pages else "() => null"
    return (
        "/* Auto-generated route-aware app shell (Epic N) — regenerated from "
        ".disco/appspec.json. */\n"
        'import { type ReactElement } from "react";\n'
        + "\n".join(imports)
        + "\n\n"
        + "\n".join(page_funcs)
        + "\n"
        "const ROUTES: Record<string, () => ReactElement> = {\n" + "\n".join(route_entries) + "\n"
        "};\n\n"
        "export default function App(): ReactElement {\n"
        '  const path = window.location.pathname.replace(/\\/+$/, "") || "/";\n'
        f"  const Page = ROUTES[path] ?? {fallback};\n"
        "  return (\n"
        '    <div className="app-main">\n'
        "      <Page />\n"
        "    </div>\n"
        "  );\n}\n"
    )


def _generate_directory(app_spec: AppSpec, design_spec: DesignSpec) -> dict[str, str]:
    """Lower the two specs into a complete STATIC directory-site tree (Epic N).

    Reuses the shared, shape-agnostic emitters (index/main/styles/content/manifest/
    tsconfig/vite) and adds the directory-specific files: a route-aware App shell, a
    static-only Worker, a table-free schema.sql, a D1-free wrangler/package/owner
    guide, and a searchable listing component for each `list` section."""
    from .blog_primitive import emit_app_tsx_with_blog_routes, emit_blog_files, has_blog

    names = _component_names(app_spec)
    files: dict[str, str] = {
        "index.html": _emit_index_html(app_spec, design_spec),
        "package.json": _emit_directory_package_json(app_spec),
        "tsconfig.json": _emit_tsconfig(),
        "vite.config.ts": _emit_vite_config(),
        "wrangler.toml": _emit_directory_wrangler_toml(app_spec),
        "schema.sql": _emit_directory_schema_sql(),
        "worker/index.ts": _emit_static_worker_ts(),
        "src/main.tsx": _emit_main_tsx(),
        "src/App.tsx": (
            emit_app_tsx_with_blog_routes(app_spec, names)
            if has_blog(app_spec)
            else _emit_directory_app_tsx(app_spec, names)
        ),
        "src/styles.css": _emit_styles_css(design_spec),
        "src/generated/content.ts": _emit_content_ts(app_spec, names),
        "src/generated/manifest.ts": _emit_manifest_ts(app_spec, design_spec, names),
        "OWNER_GUIDE.md": _emit_directory_owner_guide_md(app_spec),
        ".gitignore": _emit_gitignore(),
    }
    # Non-form sections reuse the shared component emitter; the `list` section becomes
    # the directory's searchable listing component. The lead arg is unused for these
    # (no form section in a directory site), so a synthesized default is harmless.
    lead = synthesized_lead_entity()
    for page, section in _iter_sections(app_spec):
        comp = _comp_name(names, page, section)
        if section.kind == "list":
            files[f"src/components/{comp}.tsx"] = _emit_directory_listing_component(
                comp, page, section
            )
        else:
            files[f"src/components/{comp}.tsx"] = _emit_component(comp, page, section, lead)
    # F5.3: {} when app_spec.seo is None — the no-seo tree is byte-identical.
    files.update(emit_blog_files(app_spec))
    files.update(_seo_files(app_spec))
    return dict(sorted(files.items()))


def default_directory_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """A sensible DEFAULT directory AppSpec for `app_create` when only a brief is
    given: a home page (hero → features → faq → footer) plus a `/directory` listing
    page (a searchable `list` section → footer), a `listing` entity describing the
    directory's record shape, and a nav action to the directory route. Section
    `variant_id`s follow the recipe's preferred layouts where the kind matches.
    Deterministic for a given (name, recipe)."""
    title = name.strip() or "Your Directory"
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    home = Page(
        id="home",
        route="/",
        title="Home",
        sections=(
            Section(
                id="hero",
                kind="hero",
                variant_id=prefs.get("hero"),
                content=SectionContent(heading=title, subheading=recipe.summary),
            ),
            Section(
                id="about",
                kind="features",
                variant_id=prefs.get("features"),
                content=SectionContent(
                    heading="What you'll find",
                    items=("Curated listings", "Searchable categories", "Always up to date"),
                ),
            ),
            Section(
                id="faq",
                kind="faq",
                variant_id=prefs.get("faq"),
                content=SectionContent(
                    heading="Questions",
                    items=("How do I get listed?", "How often is the directory updated?"),
                ),
            ),
            Section(
                id="footer",
                kind="footer",
                variant_id=prefs.get("footer"),
                content=SectionContent(heading=title),
            ),
        ),
    )
    directory = Page(
        id="directory",
        route="/directory",
        title="Directory",
        sections=(
            Section(
                id="listings",
                kind="list",
                variant_id=prefs.get("list"),
                content=SectionContent(
                    heading="Browse listings",
                    subheading="Search and filter the directory.",
                    items=(
                        "Acme Studio — Design",
                        "Northwind Labs — Research",
                        "Globex Co — Manufacturing",
                        "Initech — Software",
                    ),
                ),
            ),
            Section(
                id="directory_footer",
                kind="footer",
                variant_id=prefs.get("footer"),
                content=SectionContent(heading=title),
            ),
        ),
    )
    return AppSpec(
        schema_version=1,
        app_kind=DIRECTORY_PRIMITIVE_ID,
        name=title,
        pages=(home, directory),
        entities=(
            Entity(
                id="listing",
                name="Listing",
                fields=(
                    EntityField(name="title", type="str", required=True),
                    EntityField(name="category", type="str", required=False),
                    EntityField(name="summary", type="text", required=False),
                    EntityField(name="url", type="url", required=False),
                ),
            ),
        ),
        primary_actions=(
            Action(id="browse", label="Browse the directory", type="nav", target="/directory"),
        ),
    )


# ---- register the primitives (at import time, so the registry is populated before
# `generate` is ever called or the tools resolve a primitive). lead_gen FIRST so it
# is the fallback for any unrecognized app_kind. ------------------------------------

# Imported HERE (not at the top) on purpose: primitive_verify imports THIS module
# for the pure `resolve_lead_entity` (defined above), so the verify hooks can only
# be imported once that name exists — the same bottom-of-module dance as the
# importlib sibling-primitive imports below.
from .primitive_verify import directory_verify, lead_gen_verify  # noqa: E402

register_primitive(
    PrimitiveDefinition(
        id=LEAD_GEN_PRIMITIVE_ID,
        default_app_spec=default_lead_gen_app_spec,
        prepare_app_spec=ensure_lead_entity,
        generate=_generate_lead_gen,
        verify=lead_gen_verify,
    )
)
register_primitive(
    PrimitiveDefinition(
        id=DIRECTORY_PRIMITIVE_ID,
        default_app_spec=default_directory_app_spec,
        prepare_app_spec=_identity_app_spec,
        generate=_generate_directory,
        verify=directory_verify,
    )
)

# Register sibling primitives that depend on the shared emitters defined above.
importlib.import_module(".records_primitive", package=__package__)
importlib.import_module(".hello_primitive", package=__package__)
importlib.import_module(".form_primitive", package=__package__)
importlib.import_module(".seo_primitive", package=__package__)
importlib.import_module(".collection_primitive", package=__package__)
importlib.import_module(".analytics_primitive", package=__package__)
importlib.import_module(".blog_primitive", package=__package__)
importlib.import_module(".feature_flags_primitive", package=__package__)


__all__ = [
    "default_directory_app_spec",
    "default_lead_gen_app_spec",
    "ensure_lead_entity",
    "generate",
    "resolve_lead_entity",
    "synthesized_lead_entity",
]
