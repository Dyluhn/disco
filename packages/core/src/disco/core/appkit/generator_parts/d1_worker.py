"""The D1 / Worker side: schema.sql, the Drizzle schema/config, and wrangler.toml
for the lead-gen primitive (the resource-naming + migration layer `_emit_worker_ts`
sits on top of).

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

from ..spec import AppSpec, Entity
from .ids import _namespace, _namespaced, _slug, _ts

_SQL_TYPE: dict[str, str] = {
    "str": "TEXT",
    "string": "TEXT",
    "text": "TEXT",
    "datetime": "TEXT",
    "date": "TEXT",
    "email": "TEXT",
    "url": "TEXT",
    "int": "INTEGER",
    "integer": "INTEGER",
    "number": "INTEGER",
    "bool": "INTEGER",
    "boolean": "INTEGER",
    "float": "REAL",
}


def _sql_type(field_type: str) -> str:
    return _SQL_TYPE.get(field_type.strip().lower(), "TEXT")


def _table_name(lead: Entity) -> str:
    base = _slug(lead.id)
    # plural-ish table name; keep deterministic and SQL-safe.
    ident = base.replace("-", "_")
    return ident if ident.endswith("s") else ident + "s"


def _emit_schema_sql(lead: Entity) -> str:
    # Field names are spec-validated SAFE identifiers (EntityField._name_is_safe_
    # identifier), so the generator can trust them; we STILL double-quote every
    # identifier (belt-and-suspenders) so a reserved word can never break the DDL.
    table = _table_name(lead)
    cols = ['  "id" INTEGER PRIMARY KEY AUTOINCREMENT']
    for field in lead.fields:
        nullable = " NOT NULL" if field.required else ""
        cols.append(f'  "{field.name}" {_sql_type(field.type)}{nullable}')
    cols.append("  \"created_at\" TEXT NOT NULL DEFAULT (datetime('now'))")
    body = ",\n".join(cols)
    return (
        "-- Auto-generated D1 schema (Epic E). The ONE lead entity -> one table.\n"
        "-- Keep this migration in sync with src/db/schema.ts. Both files are generated\n"
        "-- from the same resolved lead entity, so they cannot drift by construction.\n"
        f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
        f"{body}\n"
        ");\n"
    )


def _drizzle_factory(field_type: str) -> str:
    """The drizzle-orm/sqlite-core column factory matching `_sql_type`.

    This intentionally lowers through the SAME SQL type map as `schema.sql` so the
    typed Drizzle table and the D1 migration are two renderings of one entity model.
    """
    sql_type = _sql_type(field_type)
    if sql_type == "INTEGER":
        return "integer"
    if sql_type == "REAL":
        return "real"
    return "text"


def _emit_drizzle_schema_ts(lead: Entity) -> str:
    """`src/db/schema.ts` - the typed Drizzle source of truth for the lead table."""
    table = _table_name(lead)
    cols = ['  id: integer("id").primaryKey({ autoIncrement: true }),']
    for field in lead.fields:
        chain = ".notNull()" if field.required else ""
        cols.append(f"  {field.name}: {_drizzle_factory(field.type)}({_ts(field.name)}){chain},")
    cols.append("  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),")
    return (
        "/* Auto-generated Drizzle schema - regenerated from .disco/appspec.json.\n"
        "   This typed table and schema.sql are lowered from the same resolved lead\n"
        "   entity, so the D1 migration and Drizzle layer cannot drift. */\n"
        'import { sql } from "drizzle-orm";\n'
        'import { integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";\n'
        "\n"
        f"export const leads = sqliteTable({_ts(table)}, {{\n" + "\n".join(cols) + "\n"
        "});\n"
    )


def _emit_drizzle_config_ts() -> str:
    return (
        'import { defineConfig } from "drizzle-kit";\n'
        "\n"
        "export default defineConfig({\n"
        '  dialect: "sqlite",\n'
        '  schema: "./src/db/schema.ts",\n'
        '  out: "./drizzle",\n'
        "});\n"
    )


def _worker_name(app: AppSpec) -> str:
    """The deterministic Cloudflare Worker name. NAMESPACED with `_namespace(app)` so
    two distinct apps that slug-collide on `app.name` get DISTINCT Worker names (no
    cross-app clobber on one account — SEC-10 defense-in-depth), while the same app
    always regenerates the same name (idempotent deploys)."""
    return _namespaced(_slug(app.name), _namespace(app))


def _db_name(app: AppSpec, lead: Entity) -> str:
    """The deterministic D1 database name shared by wrangler.toml, the package.json
    `db:*` scripts, and the OWNER_GUIDE — derived once so the binding and the owner's
    deploy commands can never disagree about which database to target. NAMESPACED
    with `_namespace(app)` (same rationale as `_worker_name`) so distinct apps never
    collide on one account, yet the same app stays idempotent."""
    return _namespaced(f"{_slug(app.name)}-{_table_name(lead)}", _namespace(app))


def _emit_wrangler_toml(app: AppSpec, lead: Entity) -> str:
    name = _worker_name(app)
    db_name = _db_name(app, lead)
    return (
        f'name = "{name}"\n'
        'compatibility_date = "2024-09-23"\n'
        'main = "worker/index.ts"\n'
        "\n"
        "# Workers Static Assets: serve the built SPA, falling back to index.html for\n"
        "# client routes (single-page-application not_found handling).\n"
        "[assets]\n"
        'directory = "./dist"\n'
        'binding = "ASSETS"\n'
        'not_found_handling = "single-page-application"\n'
        "# Worker-FIRST routing for the dynamic paths (Epic I): the lead API and the\n"
        "# admin read-back are handled by worker/index.ts, NOT the static-asset/SPA\n"
        "# layer. Without this, single-page-application not_found_handling can shadow a\n"
        "# navigation to /admin (or an /api/* read) with index.html so the Worker never\n"
        "# runs. Requires Wrangler v4.20+ (the array form of run_worker_first).\n"
        'run_worker_first = ["/api/*", "/admin"]\n'
        "\n"
        "# The D1 database the Worker inserts leads into. Replace database_id after\n"
        "# `wrangler d1 create`.\n"
        "[[d1_databases]]\n"
        'binding = "DB"\n'
        f'database_name = "{db_name}"\n'
        'database_id = "REPLACE_WITH_D1_DATABASE_ID"\n'
        "\n"
        "# OWNER ACTION REQUIRED — admin read-back auth.\n"
        "# The lead READS (GET /api/leads and /admin) are gated behind a Bearer token.\n"
        "# Set it as a Cloudflare SECRET (never hardcode it, never commit it):\n"
        "#   wrangler secret put ADMIN_TOKEN\n"
        "# Until ADMIN_TOKEN is set the worker FAILS CLOSED: every read is denied (401),\n"
        "# so leads are never exposed. POST /api/leads (public lead submission) needs\n"
        "# no token.\n"
    )
