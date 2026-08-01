"""The public entry point (`generate`) and the LEAD-GEN primitive's tree builder
+ default AppSpec (Epic E).

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

from .. import generator as _generator
from ..primitives import RECORDS_PRIMITIVE_ID
from ..recipes import SiteRecipe
from ..spec import Action, AppSpec, DesignSpec, Page, Section, SectionContent
from .api_client_ts import _emit_api_client_ts, _emit_submit_hook_ts
from .app_shell import _emit_app_tsx, _emit_content_ts, _emit_index_html, _emit_main_tsx, _seo_files
from .components import _emit_component
from .d1_worker import _db_name, _emit_drizzle_config_ts, _emit_wrangler_toml
from .design_tokens import _emit_styles_css
from .host_service_shim import _apply_host_services_shim
from .ids import _comp_name, _component_names, _iter_sections
from .lead_entity import _DEFAULT_LEAD_ID, resolve_lead_entity, synthesized_lead_entity
from .owner_guide import _emit_owner_guide_md
from .project_files import (
    _emit_dev_vars_example,
    _emit_gitignore,
    _emit_manifest_ts,
    _emit_package_json,
    _emit_package_lock_json,
    _emit_tsconfig,
    _emit_vite_config,
)

# ---- the entry point ----------------------------------------------------------


def generate(app_spec: AppSpec, design_spec: DesignSpec) -> dict[str, str]:
    """Lower the two specs into a complete AppKit Cloudflare app tree.

    DETERMINISTIC: identical specs → byte-identical `{path: contents}` with each
    primitive retaining ownership of its file order. No IO, clock, or randomness.
    DISPATCHES on the resolved PRIMITIVE
    (`AppSpec.app_kind`): `lead_gen` (the default + the fallback for any
    unrecognized kind) lowers to the lead-capture app EXACTLY as Epic E did;
    `directory` lowers to the static multi-route directory site (Epic N). The
    per-primitive `generate` callable owns the shape; the shared emitters below are
    reused across primitives.

    When the resolved primitive declares a non-empty ``host_contract`` the tree is
    extended with a ``worker/disco-client.ts`` shim and the Worker ``Env``
    interface is augmented — see ``_apply_host_services_shim``."""
    # Resolved through the PARENT module at call time (`_generator.resolve_primitive`,
    # never a captured `from ..primitives import resolve_primitive`): tests patch
    # `disco.core.appkit.generator.resolve_primitive` to install fixture primitives,
    # and a name bound at import time would silently miss that patch.
    prim = _generator.resolve_primitive(app_spec.app_kind)
    if app_spec.stripe is not None and app_spec.app_kind != RECORDS_PRIMITIVE_ID:
        raise ValueError("Stripe can only be generated on the auth-capable records primitive")
    if app_spec.webhooks is not None and app_spec.app_kind != RECORDS_PRIMITIVE_ID:
        raise ValueError("Webhooks can only be generated on the auth-capable records primitive")
    tree = prim.generate(app_spec, design_spec)
    standalone_pending_webhook = prim.id == "webhook" and app_spec.webhooks is None
    if (
        (prim.host_contract and not standalone_pending_webhook)
        or app_spec.stripe is not None
        or app_spec.webhooks is not None
    ):
        _apply_host_services_shim(tree)
    return tree


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
    # `..generator`, so it is always loaded by the time generate() runs.
    from ..analytics_primitive import (
        emit_analytics_dashboard_component,
        is_analytics_dashboard_section,
        lower_analytics_app_tsx,
        lower_analytics_main_tsx,
        lower_analytics_schema_sql,
        lower_analytics_worker_ts,
    )
    from ..blog_primitive import emit_app_tsx_with_blog_routes, emit_blog_files, has_blog
    from ..feature_flags_primitive import (
        emit_feature_flags_admin_component,
        emit_feature_flags_hook_ts,
        feature_flags_for,
        is_feature_flags_admin_section,
        lower_feature_flags_drizzle_ts,
        lower_feature_flags_schema_sql,
        lower_feature_flags_styles_css,
        lower_feature_flags_worker_ts,
    )
    from ..form_primitive import (
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
        "package-lock.json": _emit_package_lock_json(app_spec),
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
        "src/styles.css": lower_feature_flags_styles_css(_emit_styles_css(design_spec), flags),
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
            files[f"src/components/{comp}.tsx"] = emit_analytics_dashboard_component(comp, section)
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
