"""Lowering the folded AppSpec into the generated form D1/Drizzle surface.

Extracted verbatim from ``form_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..primitives import RECORDS_PRIMITIVE_ID, resolve_primitive
from ..spec import AppSpec, Entity
from .fold import _entity_table
from .model import _KIND_BY_ENTITY_TYPE


def form_submission_entities_for(
    app: AppSpec, *, reserved_entities: tuple[Entity, ...] = ()
) -> tuple[Entity, ...]:
    """The app's form-submission entities, in entity order: entities targeted by a
    `form` section's `content_ref` (the fold's marker), excluding any reserved
    entities owned by the host primitive (for lead_gen, the resolved lead entity).
    Raises if two forms would lower to the same D1 table, or collide with a reserved
    entity's table (invalid tree otherwise)."""
    targets = {
        s.content_ref
        for p in app.pages
        for s in p.sections
        if s.kind == "form" and s.content_ref is not None
    }
    reserved_ids = {e.id for e in reserved_entities}
    forms = tuple(e for e in app.entities if e.id in targets and e.id not in reserved_ids)
    seen: dict[str, str] = {_entity_table(e): e.id for e in reserved_entities}
    for entity in forms:
        table = _entity_table(entity)
        if table in seen:
            raise ValueError(
                f"form entity {entity.id!r} lowers to table {table!r}, which entity "
                f"{seen[table]!r} already owns — rename the form"
            )
        seen[table] = entity.id
    return forms


def form_entities_for(app: AppSpec, lead: Entity) -> tuple[Entity, ...]:
    """Lead-gen compatibility wrapper: exclude the resolved lead entity so a form
    section pointing at the lead renders the classic capture form."""
    return form_submission_entities_for(app, reserved_entities=(lead,))


def form_route_for(app: AppSpec, entity: Entity) -> str:
    """The public POST route for one folded form entity.

    lead_gen and legacy lead-shaped apps keep the original `/api/<table>` route.
    records apps use `/api/forms/<form_id>` so records retains exclusive ownership
    of its `/api/<table>` CRUD route table.
    """
    base = resolve_primitive(app.app_kind)
    if base.id == RECORDS_PRIMITIVE_ID:
        return f"/api/forms/{entity.id}"
    return f"/api/{_entity_table(entity)}"


def _field_kind(field_type: str) -> str:
    """The FormField kind a folded entity field's type maps back to (`text` for any
    unrecognized hand-authored type — the safe string fallback)."""
    return _KIND_BY_ENTITY_TYPE.get(field_type.strip().lower(), "text")


def emit_form_schema_sql(forms: tuple[Entity, ...]) -> str:
    """The form submissions table blocks that can be appended to any D1-backed
    host primitive. Empty `forms` → empty string."""
    from ..generator import _sql_type, _table_name

    blocks: list[str] = []
    for entity in forms:
        cols = ['  "id" INTEGER PRIMARY KEY AUTOINCREMENT']
        for field in entity.fields:
            nullable = " NOT NULL" if field.required else ""
            cols.append(f'  "{field.name}" {_sql_type(field.type)}{nullable}')
        cols.append("  \"created_at\" TEXT NOT NULL DEFAULT (datetime('now'))")
        body = ",\n".join(cols)
        blocks.append(
            f"-- Form primitive (F3.1): submissions table for the {entity.id!r} form.\n"
            f'CREATE TABLE IF NOT EXISTS "{_table_name(entity)}" (\n'
            f"{body}\n"
            ");\n"
        )
    return "\n".join(blocks)


def lower_form_schema_sql(lead: Entity, forms: tuple[Entity, ...]) -> str:
    """`schema.sql`: the lead table (byte-identical base emitter) plus one
    submissions table per form. Empty `forms` → the base output, unchanged."""
    from ..generator import _emit_schema_sql

    base = _emit_schema_sql(lead)
    if not forms:
        return base
    return "\n".join([base, emit_form_schema_sql(forms)])


def _form_const_name(entity: Entity) -> str:
    """The Drizzle export const for a form's table. The `form_` prefix keeps it a
    valid, non-reserved TS identifier that can never collide with `leads`."""
    return f"form_{_entity_table(entity)}"


def emit_form_drizzle_ts(forms: tuple[Entity, ...]) -> str:
    """The Drizzle sqliteTable exports for folded forms. Empty `forms` → empty string."""
    from ..generator import _drizzle_factory, _table_name, _ts

    blocks: list[str] = []
    for entity in forms:
        cols = ['  id: integer("id").primaryKey({ autoIncrement: true }),']
        for field in entity.fields:
            chain = ".notNull()" if field.required else ""
            cols.append(
                f"  {field.name}: {_drizzle_factory(field.type)}({_ts(field.name)}){chain},"
            )
        cols.append("  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),")
        blocks.append(
            f"/* Form primitive (F3.1): submissions table for the {entity.id!r} form. */\n"
            f"export const {_form_const_name(entity)} = "
            f"sqliteTable({_ts(_table_name(entity))}, {{\n" + "\n".join(cols) + "\n});\n"
        )
    return "\n".join(blocks)


def lower_form_drizzle_ts(lead: Entity, forms: tuple[Entity, ...]) -> str:
    """`src/db/schema.ts`: the lead table (byte-identical base emitter) plus one
    Drizzle sqliteTable export per form. Empty `forms` → the base output."""
    from ..generator import _emit_drizzle_schema_ts

    base = _emit_drizzle_schema_ts(lead)
    if not forms:
        return base
    return "\n".join([base, emit_form_drizzle_ts(forms)])
