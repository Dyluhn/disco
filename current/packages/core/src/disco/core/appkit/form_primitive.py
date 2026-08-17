"""The AppKit `form` primitive (Epic F3.1, scaffold half): a declarative,
validated form that folds into an existing Cloudflare/D1 app.

An ADD-ON primitive, not a base scaffold: `app_add_primitive` validates a
`FormSpec`, `apply_form_spec` folds it into the AppSpec (a submissions Entity +
a `form` Section wired to it via `content_ref` + a `submit` primary action), and
the HOST app's own base primitive regenerates the whole tree from the folded
spec. The lowering rides the EXISTING lead_gen emitters: `lower_form_*` wrap
`_emit_schema_sql` / `_emit_worker_ts` / `_emit_drizzle_schema_ts` and are
byte-identical pass-throughs when the app declares no forms — the three
pre-existing primitives' `generate()` output is unchanged by construction.

The generated surface per form:

* a D1 submissions table in `schema.sql` (+ the matching Drizzle table in
  `src/db/schema.ts`);
* a Worker submission route whose validation MIRRORS the declared field kinds +
  required flags (422 on any mismatch — the generalization of lead_gen's capture
  route, which keeps its own byte-identical 400 contract);
* a React form component with one control per field (text/email/textarea/
  number/checkbox), inline required validation, and the spec'd success message.

Host scope (deliberate): `lead_gen` and legacy fallback kinds keep the original
`POST /api/<table>` form route; `records` apps use `POST /api/forms/<form_id>` so
records keeps exclusive ownership of `/api/<table>` CRUD routes. `directory` is
still refused because it is a static site with no D1/Worker data plane; silently
adding a form there would require upgrading the app shape and all current/deploy/verify
contracts, not just folding a section. `hello` is also refused: it is the
mount-proof primitive.

SECURITY SCOPE — READ THIS: this scaffold has NO spam protection, NO captcha,
NO rate limiting, and NO upload handling. The `template_only` tier upgrade and
the adversarial spam/upload harness are a SEPARATE, security-classed work
order; until that lands the primitive stays `tier="fillable"` and the generated
endpoint must not be treated as hardened.

Layering: `disco.core` is the leaf package (.importlinter). This module imports
pydantic + the stdlib + sibling core modules only. `generator.py` force-imports
it at the end of its module body (like records/hello), so the registry is
populated before anything generates; generator internals are imported lazily
inside functions (the records_primitive convention) to keep import order a
non-issue.

Implementation note: the declarative spec models, the FormSpec -> AppSpec fold,
the lowering into the D1/Drizzle/Worker surface, and the React form component
emitter live under `form_primitive_parts/` (module-size split); this module
re-imports every name unchanged and is the sole public-facing surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .form_primitive_parts.component import emit_app_form_component
from .form_primitive_parts.fold import apply_form_spec
from .form_primitive_parts.lowering import (
    _form_const_name as _form_const_name,
)
from .form_primitive_parts.lowering import (
    emit_form_drizzle_ts,
    emit_form_schema_sql,
    form_entities_for,
    form_route_for,
    form_submission_entities_for,
    lower_form_drizzle_ts,
    lower_form_schema_sql,
)
from .form_primitive_parts.model import (
    FormField,
    FormSpec,
)
from .form_primitive_parts.worker import (
    lower_form_records_worker_ts,
    lower_form_worker_ts,
)
from .primitives import (
    FORM_PRIMITIVE_ID,
    PrimitiveDefinition,
    register_primitive,
)
from .spec import (
    AppSpec,
    DesignSpec,
)

if TYPE_CHECKING:
    from .recipes import SiteRecipe

# Bare re-annotation (no rebinding — the objects still live in
# form_primitive_parts.model): restores this module's `__annotations__` entry
# for the two names that carried an annotated assignment before the split.
_ENTITY_TYPE_BY_KIND: dict[str, str]
_KIND_BY_ENTITY_TYPE: dict[str, str]


# ---- registration -----------------------------------------------------------------

from .primitive_verify import form_verify  # noqa: E402


def default_form_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The form primitive is an ADD-ON, never a base scaffold — `app_create` must
    not scaffold it. Raising here (the executor maps it to a failed tool result)
    keeps the affordance honest instead of silently aliasing to lead_gen."""
    del name, recipe
    raise ValueError(
        "the 'form' primitive is an ADD-ON, not a base scaffold: app_create a base "
        "app first (e.g. primitive_id='lead_gen'), then "
        "app_add_primitive(primitive_id='form', spec={...})."
    )


def prepare_form_app_spec(app: AppSpec) -> AppSpec:
    """Identity — the form primitive never owns a base app to normalize."""
    return app


def generate_form(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Never called: the HOST app's base primitive regenerates the whole tree from
    the folded AppSpec (the WO-A1 fold-into-AppSpec contract)."""
    del app, design
    raise ValueError(
        "the 'form' primitive does not generate a tree of its own; the host app's "
        "base primitive regenerates from the folded AppSpec."
    )


register_primitive(
    PrimitiveDefinition(
        id=FORM_PRIMITIVE_ID,
        default_app_spec=default_form_app_spec,
        prepare_app_spec=prepare_form_app_spec,
        generate=generate_form,
        tier="fillable",
        host_contract=(),
        spec_schema=FormSpec,
        verify=form_verify,
        apply_spec=apply_form_spec,
    )
)


__all__ = [
    "FORM_PRIMITIVE_ID",
    "FormField",
    "FormSpec",
    "apply_form_spec",
    "emit_form_drizzle_ts",
    "emit_form_schema_sql",
    "emit_app_form_component",
    "form_entities_for",
    "form_route_for",
    "form_submission_entities_for",
    "form_verify",
    "lower_form_drizzle_ts",
    "lower_form_records_worker_ts",
    "lower_form_schema_sql",
    "lower_form_worker_ts",
]
