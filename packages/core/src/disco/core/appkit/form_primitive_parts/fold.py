"""FormSpec -> AppSpec fold (`apply_form_spec`) for the `form` primitive.

Extracted verbatim from ``form_primitive.py`` to reduce module size, and
decomposed (PY-0378/PY-0379) into cohesive validation clusters + the fold JSON
builder. The parent module re-imports every name here unchanged (see its
module docstring).
"""

from __future__ import annotations

from pydantic import BaseModel

from ..primitives import (
    FORM_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    RECORDS_PRIMITIVE_ID,
    PrimitiveDefinition,
    resolve_primitive,
)
from ..spec import AppSpec, Entity
from .model import _ENTITY_TYPE_BY_KIND, FormSpec


def _entity_table(entity: Entity) -> str:
    from ..generator import _table_name

    return _table_name(entity)


def _resolve_form_host(app: AppSpec) -> PrimitiveDefinition:
    """Resolve + validate the host primitive a FormSpec may fold into."""
    base = resolve_primitive(app.app_kind)
    if base.id not in {LEAD_GEN_PRIMITIVE_ID, RECORDS_PRIMITIVE_ID}:
        if base.id == "directory":
            raise ValueError(
                "the form primitive cannot fold into directory apps yet: directory is "
                "a static site with no D1 binding, schema.sql data plane, or dynamic "
                "submission Worker route. Supported hosts are lead_gen-shaped apps and "
                "records apps; directory support must first upgrade the app shape to emit "
                "a Worker + D1 schema for the form instead of shipping a build that cannot "
                "verify or deploy."
            )
        if base.id == "hello":
            raise ValueError(
                "the form primitive cannot fold into hello apps: hello is the "
                "mount-proof minimal primitive, not a Cloudflare/D1 host. Create a "
                "lead_gen or records app first, then add the form primitive."
            )
        raise ValueError(
            f"the form primitive can fold only into lead_gen-shaped apps or records "
            f"apps; this app's app_kind {app.app_kind!r} lowers through {base.id!r}."
        )
    return base


def _resolve_form_target_page(app: AppSpec, spec: FormSpec) -> str:
    """The page id the form section is placed on."""
    if not app.pages:
        raise ValueError("the app has no pages to place the form section on")
    page_ids = [p.id for p in app.pages]
    if spec.page_id is None:
        return page_ids[0]
    if spec.page_id in page_ids:
        return spec.page_id
    raise ValueError(f"unknown page_id {spec.page_id!r}; known page ids: {', '.join(page_ids)}")


def _check_form_collisions(
    app: AppSpec, spec: FormSpec, base: PrimitiveDefinition, target_page_id: str
) -> None:
    """Raise if the form's derived ids/table collide with anything already in
    the app."""
    if any(e.id == spec.form_id for e in app.entities):
        raise ValueError(
            f"an entity with id {spec.form_id!r} already exists in this app — "
            "the form was likely already added; pick another form_id"
        )
    table = _entity_table(Entity(id=spec.form_id, name=spec.title, fields=()))
    taken_tables = {_entity_table(e) for e in app.entities}
    if base.id == LEAD_GEN_PRIMITIVE_ID:
        from ..generator import resolve_lead_entity

        taken_tables.add(_entity_table(resolve_lead_entity(app)))
    if table in taken_tables:
        raise ValueError(
            f"the form's submissions table {table!r} collides with an existing "
            "entity's table — pick another form_id"
        )
    target_page = next(p for p in app.pages if p.id == target_page_id)
    if any(s.id == spec.form_id for s in target_page.sections):
        raise ValueError(
            f"a section with id {spec.form_id!r} already exists on page "
            f"{target_page_id!r} — pick another form_id"
        )
    action_id = f"submit_{spec.form_id}"
    if any(a.id == action_id for a in app.primary_actions):
        raise ValueError(f"a primary action with id {action_id!r} already exists")


def _build_form_fold_json(
    spec: FormSpec, action_id: str
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """The three JSON fragments folded into the AppSpec dump: the submissions
    entity, the `form` section, and the `submit` primary action."""
    entity_json: dict[str, object] = {
        "id": spec.form_id,
        "name": spec.title,
        "fields": [
            {
                "name": f.name,
                "type": _ENTITY_TYPE_BY_KIND[f.kind],
                "required": f.required,
                "label": f.label,
            }
            for f in spec.fields
        ],
    }
    section_json: dict[str, object] = {
        "id": spec.form_id,
        "kind": "form",
        "content_ref": spec.form_id,
        "content": {
            "heading": spec.title,
            "cta_label": "Submit",
            "success_message": spec.success_message,
        },
    }
    action_json: dict[str, object] = {
        "id": action_id,
        "label": spec.title,
        "type": "submit",
        "target": spec.form_id,
    }
    return entity_json, section_json, action_json


def apply_form_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated FormSpec into the AppSpec (the house dance: dump → mutate →
    re-validate). Appends the submissions Entity, a `form` Section wired to it via
    `content_ref` (inserted before a trailing footer), and a `submit` primary
    action. Raises ValueError with actionable guidance on any refusal —
    `app_add_primitive` converts raised errors into model-facing refusals."""
    if not isinstance(spec, FormSpec):  # defensive: app_add_primitive validated it
        raise TypeError(f"apply_spec for {FORM_PRIMITIVE_ID!r} needs a FormSpec")

    base = _resolve_form_host(app)
    target_page_id = _resolve_form_target_page(app, spec)
    _check_form_collisions(app, spec, base, target_page_id)

    action_id = f"submit_{spec.form_id}"
    entity_json, section_json, action_json = _build_form_fold_json(spec, action_id)

    data = app.model_dump(mode="json")
    data["entities"] = [*data["entities"], entity_json]
    for page in data["pages"]:
        if page["id"] != target_page_id:
            continue
        sections = list(page["sections"])
        insert_at = len(sections)
        if sections and sections[-1].get("kind") == "footer":
            insert_at -= 1  # keep a trailing footer last
        sections.insert(insert_at, section_json)
        page["sections"] = sections
    data["primary_actions"] = [*data["primary_actions"], action_json]
    return AppSpec.model_validate(data)
