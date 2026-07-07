"""AppKit — structured scaffolding for the Build surface.

EPIC B (Build Brief): a deterministic, non-LLM classifier that distills a
free-text build request into a small structured `BuildBrief`. See `build_brief`.

EPIC C (App & Design specs): the structured contracts the scaffold generator
consumes — `AppSpec` (app structure) and `DesignSpec` (descriptive design
decisions, with justified non-defaults), plus pure JSON/`.disco/` IO helpers.
See `spec`.

EPIC D (Design recipes + section catalog): `SiteRecipe` — coherent, NON-default
design systems that lower to justified `DesignSpec`s (`recipes`) — and the
`SectionVariant` catalog of >= 2 layouts per section kind (`section_catalog`),
so generated sites stop collapsing to the centered-hero/3-cards/cta median. The
matching slop scanner (`design_lint`) lives in the tools layer.
"""

from __future__ import annotations

from .build_brief import BuildBrief, classify_build_brief
from .generator import (
    default_directory_app_spec,
    default_lead_gen_app_spec,
    ensure_lead_entity,
    generate,
    resolve_lead_entity,
    synthesized_lead_entity,
)
from .local_verify import (
    CF_EXPORT_FILES,
    STATIC_CF_EXPORT_FILES,
    CheckResult,
    WorkerAuthModel,
    check_drizzle_schema,
    check_schema_sql,
    cloudflare_export_ready,
    cloudflare_export_ready_static,
    local_api_roundtrip,
)
from .primitives import (
    ANALYTICS_PRIMITIVE_ID,
    COLLECTION_PRIMITIVE_ID,
    DIRECTORY_PRIMITIVE_ID,
    FORM_PRIMITIVE_ID,
    HELLO_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    RECORDS_PRIMITIVE_ID,
    SEO_PRIMITIVE_ID,
    PrimitiveDefinition,
    get_primitive,
    primitive_ids,
    resolve_primitive,
)
from .recipes import (
    CANONICAL_CHOICE_KEYS,
    CHOICE_COMPONENT_BUTTON_RADIUS,
    CHOICE_COMPONENT_STYLE,
    CHOICE_DENSITY,
    CHOICE_EFFECTS_GLOW,
    CHOICE_EFFECTS_GRADIENT_TEXT,
    CHOICE_ICONS_STYLE,
    CHOICE_LAYOUT_FAMILY,
    CHOICE_LAYOUT_SECTION_SEQUENCE,
    CHOICE_MOTION_DENSITY,
    CHOICE_PALETTE_ACCENT,
    CHOICE_PALETTE_PRIMARY,
    CHOICE_PALETTE_SURFACE,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
    RECIPES,
    SectionVariantPreference,
    SiteRecipe,
    get_recipe,
    recipe_ids,
)
from .records_primitive import (
    default_records_app_spec,
    default_records_auth_app_spec,
    generate_records,
    prepare_records_app_spec,
)
from .section_catalog import (
    COVERED_KINDS,
    SECTION_VARIANTS,
    SectionVariant,
    get_variant,
    variant_ids,
    variants_for,
)
from .snapshot import (
    spec_digest,
    summarize_specs,
    tree_digest,
    tree_file_hashes,
)
from .spec import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    MAX_DESIGNSPEC_BYTES,
    Action,
    AnalyticsMeta,
    DesignSpec,
    Entity,
    EntityField,
    Justification,
    Page,
    Palette,
    Section,
    SectionContent,
    SectionKind,
    SeoMeta,
    Typography,
    appspec_path,
    designspec_path,
    load_app_spec,
    load_app_spec_from_bytes,
    load_design_spec,
    load_design_spec_from_bytes,
    load_specs,
    save_app_spec,
    save_design_spec,
    serialize_app_spec,
    serialize_design_spec,
)

__all__ = [
    "APPSPEC_RELPATH",
    "ANALYTICS_PRIMITIVE_ID",
    "DESIGNSPEC_RELPATH",
    "CANONICAL_CHOICE_KEYS",
    "CF_EXPORT_FILES",
    "CHOICE_COMPONENT_BUTTON_RADIUS",
    "CHOICE_COMPONENT_STYLE",
    "CHOICE_DENSITY",
    "CHOICE_EFFECTS_GLOW",
    "CHOICE_EFFECTS_GRADIENT_TEXT",
    "CHOICE_ICONS_STYLE",
    "CHOICE_LAYOUT_FAMILY",
    "CHOICE_LAYOUT_SECTION_SEQUENCE",
    "CHOICE_MOTION_DENSITY",
    "CHOICE_PALETTE_ACCENT",
    "CHOICE_PALETTE_PRIMARY",
    "CHOICE_PALETTE_SURFACE",
    "CHOICE_TYPOGRAPHY_BODY",
    "CHOICE_TYPOGRAPHY_HEADING",
    "COLLECTION_PRIMITIVE_ID",
    "COVERED_KINDS",
    "DIRECTORY_PRIMITIVE_ID",
    "FORM_PRIMITIVE_ID",
    "HELLO_PRIMITIVE_ID",
    "LEAD_GEN_PRIMITIVE_ID",
    "RECORDS_PRIMITIVE_ID",
    "MAX_DESIGNSPEC_BYTES",
    "RECIPES",
    "SECTION_VARIANTS",
    "SEO_PRIMITIVE_ID",
    "STATIC_CF_EXPORT_FILES",
    "Action",
    "AnalyticsMeta",
    "BuildBrief",
    "CheckResult",
    "DesignSpec",
    "PrimitiveDefinition",
    "Entity",
    "EntityField",
    "Justification",
    "Page",
    "Palette",
    "Section",
    "SectionContent",
    "SectionKind",
    "SectionVariant",
    "SectionVariantPreference",
    "SeoMeta",
    "SiteRecipe",
    "Typography",
    "WorkerAuthModel",
    "appspec_path",
    "check_drizzle_schema",
    "check_schema_sql",
    "classify_build_brief",
    "cloudflare_export_ready",
    "cloudflare_export_ready_static",
    "default_directory_app_spec",
    "default_lead_gen_app_spec",
    "default_records_auth_app_spec",
    "default_records_app_spec",
    "designspec_path",
    "ensure_lead_entity",
    "local_api_roundtrip",
    "generate",
    "generate_records",
    "get_primitive",
    "get_recipe",
    "get_variant",
    "primitive_ids",
    "prepare_records_app_spec",
    "resolve_primitive",
    "load_app_spec",
    "load_app_spec_from_bytes",
    "load_design_spec",
    "load_design_spec_from_bytes",
    "load_specs",
    "recipe_ids",
    "resolve_lead_entity",
    "save_app_spec",
    "save_design_spec",
    "serialize_app_spec",
    "serialize_design_spec",
    "spec_digest",
    "summarize_specs",
    "synthesized_lead_entity",
    "tree_digest",
    "tree_file_hashes",
    "variant_ids",
    "variants_for",
]

# ---------------------------------------------------------------------------
# Legacy P4 surface (pre-port). The v1 AppSpec/renderer still power the current
# app_* tools + kits until B2 of the gap-close plan swaps them to the ported
# engine above; the root `AppSpec` name stays bound to v1 for that transition
# (the ported v2 spec is imported explicitly: `from disco.core.appkit.spec
# import AppSpec`).
from .models import (
    DEFAULT_DESIGN,
    SECTION_KINDS,
    AppSection,
    AppSpec,
    render_html,
)

__all__ += [
    "DEFAULT_DESIGN",
    "SECTION_KINDS",
    "AppSection",
    "AppSpec",
    "render_html",
]
