"""Identifiers, slugs, Cloudflare resource naming, and safe-interpolation escapers.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from urllib.parse import quote

from ..spec import AppSpec, Page, Section

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


def section_component_names(app: AppSpec) -> dict[tuple[str, str], str]:
    """Return the generated component/content key for every semantic section.

    AppKit deliberately publishes the generated key in ``content.ts`` and the
    build manifest.  Semantic tools also need to recognize that representation
    when an agent reads generated output before editing it.  Keep that mapping
    owned by the generator so collision suffixes and future naming changes can
    never drift across the read/edit boundary.
    """
    return _component_names(app)


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


def _iter_sections(app: AppSpec) -> list[tuple[Page, Section]]:
    return [(page, section) for page in app.pages for section in page.sections]
