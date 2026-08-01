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

DECOMPOSITION: the actual emitters, tree-builders, and default-spec factories
live in `generator_parts/` (kept under the module/callable size budgets); this
module is the STABLE PUBLIC SEAM — the entry point (`generate`), the primitive
registry wiring, and every name a sibling primitive module or test reaches into
`.generator` for (including underscore-prefixed ones). See
`generator_parts/__init__.py` for the byte-identity contract the split holds.
"""

from __future__ import annotations

import importlib

from .generator_parts.api_client_ts import (
    _emit_api_client_ts as _emit_api_client_ts,
)
from .generator_parts.api_client_ts import (
    _emit_submit_hook_ts as _emit_submit_hook_ts,
)
from .generator_parts.app_shell import (
    _emit_app_tsx as _emit_app_tsx,
)
from .generator_parts.app_shell import (
    _emit_content_ts as _emit_content_ts,
)
from .generator_parts.app_shell import (
    _emit_index_html as _emit_index_html,
)
from .generator_parts.app_shell import (
    _emit_main_tsx as _emit_main_tsx,
)
from .generator_parts.app_shell import (
    _seo_abs_url as _seo_abs_url,
)
from .generator_parts.app_shell import (
    _seo_files as _seo_files,
)
from .generator_parts.components import _emit_component as _emit_component
from .generator_parts.d1_worker import (
    _db_name as _db_name,
)
from .generator_parts.d1_worker import (
    _drizzle_factory as _drizzle_factory,
)
from .generator_parts.d1_worker import (
    _emit_drizzle_config_ts as _emit_drizzle_config_ts,
)
from .generator_parts.d1_worker import (
    _emit_drizzle_schema_ts as _emit_drizzle_schema_ts,
)
from .generator_parts.d1_worker import (
    _emit_schema_sql as _emit_schema_sql,
)
from .generator_parts.d1_worker import (
    _emit_wrangler_toml as _emit_wrangler_toml,
)
from .generator_parts.d1_worker import (
    _sql_type as _sql_type,
)
from .generator_parts.d1_worker import (
    _table_name as _table_name,
)
from .generator_parts.design_tokens import (
    _emit_styles_css as _emit_styles_css,
)
from .generator_parts.design_tokens import (
    _variant_layout as _variant_layout,
)
from .generator_parts.directory_app import (
    _generate_directory,
    _identity_app_spec,
    default_directory_app_spec,
)
from .generator_parts.ids import (
    _comp_name as _comp_name,
)
from .generator_parts.ids import (
    _component_names as _component_names,
)
from .generator_parts.ids import (
    _html_text as _html_text,
)
from .generator_parts.ids import (
    _iter_sections as _iter_sections,
)
from .generator_parts.ids import (
    _pascal as _pascal,
)
from .generator_parts.ids import (
    _slug as _slug,
)
from .generator_parts.ids import (
    _ts as _ts,
)
from .generator_parts.lead_entity import (
    ensure_lead_entity,
    resolve_lead_entity,
    synthesized_lead_entity,
)
from .generator_parts.lead_gen_app import _generate_lead_gen, default_lead_gen_app_spec, generate
from .generator_parts.project_files import (
    _emit_dev_vars_example as _emit_dev_vars_example,
)
from .generator_parts.project_files import (
    _emit_gitignore as _emit_gitignore,
)
from .generator_parts.project_files import (
    _emit_manifest_ts as _emit_manifest_ts,
)
from .generator_parts.project_files import (
    _emit_package_json as _emit_package_json,
)
from .generator_parts.project_files import (
    _emit_package_lock_json as _emit_package_lock_json,
)
from .generator_parts.project_files import (
    _emit_tsconfig as _emit_tsconfig,
)
from .generator_parts.project_files import (
    _emit_vite_config as _emit_vite_config,
)
from .generator_parts.semantic_attrs import (
    _disco_field_attr as _disco_field_attr,
)
from .generator_parts.semantic_attrs import (
    _disco_section_attrs as _disco_section_attrs,
)
from .generator_parts.worker_ts import _emit_worker_ts as _emit_worker_ts

# ---- register the primitives (at import time, so the registry is populated before
# `generate` is ever called or the tools resolve a primitive). lead_gen FIRST so it
# is the fallback for any unrecognized app_kind. ------------------------------------
# Imported HERE (not at the top) on purpose: primitive_verify imports THIS module
# for the pure `resolve_lead_entity` (and the small `_comp_name`/`_component_names`/
# `_table_name`/`_ts` set, all already bound above), so the verify hooks can only be
# imported once those names exist — the same bottom-of-module dance as the
# importlib sibling-primitive imports below.
from .primitive_verify import directory_verify, lead_gen_verify  # noqa: E402
from .primitives import (
    DIRECTORY_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    PrimitiveDefinition,
    register_primitive,
)
from .primitives import (
    resolve_primitive as resolve_primitive,
)

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
importlib.import_module(".local_list_primitive", package=__package__)
importlib.import_module(".hello_primitive", package=__package__)
importlib.import_module(".stripe_primitive", package=__package__)
importlib.import_module(".form_primitive", package=__package__)
importlib.import_module(".seo_primitive", package=__package__)
importlib.import_module(".collection_primitive", package=__package__)
importlib.import_module(".analytics_primitive", package=__package__)
importlib.import_module(".blog_primitive", package=__package__)
importlib.import_module(".feature_flags_primitive", package=__package__)
importlib.import_module(".webhook_primitive", package=__package__)


__all__ = [
    "default_directory_app_spec",
    "default_lead_gen_app_spec",
    "ensure_lead_entity",
    "generate",
    "resolve_lead_entity",
    "synthesized_lead_entity",
]
