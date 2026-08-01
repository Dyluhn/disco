"""D1 ``schema.sql`` emitters for the records primitive.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import Entity
from .naming import _fk_order_entities, _records_table_name


def _emit_records_schema_sql(entities: tuple[Entity, ...]) -> str:
    from ..generator import _sql_type

    ordered = _fk_order_entities(entities)
    tables = {entity.id: _records_table_name(entity) for entity in entities}
    blocks: list[str] = []
    for entity in ordered:
        cols = ['  "id" INTEGER PRIMARY KEY AUTOINCREMENT']
        constraints: list[str] = []
        for field in entity.fields:
            nullable = " NOT NULL" if field.required else ""
            col_type = "INTEGER" if field.references is not None else _sql_type(field.type)
            cols.append(f'  "{field.name}" {col_type}{nullable}')
            if field.references is not None:
                target = tables[field.references]
                constraints.append(f'  FOREIGN KEY("{field.name}") REFERENCES "{target}"("id")')
        cols.append("  \"created_at\" TEXT NOT NULL DEFAULT (datetime('now'))")
        cols.extend(constraints)
        body = ",\n".join(cols)
        blocks.append(f'CREATE TABLE IF NOT EXISTS "{tables[entity.id]}" (\n{body}\n);')
    return (
        "-- Auto-generated D1 schema (records primitive).\n"
        "-- Keep this migration in sync with src/db/schema.ts.\n" + "\n\n".join(blocks) + "\n"
    )


def _emit_auth_tables_schema_sql() -> str:
    return (
        'CREATE TABLE IF NOT EXISTS "users" (\n'
        '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  "email" TEXT NOT NULL UNIQUE,\n'
        '  "password_hash" TEXT NOT NULL,\n'
        '  "password_salt" TEXT NOT NULL,\n'
        '  "role" TEXT NOT NULL,\n'
        "  \"created_at\" TEXT NOT NULL DEFAULT(datetime('now'))\n"
        ");\n"
        "\n"
        'CREATE TABLE IF NOT EXISTS "sessions" (\n'
        '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  "token_hash" TEXT NOT NULL UNIQUE,\n'
        '  "user_id" INTEGER NOT NULL,\n'
        '  "expires_at" TEXT NOT NULL,\n'
        "  \"created_at\" TEXT NOT NULL DEFAULT(datetime('now')),\n"
        '  FOREIGN KEY("user_id") REFERENCES "users"("id")\n'
        ");"
    )


def _emit_records_auth_schema_sql(entities: tuple[Entity, ...]) -> str:
    base = _emit_records_schema_sql(entities)
    body = base.split("\n", 2)[2]
    return (
        "-- Auto-generated D1 schema (records primitive).\n"
        "-- Keep this migration in sync with src/db/schema.ts.\n"
        + _emit_auth_tables_schema_sql()
        + "\n\n"
        + body
    )
