"""Drizzle ``src/db/schema.ts`` emitters for the records primitive.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import Entity
from .naming import _fk_order_entities, _records_table_name, _ts_const_names


def _emit_records_drizzle_ts(entities: tuple[Entity, ...]) -> str:
    from ..generator import _drizzle_factory, _ts

    ordered = _fk_order_entities(entities)
    tables = {entity.id: _records_table_name(entity) for entity in entities}
    consts = _ts_const_names(entities)
    blocks: list[str] = []
    for entity in ordered:
        cols = ['  id: integer("id").primaryKey({ autoIncrement: true }),']
        for field in entity.fields:
            if field.references is not None:
                factory = "integer"
                chain = f".references(() => {consts[field.references]}.id)"
            else:
                factory = _drizzle_factory(field.type)
                chain = ""
            if field.required:
                chain += ".notNull()"
            cols.append(f"  {field.name}: {factory}({_ts(field.name)}){chain},")
        cols.append("  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),")
        blocks.append(
            f"export const {consts[entity.id]} = sqliteTable({_ts(tables[entity.id])}, {{\n"
            + "\n".join(cols)
            + "\n});"
        )
    return (
        "/* Auto-generated Drizzle schema - regenerated from .disco/appspec.json. */\n"
        'import { sql } from "drizzle-orm";\n'
        'import { integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";\n'
        "\n" + "\n\n".join(blocks) + "\n"
    )


def _emit_auth_tables_drizzle_ts() -> str:
    return (
        'export const users = sqliteTable("users", {\n'
        '  id: integer("id").primaryKey({ autoIncrement: true }),\n'
        '  email: text("email").notNull().unique(),\n'
        '  password_hash: text("password_hash").notNull(),\n'
        '  password_salt: text("password_salt").notNull(),\n'
        '  role: text("role").notNull(),\n'
        "  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});\n"
        "\n"
        'export const sessions = sqliteTable("sessions", {\n'
        '  id: integer("id").primaryKey({ autoIncrement: true }),\n'
        '  token_hash: text("token_hash").notNull().unique(),\n'
        '  user_id: integer("user_id").notNull().references(() => users.id),\n'
        '  expires_at: text("expires_at").notNull(),\n'
        "  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});"
    )


def _emit_records_auth_drizzle_ts(entities: tuple[Entity, ...]) -> str:
    user_tables = _emit_records_drizzle_ts(entities)
    body = user_tables.split("\n", 4)[4]
    return (
        "/* Auto-generated Drizzle schema - regenerated from .disco/appspec.json. */\n"
        'import { sql } from "drizzle-orm";\n'
        'import { integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";\n'
        "\n" + _emit_auth_tables_drizzle_ts() + "\n\n" + body
    )
