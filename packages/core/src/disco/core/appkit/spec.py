"""AppKit EPIC C — the App & Design specs: the structured contracts the scaffold
generator (Epic D) consumes.

Two specs, two concerns:

* `AppSpec` describes the app STRUCTURE — pages/routes, the sections on each page,
  the data entities, and the primary user actions. It is what the scaffold
  generator walks to emit files; it says *what* the app is, not how it looks.
* `DesignSpec` describes the DESIGN DECISIONS — typography, palette, layout family,
  component style, density — DESCRIPTIVELY (a small, declared vocabulary), not as a
  CSS AST. Crucially it carries `justifications`: a deliberate, non-default choice
  must be justified with a real reason so Epic D's `design_lint` can tell an
  intentional decision from slop (an unjustified off-default value).

Both models are strict: `extra="forbid"` (an unknown field is a typo/version skew,
not silently dropped), and they validate the invariants the generator relies on
(routes are absolute, IDs are unique, colors are real hex, reasons are substantive).

IO helpers persist these as plain JSON under the workspace's `.disco/` directory.
There is NO new workspace-persistence API here — `.disco/` rides the existing
snapshot/rehydrate of the workspace root; these helpers just read/write the two
JSON files (schema-validated on load).

Layering: `disco.core` is the leaf package (.importlinter) — this module imports
ONLY pydantic + the stdlib. No agent-server / tools / runtime imports.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .spec_parts.io import _APPSPEC_NAME as _APPSPEC_NAME
from .spec_parts.io import _DESIGNSPEC_NAME as _DESIGNSPEC_NAME
from .spec_parts.io import _DISCO_DIR as _DISCO_DIR
from .spec_parts.io import _MAX_SPEC_BYTES as _MAX_SPEC_BYTES

# IO helpers live in `spec_parts/io.py` (kept out of this module to stay under the
# `python_or_harness_module_logical_gt_700` budget) and are re-exported here at
# their original names/paths — see the "IO helpers" section below for the full
# story. `spec_parts.io` imports `AppSpec`/`DesignSpec` back from this module, but
# only LOCALLY inside the functions that need them at call time (never at its own
# module scope), so this top-level import is a normal one-way dependency, not a
# cycle. Every name is imported `as` itself — the explicit-re-export convention —
# so this module's own attribute (not just `spec_parts.io`'s) is what every
# existing `from disco.core.appkit.spec import <name>` call site keeps resolving.
from .spec_parts.io import APPSPEC_RELPATH as APPSPEC_RELPATH
from .spec_parts.io import DESIGNSPEC_RELPATH as DESIGNSPEC_RELPATH
from .spec_parts.io import MAX_DESIGNSPEC_BYTES as MAX_DESIGNSPEC_BYTES
from .spec_parts.io import _load_json as _load_json
from .spec_parts.io import _parse_spec_bytes as _parse_spec_bytes
from .spec_parts.io import _save as _save
from .spec_parts.io import _serialize as _serialize
from .spec_parts.io import appspec_path as appspec_path
from .spec_parts.io import designspec_path as designspec_path
from .spec_parts.io import load_app_spec as load_app_spec
from .spec_parts.io import load_app_spec_from_bytes as load_app_spec_from_bytes
from .spec_parts.io import load_design_spec as load_design_spec
from .spec_parts.io import load_design_spec_from_bytes as load_design_spec_from_bytes
from .spec_parts.io import load_specs as load_specs
from .spec_parts.io import save_app_spec as save_app_spec
from .spec_parts.io import save_design_spec as save_design_spec
from .spec_parts.io import serialize_app_spec as serialize_app_spec
from .spec_parts.io import serialize_design_spec as serialize_design_spec

# ---- shared config ------------------------------------------------------------

# Reject unknown fields everywhere: an extra key is a typo or a version mismatch,
# which we want to fail loudly rather than silently ignore. `frozen=True` makes
# every model (and every nested model) immutable after construction: a valid spec
# can't be mutated post-validation into an invalid route/action/entity that then
# gets dumped to disk by `save_*` without re-running the validators.
#
# NOTE: `frozen=True` only blocks ATTRIBUTE REASSIGNMENT — it does NOT freeze the
# *values*. A plain `list` field stays mutable in place (`spec.pages.append(bad)`,
# `spec.pages[0] = bad`, `.clear()`), which would smuggle a duplicate id/route past
# the cross-field validators and on to disk. So every collection field below is a
# `tuple[...]`, not a `list[...]`: pydantic coerces an input JSON array to a tuple on
# construction, and tuples have no `append`/`__setitem__`/`clear`, so a validated
# spec is genuinely immutable end to end.
_STRICT = ConfigDict(extra="forbid", frozen=True)

# Accept #rgb or #rrggbb (case-insensitive). The design vocabulary is hex only.
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# A route is an absolute path of safe path segments: letters, digits, '/', '_', '-'.
# No spaces, no dots (so no '..' traversal), and we separately reject empty '//'
# segments. This is what the scaffold generator maps to files/routes.
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9/_-]*$")

# An entity field name is emitted VERBATIM by the generator as a SQL column
# identifier (schema.sql / the worker INSERT) and as a TSX/JS object key, so it
# must be a SAFE identifier: snake_case starting with a letter. Constraining it
# here (the Epic C airtight way) lets the generator TRUST the name — no escaping
# guesswork — and kills identifier-injection / broken-code via a hostile
# `.disco/appspec.json` (`first-name`, a SQL keyword, a quote, etc.).
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# SQL-reserved: the generator emits implicit `id` (PK) + `created_at` columns and
# sqlite reserves `rowid`; a user field with one of these names produces a duplicate
# column → invalid DDL.
_RESERVED_SQL_FIELD_NAMES = frozenset({"id", "created_at", "rowid"})

# JS-reserved: the field name is ALSO emitted VERBATIM as an object KEY in the
# generated TSX — the form state (`setForm({ ...form, [name]: value })`) and the
# content objects. `_IDENT_RE` requires a LEADING LETTER, so `__proto__` (leading
# `_`) is already rejected — but the lowercase Object.prototype / reserved
# identifiers below start with a letter and pass the pattern, yet are DANGEROUS as
# object keys: a field named `constructor` shadows `Object.prototype.constructor`
# on the form-state object (breaking the `{ ...form }` spread / serialization),
# and `hasownproperty` / `tostring` / `valueof` / … shadow the prototype methods
# the generated or runtime code may rely on. Field names are already lowercased
# snake_case (`_IDENT_RE`), so a case-sensitive match against these lowercase forms
# is exact. Reject at the spec boundary so a VALID-looking AppSpec can never emit a
# polluted form-state / content object.
_RESERVED_JS_FIELD_NAMES = frozenset(
    {
        "constructor",
        "prototype",
        "hasownproperty",
        "isprototypeof",
        "propertyisenumerable",
        "tostring",
        "tolocalestring",
        "valueof",
        "proto",
    }
)

# The full reject set (SQL ∪ JS). Kept as one public-ish name for any external probe.
_RESERVED_FIELD_NAMES = _RESERVED_SQL_FIELD_NAMES | _RESERVED_JS_FIELD_NAMES

# A declared font family is emitted into THREE generated contexts: a CSS
# `font-family` value (styles.css), a Google Fonts URL query (`family=` in
# index.html's stylesheet href), and a quoted CSS string. Real Google/web font
# families are plain words and digits separated by single spaces ("Space Grotesk",
# "IBM Plex Sans", "Source Sans 3"), so we constrain the family to that safe
# charset. This makes the name injection-proof BY CONSTRUCTION in every context: no
# quote can break out of the CSS string, no `<`/`>`/`"` can break out of the href
# attribute, no `&`/`?`/`:` can inject another font/URL param. The generator ALSO
# URL-/CSS-escapes regardless (belt-and-suspenders), but this kills the class.
_FONT_RE = re.compile(r"^[A-Za-z0-9 ]+$")

_MIN_REASON_LEN = 12

# The constrained section vocabulary the Epic E scaffolder switches on. `custom`
# is the escape hatch for a generic block.
SectionKind = Literal[
    "hero",
    "features",
    "cta",
    "list",
    "form",
    "table",
    "gallery",
    "testimonials",
    "pricing",
    "faq",
    "footer",
    "custom",
]

# List caps so validation itself is bounded — a hostile `.disco/*.json` can't make
# us validate an unbounded number of items (see also the file-size cap in IO).
_MAX_PAGES = 50
_MAX_SECTIONS = 30
_MAX_ENTITIES = 50
_MAX_FIELDS = 60
_MAX_ACTIONS = 30
_MAX_JUSTIFICATIONS = 50

# Bounded caps for SectionContent — a section's content is short copy, never a data
# dump, so every slot is length-capped and the items list is count-capped. This
# keeps a hostile/oversize `.disco/appspec.json` from smuggling unbounded content
# through (the file-size cap in IO is the outer backstop; these are the per-field
# inner ones), and gives `app_update_content` a SPEC-OWNED, validated place to land.
_MAX_HEADING = 200
_MAX_SUBHEADING = 300
_MAX_BODY = 4000
_MAX_CTA_LABEL = 120
_MAX_ITEM = 300
_MAX_ITEMS = 24
_MAX_SUCCESS_MESSAGE = 300
_MAX_STRIPE_SUCCESS_MESSAGE = 200

# ---- bounded free-form string aliases ----------------------------------------
#
# Every free-form spec string is length-capped so a SCHEMA-VALID AppSpec can NEVER
# drive the Epic E generator into an unwritable filesystem path. The generator
# emits one component file per section at
#     src/components/{_pascal(page.id)}{_pascal(section.id)}[<n>]Section.tsx
# (`generator._component_names`). `_pascal` strips every non-alphanumeric and
# concatenates the parts, so it never grows its input EXCEPT for a single leading
# "X" it prepends when the result would start with a digit — i.e.
# `len(_pascal(raw)) <= len(raw) + 1`. With ids capped at `_ID_MAX` (64) the worst
# case is therefore:
#
#   filename  = pascal(page.id) + pascal(section.id) + <collision suffix> + "Section.tsx"
#             <= 65 + 65 + 4 + len("Section.tsx")  = 65 + 65 + 4 + 11 = 145 bytes
#   rel path  = "src/components/" (15) + filename  <= 15 + 145         = 160 bytes
#
# The collision suffix is a bare integer bounded by the total section count
# (<= _MAX_PAGES * _MAX_SECTIONS = 1500), i.e. at most 4 digits. Both the single
# longest path SEGMENT (145) and the full relative path (160) sit well under the
# POSIX NAME_MAX of 255, so `app_create` can always write the tree. Picking the id
# cap is what makes that guarantee hold; the other caps just bound validation
# work + serialized size (the IO byte-cap is the outer backstop).
_ID_MAX = 64  # ids feed the generated component filename — see the bound above.

# page/section/entity/action ids + the field name/refs that feed the component path
_IdStr = Annotated[str, StringConstraints(max_length=_ID_MAX)]
_NameStr = Annotated[str, StringConstraints(max_length=200)]  # human display names / labels
_TitleStr = Annotated[str, StringConstraints(max_length=200)]  # page titles
_RouteStr = Annotated[str, StringConstraints(max_length=200)]  # in-app route paths
# action targets (may be an absolute URL)
_TargetStr = Annotated[str, StringConstraints(max_length=2048)]
# small vocab tokens (kinds/types/fonts/styles)
_ShortStr = Annotated[str, StringConstraints(max_length=120)]
_FontFamilyStr = Annotated[
    str,
    StringConstraints(max_length=120, pattern=_FONT_RE.pattern),
]
# justification prose (also has a MIN length validator below)
_ReasonStr = Annotated[str, StringConstraints(max_length=2000)]
_HexStr = Annotated[str, StringConstraints(max_length=7)]  # "#rrggbb" — also hex-validated below


def _validate_hex(value: str, *, field: str) -> str:
    if not _HEX_RE.match(value):
        raise ValueError(f"{field} must be a hex color like '#1a2b3c' or '#abc', got {value!r}")
    return value


# ============================ AppSpec ==========================================


class SectionContent(BaseModel):
    """The SPEC-OWNED content slots a section renders — the place
    `app_update_content` persists copy so the generated `content.ts` stays
    regenerable FROM the spec (never hand-edited in the generated tree).

    Every slot is bounded (length caps + a count-capped `items`) and unknown keys
    are forbidden, exactly like the rest of the Epic C contract. All slots default
    to None / empty so the model is fully backward-compatible: a section with no
    content validates, and the generator simply renders the slots that are set."""

    model_config = _STRICT

    heading: str | None = Field(default=None, max_length=_MAX_HEADING)
    subheading: str | None = Field(default=None, max_length=_MAX_SUBHEADING)
    body: str | None = Field(default=None, max_length=_MAX_BODY)
    cta_label: str | None = Field(default=None, max_length=_MAX_CTA_LABEL)
    items: tuple[str, ...] = Field(default_factory=tuple, max_length=_MAX_ITEMS)
    # The confirmation copy a `form` section shows after a successful submission
    # (the F3.1 form primitive folds it in; the generator bakes it into the emitted
    # form component). `exclude_if` keeps the slot OUT of every dump when unset, so
    # pre-F3.1 specs serialize byte-identically (spec digests / manifests unchanged).
    success_message: str | None = Field(
        default=None,
        max_length=_MAX_SUCCESS_MESSAGE,
        exclude_if=lambda value: value is None,
    )

    @field_validator("items")
    @classmethod
    def _items_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if len(item) > _MAX_ITEM:
                raise ValueError(
                    f"content item too long ({len(item)} > {_MAX_ITEM} chars): {item[:40]!r}…"
                )
        return value


class Section(BaseModel):
    """A block within a page (hero, features, cta, list, form, table, ...).

    `kind` is a CONSTRAINED vocabulary (`SectionKind`) so the Epic E scaffolder can
    switch on it deterministically; `custom` is the escape hatch for a generic
    block. `content_ref` optionally points at a content/entity source for the
    section (e.g. an entity id for a `table`). `variant_id` names the Epic D
    section-catalog layout to render (a variant OF this `kind`) — referential
    integrity is enforced by the generator/tools against the catalog (not here, to
    keep core's leaf-import graph acyclic: the catalog imports `SectionKind` from
    this module). `content` is the bounded, spec-owned copy the generator lowers
    into `content.ts`. Both default to None so the field is backward-compatible."""

    model_config = _STRICT

    id: _IdStr
    kind: SectionKind
    content_ref: _IdStr | None = None
    variant_id: _IdStr | None = None
    content: SectionContent | None = None


class Page(BaseModel):
    """A single routable page and the sections it stacks, in order."""

    model_config = _STRICT

    id: _IdStr
    route: _RouteStr
    title: _TitleStr
    sections: tuple[Section, ...] = Field(default_factory=tuple, max_length=_MAX_SECTIONS)

    @field_validator("route")
    @classmethod
    def _route_is_valid(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"route must start with '/', got {value!r}")
        if "//" in value:
            raise ValueError(f"route must not contain an empty '//' segment, got {value!r}")
        if ".." in value:
            raise ValueError(f"route must not contain '..', got {value!r}")
        if not _ROUTE_RE.match(value):
            raise ValueError(
                "route may contain only letters, digits, '/', '_' and '-' "
                f"(no spaces or other punctuation), got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _section_ids_unique(self) -> Page:
        _require_unique((s.id for s in self.sections), what=f"section id on page {self.id!r}")
        return self


class EntityField(BaseModel):
    """A field on an entity. `type` is an open scalar vocabulary
    (str/int/bool/datetime/text/...) the generator maps to columns/inputs.

    `references`, when set, names another Entity id whose implicit `id` column this
    field foreign-keys to. Referencing fields must be declared as integer-ish
    (`int`, `integer`, or `number`) and are emitted as INTEGER columns regardless of
    other type spelling.

    `label`, when set, is the human display label a generated input control shows
    for this field (the F3.1 form primitive folds it from `FormField.label`); when
    unset the generator derives one from `name`. `exclude_if` keeps it out of every
    dump when unset, so pre-F3.1 specs serialize byte-identically."""

    model_config = _STRICT

    name: _IdStr
    type: _ShortStr
    required: bool = False
    references: _IdStr | None = Field(default=None, exclude_if=lambda value: value is None)
    label: _NameStr | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("name")
    @classmethod
    def _name_is_safe_identifier(cls, value: str) -> str:
        if not _IDENT_RE.match(value):
            raise ValueError(
                "entity field name must be a snake_case identifier "
                f"(pattern {_IDENT_RE.pattern!r}: lowercase, starts with a letter, "
                f"only letters/digits/underscore), got {value!r}"
            )
        # The generator always emits implicit `id` (PK) + `created_at` columns (and
        # sqlite reserves `rowid`); a user field with one of these names produces a
        # duplicate column → invalid DDL. Reject at the spec so a valid AppSpec can
        # never generate a table that won't create.
        if value in _RESERVED_SQL_FIELD_NAMES:
            raise ValueError(
                f"entity field name {value!r} is reserved (the generator emits "
                f"implicit {sorted(_RESERVED_SQL_FIELD_NAMES)} columns); choose another name"
            )
        # The name is ALSO emitted verbatim as an object KEY in the generated TSX
        # (the form-state + content objects). A lowercase Object.prototype / reserved
        # identifier (`constructor`, `tostring`, …) passes `_IDENT_RE` but pollutes /
        # shadows the prototype on those objects → broken generated code. Reject it.
        if value in _RESERVED_JS_FIELD_NAMES:
            raise ValueError(
                f"entity field name {value!r} is a reserved JS object/prototype key "
                f"(emitted verbatim as an object key in the generated form-state and "
                f"content objects; {sorted(_RESERVED_JS_FIELD_NAMES)} pollute/shadow "
                f"Object.prototype); choose another name"
            )
        return value

    @model_validator(mode="after")
    def _reference_type_is_integer(self) -> EntityField:
        if self.references is None:
            return self
        if self.type.strip().lower() not in {"int", "integer", "number"}:
            raise ValueError(
                "entity field with references set must use type 'int', 'integer', "
                f"or 'number', got {self.type!r}"
            )
        return self


class Entity(BaseModel):
    """A data entity (model/record) the app stores and renders."""

    model_config = _STRICT

    id: _IdStr
    name: _NameStr
    fields: tuple[EntityField, ...] = Field(default_factory=tuple, max_length=_MAX_FIELDS)
    write_roles: tuple[_IdStr, ...] = Field(default=(), exclude_if=lambda value: not value)
    read_roles: tuple[_IdStr, ...] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _field_names_unique(self) -> Entity:
        _require_unique((f.name for f in self.fields), what=f"field name on entity {self.id!r}")
        return self

    @field_validator("write_roles", "read_roles")
    @classmethod
    def _roles_are_safe_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for role in value:
            if not _IDENT_RE.match(role):
                raise ValueError(
                    "entity role names must be snake_case identifiers "
                    f"(pattern {_IDENT_RE.pattern!r}: lowercase, starts with a letter, "
                    f"only letters/digits/underscore), got {role!r}"
                )
        return value


class Action(BaseModel):
    """A primary user action the scaffold generator wires up.

    `type` selects how `target` is interpreted (and validated):

    * `nav` / `link` — `target` is an in-app route (must start with '/');
    * `external`     — `target` is an absolute http(s) URL;
    * `submit`       — `target` is an entity id / endpoint name (any non-empty token).

    Structured (not a bare string) so Epic E can emit a real button/link/form
    handler deterministically instead of guessing."""

    model_config = _STRICT

    id: _IdStr
    label: _NameStr
    type: Literal["link", "nav", "submit", "external"]
    target: _TargetStr

    @model_validator(mode="after")
    def _target_matches_type(self) -> Action:
        target = self.target.strip()
        if not target:
            raise ValueError(f"action {self.id!r} target must be non-empty")
        if self.type in ("nav", "link"):
            if not target.startswith("/"):
                raise ValueError(
                    f"action {self.id!r} of type {self.type!r} must target an in-app "
                    f"route starting with '/', got {self.target!r}"
                )
        elif self.type == "external":
            if not (target.startswith("http://") or target.startswith("https://")):
                raise ValueError(
                    f"action {self.id!r} of type 'external' must target an http(s) URL, "
                    f"got {self.target!r}"
                )
        # `submit`: an entity id / endpoint token — only the non-empty check applies.
        return self


# ---- SEO metadata (Epic F5.3) ---------------------------------------------------

# Bounds for the SEO slots — shared with `seo_primitive.SeoSpec` (the fill-and-
# validate schema mirrors this resolved model) so the two can never drift.
MAX_SEO_DESCRIPTION = 300
MAX_SEO_URL = 2048
MAX_SEO_SITE_NAME = 200

# Bounds/defaults for the analytics primitive's resolved metadata. The fillable
# AnalyticsSpec mirrors these so the model-facing schema and persisted AppSpec
# cannot drift.
DEFAULT_ANALYTICS_DASHBOARD_PAGE_TITLE = "Analytics"
MAX_ANALYTICS_DASHBOARD_PAGE_TITLE = 120


def validate_http_url(value: str, *, field: str) -> str:
    """A SIMPLE absolute-URL shape check (stdlib urlparse, deliberately not a full
    RFC validator): an explicit `https://`/`http://` scheme + a non-empty host.
    Refuses the garbage classes that matter at the spec boundary — a bare domain,
    a relative path, `javascript:`/`data:` schemes, a host-less `https://` — while
    the emitters ALSO escape every URL they interpolate (belt-and-suspenders)."""
    if not (value.startswith("https://") or value.startswith("http://")):
        raise ValueError(f"{field} must start with 'https://' or 'http://', got {value!r}")
    if not urlparse(value).netloc:
        raise ValueError(f"{field} must have a host (e.g. 'https://example.com'), got {value!r}")
    return value


class SeoMeta(BaseModel):
    """The RESOLVED SEO metadata the `seo` primitive (Epic F5.3) folds into an app —
    what the SHARED emitters read to add the meta/OG/JSON-LD head block plus
    `public/robots.txt` / `public/sitemap.xml` to the generated tree.

    Mirrors `seo_primitive.SeoSpec`'s validated values (that module owns the
    model-facing fill-and-validate schema); `site_name=None` resolves to
    `AppSpec.name` at EMIT time, so a later app rename never strands a stale copy
    here. STRICTLY additive: `AppSpec.seo` defaults to None and is EXCLUDED from
    dumps when None, so every pre-F5.3 spec validates, serializes and generates
    byte-identically."""

    model_config = _STRICT

    site_description: str = Field(min_length=1, max_length=MAX_SEO_DESCRIPTION)
    base_url: str = Field(min_length=1, max_length=MAX_SEO_URL)
    social_image_url: str | None = Field(default=None, max_length=MAX_SEO_URL)
    site_name: str | None = Field(default=None, min_length=1, max_length=MAX_SEO_SITE_NAME)

    @field_validator("base_url")
    @classmethod
    def _base_url_is_http(cls, value: str) -> str:
        return validate_http_url(value, field="seo base_url")

    @field_validator("social_image_url")
    @classmethod
    def _social_image_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_http_url(value, field="seo social_image_url")


# ---- Blog content (Epic 5.3-lite) ----------------------------------------------

MAX_BLOG_POSTS = 50
MAX_BLOG_TITLE = 200
MAX_BLOG_SUMMARY = 300
MAX_BLOG_BODY_MD = 4000
MAX_BLOG_INDEX_TITLE = 200
MAX_BLOG_SLUG = 64

_BLOG_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_blog_slug(value: str, *, field: str) -> str:
    """A safe blog URL slug. Blog routes are emitted as `/blog/<slug>`, so slugs
    are lower-case path segments with no slash, dot, spaces, or punctuation."""
    if not _BLOG_SLUG_RE.match(value):
        raise ValueError(
            f"{field} must match {_BLOG_SLUG_RE.pattern!r}: lower-case letters, "
            f"digits and hyphens, starting with a letter/digit; got {value!r}"
        )
    return value


def validate_iso_date(value: str, *, field: str) -> str:
    """Validate a `YYYY-MM-DD` ISO calendar date while preserving the JSON string."""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date like '2026-07-07'") from exc
    return value


class BlogPostMeta(BaseModel):
    """One resolved blog post stored on AppSpec. The blog primitive owns the
    model-facing spec, but the generated tree must remain regenerable from
    `.disco/appspec.json`, so the folded post bodies live here."""

    model_config = _STRICT

    slug: str = Field(min_length=1, max_length=MAX_BLOG_SLUG)
    title: str = Field(min_length=1, max_length=MAX_BLOG_TITLE)
    date: str = Field(min_length=1, max_length=10)
    summary: str | None = Field(default=None, min_length=1, max_length=MAX_BLOG_SUMMARY)
    body_md: str = Field(min_length=1, max_length=MAX_BLOG_BODY_MD)

    @field_validator("slug")
    @classmethod
    def _slug_is_safe(cls, value: str) -> str:
        return validate_blog_slug(value, field="blog slug")

    @field_validator("date")
    @classmethod
    def _date_is_iso(cls, value: str) -> str:
        return validate_iso_date(value, field="blog date")

    @field_validator("title", "summary", "body_md")
    @classmethod
    def _text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("blog text fields must not be blank")
        return value


class BlogMeta(BaseModel):
    """Resolved blog content folded into an app by the `blog` primitive. `None`
    stays absent from dumps, so pre-blog specs and generated trees are unchanged."""

    model_config = _STRICT

    posts: tuple[BlogPostMeta, ...] = Field(min_length=1, max_length=MAX_BLOG_POSTS)
    index_page_title: str | None = Field(
        default=None, min_length=1, max_length=MAX_BLOG_INDEX_TITLE
    )

    @field_validator("index_page_title")
    @classmethod
    def _index_title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("blog index_page_title must not be blank")
        return value

    @model_validator(mode="after")
    def _slugs_unique(self) -> BlogMeta:
        _require_unique((post.slug for post in self.posts), what="blog post slug")
        return self


class AnalyticsMeta(BaseModel):
    """Resolved first-party analytics metadata folded into an app by the
    `analytics` primitive. Strictly additive: unset analytics is excluded from
    dumps, so pre-analytics specs serialize and generate exactly as before."""

    model_config = _STRICT

    dashboard_page_title: str = Field(
        default=DEFAULT_ANALYTICS_DASHBOARD_PAGE_TITLE,
        min_length=1,
        max_length=MAX_ANALYTICS_DASHBOARD_PAGE_TITLE,
    )

    @field_validator("dashboard_page_title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("analytics dashboard_page_title must not be blank")
        return stripped


class StripeMeta(BaseModel):
    """Resolved, non-secret Stripe configuration folded into an app.

    The operator-owned price mapping and every Stripe credential intentionally
    remain host-side.  ``plan_selector`` is only the stable lookup key the host
    maps to a configured Stripe Price; it is never a price id or amount.
    """

    model_config = _STRICT

    app_binding: _IdStr
    plan_selector: _IdStr
    entitlement_flag: _IdStr
    success_message: str = Field(min_length=1, max_length=_MAX_STRIPE_SUCCESS_MESSAGE)

    @field_validator("plan_selector", "entitlement_flag")
    @classmethod
    def _identifiers_are_safe(cls, value: str) -> str:
        if not _IDENT_RE.match(value):
            raise ValueError(
                "Stripe identifiers must be snake_case identifiers "
                f"(pattern {_IDENT_RE.pattern!r}), got {value!r}"
            )
        return value

    @field_validator("app_binding")
    @classmethod
    def _app_binding_is_safe(cls, value: str) -> str:
        if not re.fullmatch(r"app_[0-9a-f]{32}", value):
            raise ValueError("Stripe app_binding must match 'app_' plus 32 lowercase hex digits")
        return value

    @field_validator("success_message")
    @classmethod
    def _success_message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Stripe success_message must not be blank")
        return value


class WebhookEndpointMeta(BaseModel):
    """One non-secret webhook contract lowered into trusted Worker code."""

    model_config = _STRICT

    endpoint_id: _IdStr
    direction: Literal["inbound", "outbound"]
    event_types: tuple[_ShortStr, ...] = Field(min_length=1, max_length=12)

    @field_validator("endpoint_id")
    @classmethod
    def _endpoint_id_is_safe(cls, value: str) -> str:
        if not _IDENT_RE.fullmatch(value):
            raise ValueError("webhook endpoint_id must be a snake_case identifier")
        return value

    @field_validator("event_types")
    @classmethod
    def _event_types_are_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        event_re = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
        if any(len(event) > 64 or event_re.fullmatch(event) is None for event in value):
            raise ValueError("webhook event types must be bounded dotted snake_case")
        _require_unique(value, what="webhook event type")
        return value


class WebhookMeta(BaseModel):
    """Resolved webhook contracts; target URLs and signing keys stay host-side."""

    model_config = _STRICT

    app_binding: _IdStr
    endpoints: tuple[WebhookEndpointMeta, ...] = Field(min_length=1, max_length=24)

    @field_validator("app_binding")
    @classmethod
    def _app_binding_is_safe(cls, value: str) -> str:
        if re.fullmatch(r"app_[0-9a-f]{32}", value) is None:
            raise ValueError("Webhook app_binding must match 'app_' plus 32 lowercase hex digits")
        return value

    @model_validator(mode="after")
    def _endpoint_ids_unique(self) -> WebhookMeta:
        _require_unique(
            (endpoint.endpoint_id for endpoint in self.endpoints), what="webhook endpoint"
        )
        return self


class AppSpec(BaseModel):
    """The app STRUCTURE the scaffold generator consumes.

    Minimal but sufficient: pages (with their sections), the data entities, and the
    primary actions. Cross-page invariants — unique page/section/entity/action ids,
    unique absolute routes, and well-formed routes — are enforced here so the
    generator can trust the spec."""

    model_config = _STRICT

    schema_version: int = Field(ge=1)
    # The dominant kind of thing being built; aligns with BuildBrief.app_kind.
    app_kind: _ShortStr
    name: _NameStr
    roles: tuple[_IdStr, ...] = Field(default=(), exclude_if=lambda value: not value)
    pages: tuple[Page, ...] = Field(default_factory=tuple, max_length=_MAX_PAGES)
    entities: tuple[Entity, ...] = Field(default_factory=tuple, max_length=_MAX_ENTITIES)
    primary_actions: tuple[Action, ...] = Field(default_factory=tuple, max_length=_MAX_ACTIONS)
    # Epic F5.3 - resolved SEO metadata, folded in by the `seo` primitive.
    # `exclude_if` keeps a None out of every dump, so pre-F5.3 specs serialize
    # byte-identically (and the 256 KiB cap math is unchanged for them).
    seo: SeoMeta | None = Field(default=None, exclude_if=lambda value: value is None)
    # Epic 5.3-lite blog — resolved static posts folded in by the `blog` primitive.
    # The generator lowers these into SPA routes + RSS. Absent means no blog files
    # and keeps legacy specs/trees byte-identical.
    blog: BlogMeta | None = Field(default=None, exclude_if=lambda value: value is None)
    # Epic 6.1 - resolved first-party analytics metadata, folded in by the
    # `analytics` primitive. Hidden when unset, matching the SEO additive pattern.
    analytics: AnalyticsMeta | None = Field(default=None, exclude_if=lambda value: value is None)
    # WO-F4.1 - non-secret Stripe data-plane metadata.  Absent metadata is
    # excluded so every unrelated AppSpec and generated tree stays byte-stable.
    stripe: StripeMeta | None = Field(default=None, exclude_if=lambda value: value is None)
    # WO-F3.3 - non-secret webhook data-plane metadata. Runtime targets and
    # signing keys remain in host-owned configuration/secret stores.
    webhooks: WebhookMeta | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("roles")
    @classmethod
    def _roles_are_safe_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for role in value:
            if not _IDENT_RE.match(role):
                raise ValueError(
                    "role names must be snake_case identifiers "
                    f"(pattern {_IDENT_RE.pattern!r}: lowercase, starts with a letter, "
                    f"only letters/digits/underscore), got {role!r}"
                )
        _require_unique(value, what="role")
        return value

    @model_validator(mode="after")
    def _ids_unique(self) -> AppSpec:
        _require_unique((p.id for p in self.pages), what="page id")
        _require_unique((p.route for p in self.pages), what="route")
        _require_unique((e.id for e in self.entities), what="entity id")
        _require_unique((a.id for a in self.primary_actions), what="primary action")
        return self

    @model_validator(mode="after")
    def _foreign_keys_valid(self) -> AppSpec:
        entity_ids = {e.id for e in self.entities}
        deps: dict[str, set[str]] = {}
        for entity in self.entities:
            refs = {f.references for f in entity.fields if f.references is not None}
            for ref in refs:
                if ref not in entity_ids:
                    raise ValueError(f"entity {entity.id!r} references unknown entity {ref!r}")
            deps[entity.id] = set(refs)

        emitted: set[str] = set()
        pending = dict(deps)
        while pending:
            ready = [entity_id for entity_id, refs in pending.items() if refs <= emitted]
            if not ready:
                cycle = ", ".join(sorted(pending))
                raise ValueError(f"entity foreign key cycle detected among: {cycle}")
            for entity_id in ready:
                emitted.add(entity_id)
                del pending[entity_id]
        return self

    @model_validator(mode="after")
    def _entity_roles_valid(self) -> AppSpec:
        declared = set(self.roles)
        for entity in self.entities:
            for role in (*entity.write_roles, *entity.read_roles):
                if not declared:
                    raise ValueError(
                        f"entity {entity.id!r} declares role {role!r} but AppSpec.roles is empty"
                    )
                if role not in declared:
                    raise ValueError(
                        f"entity {entity.id!r} declares unknown role {role!r}; "
                        f"known roles: {sorted(declared)}"
                    )
        return self


# ============================ DesignSpec =======================================


class Typography(BaseModel):
    """Declared fonts. A real declared family ("Inter", "Georgia") — never the
    non-answer "default" — so design_lint can see a choice was actually made."""

    model_config = _STRICT

    heading_font: _FontFamilyStr = Field(
        description="One primary font family name (letters, digits, and spaces); "
        "never a comma-separated CSS fallback stack."
    )
    body_font: _FontFamilyStr = Field(
        description="One primary font family name (letters, digits, and spaces); "
        "never a comma-separated CSS fallback stack."
    )

    @field_validator("heading_font", "body_font")
    @classmethod
    def _is_a_real_font(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("font must be a non-empty declared family")
        if stripped.lower() == "default":
            raise ValueError(
                "font must be a real declared family, not 'default' (declare the actual font)"
            )
        # Constrain to a safe font charset (letters/digits/spaces). Real web font
        # families fit this; it makes the name injection-proof in the CSS
        # font-family value, the Google Fonts URL, and the quoted CSS string the
        # generator emits it into (no quote/angle-bracket/&/?/: to break out with).
        if not _FONT_RE.match(stripped):
            raise ValueError(
                "font family must contain only letters, digits and spaces "
                f"(pattern {_FONT_RE.pattern!r}), got {value!r}"
            )
        return value


class Palette(BaseModel):
    """The core color roles. Hex only; `accent` is optional."""

    model_config = _STRICT

    primary: _HexStr
    surface: _HexStr
    text: _HexStr
    accent: _HexStr | None = None

    @field_validator("primary", "surface", "text")
    @classmethod
    def _required_hex(cls, value: str) -> str:
        return _validate_hex(value, field="palette color")

    @field_validator("accent")
    @classmethod
    def _optional_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, field="palette accent")


class Justification(BaseModel):
    """Why a DELIBERATE, non-default design choice was made.

    `reason` must be substantive (non-empty, >= a sane minimum length) so
    design_lint can distinguish an intentional decision from unjustified slop."""

    model_config = _STRICT

    choice: _ShortStr
    reason: _ReasonStr

    @field_validator("choice")
    @classmethod
    def _choice_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("justification 'choice' must be non-empty")
        return value

    @field_validator("reason")
    @classmethod
    def _reason_substantive(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < _MIN_REASON_LEN:
            raise ValueError(
                f"justification 'reason' must be substantive "
                f"(>= {_MIN_REASON_LEN} chars), got {len(stripped)}: {value!r}"
            )
        return value


class DesignSpec(BaseModel):
    """The DESIGN DECISIONS — descriptive, not a CSS AST. Feeds Epic D's
    design_lint, which uses `justifications` to tell intentional non-default
    choices from slop."""

    model_config = _STRICT

    schema_version: int = Field(ge=1)
    typography: Typography
    palette: Palette
    # Open descriptive vocabularies (sidebar/topnav/centered/split, flat/outlined/
    # soft, compact/comfortable/...). Kept as strings so the vocab can grow without
    # a schema bump; design_lint owns the semantic checks.
    layout_family: _ShortStr
    component_style: _ShortStr
    density: _ShortStr
    justifications: tuple[Justification, ...] = Field(
        default_factory=tuple, max_length=_MAX_JUSTIFICATIONS
    )


# ---- shared validation helper -------------------------------------------------


def _require_unique(ids: object, *, what: str) -> None:
    """Raise ValueError on the first duplicate in `ids` (an iterable of str)."""
    seen: set[str] = set()
    for item in ids:  # type: ignore[attr-defined]
        if item in seen:
            raise ValueError(f"duplicate {what}: {item!r}")
        seen.add(item)


# ============================ IO helpers =======================================
#
# Pure JSON read/write under the workspace's `.disco/` dir. Schema-validated on
# load. No new persistence API — `.disco/` rides the existing snapshot/rehydrate.
#
# Relocated VERBATIM to `spec_parts/io.py` to keep this module under the
# `python_or_harness_module_logical_gt_700` budget and imported back in at the
# TOP of this module (see the import block up top) — every name (including the
# underscore-prefixed ones some tests reach through the module object for) is
# re-exported here at its original path, so `from .spec import save_app_spec`
# (or `_MAX_SPEC_BYTES`, etc.) is unaffected.
