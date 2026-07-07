"""The AppKit `records` primitive: related entities + per-entity CRUD routes.

This primitive deliberately lives beside lead-gen/directory instead of widening the
lead-gen path into a generic CRUD DSL. It reuses the generator's shared
shape-agnostic emitters and owns only the multi-table schema, Drizzle schema, and
N-entity Worker surface.
"""

from __future__ import annotations

import hashlib
import json
import re

from .primitives import RECORDS_PRIMITIVE_ID, PrimitiveDefinition, register_primitive
from .recipes import SiteRecipe
from .spec import (
    Action,
    AppSpec,
    DesignSpec,
    Entity,
    EntityField,
    Page,
    Section,
    SectionContent,
)

_TS_RESERVED = frozenset(
    {
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "export",
        "extends",
        "finally",
        "for",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "new",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)


def default_records_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """A default two-entity records app that proves a real FK relation."""
    title = name.strip() or "Team Records"
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    sections = (
        Section(
            id="hero",
            kind="hero",
            variant_id=prefs.get("hero"),
            content=SectionContent(
                heading=title,
                subheading="Track team members and the shifts assigned to them.",
            ),
        ),
        Section(
            id="records",
            kind="list",
            variant_id=prefs.get("list"),
            content=SectionContent(
                heading="Records",
                items=("Team members", "Shifts", "Member assignments"),
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
        app_kind=RECORDS_PRIMITIVE_ID,
        name=title,
        pages=(Page(id="home", route="/", title="Home", sections=sections),),
        entities=(
            Entity(
                id="team_member",
                name="Team Member",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="email", type="email", required=True),
                ),
            ),
            Entity(
                id="shift",
                name="Shift",
                fields=(
                    EntityField(name="title", type="str", required=True),
                    EntityField(name="starts_at", type="datetime", required=True),
                    EntityField(
                        name="member_id",
                        type="int",
                        required=True,
                        references="team_member",
                    ),
                ),
            ),
        ),
        primary_actions=(
            Action(id="view_records", label="View records", type="nav", target="/"),
        ),
    )


def default_records_auth_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """A default RBAC records app: members request time off, approvers approve."""
    title = name.strip() or "Shift Calendar"
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    sections = (
        Section(
            id="hero",
            kind="hero",
            variant_id=prefs.get("hero"),
            content=SectionContent(
                heading=title,
                subheading="Request time off and route approvals to authorized approvers.",
            ),
        ),
        Section(
            id="requests",
            kind="list",
            variant_id=prefs.get("list"),
            content=SectionContent(
                heading="Shift calendar records",
                items=("Team members", "Time-off requests", "Approvals"),
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
        app_kind=RECORDS_PRIMITIVE_ID,
        name=title,
        roles=("approver", "member"),
        pages=(Page(id="home", route="/", title="Home", sections=sections),),
        entities=(
            Entity(
                id="team_member",
                name="Team Member",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="email", type="email", required=True),
                ),
            ),
            Entity(
                id="time_off_request",
                name="Time Off Request",
                fields=(
                    EntityField(
                        name="member_id",
                        type="int",
                        required=True,
                        references="team_member",
                    ),
                    EntityField(name="reason", type="str", required=True),
                ),
            ),
            Entity(
                id="approval",
                name="Approval",
                write_roles=("approver",),
                read_roles=("approver",),
                fields=(
                    EntityField(
                        name="request_id",
                        type="int",
                        required=True,
                        references="time_off_request",
                    ),
                    EntityField(name="decision", type="str", required=True),
                ),
            ),
        ),
        primary_actions=(
            Action(id="view_requests", label="View requests", type="nav", target="/"),
        ),
    )


def prepare_records_app_spec(app: AppSpec) -> AppSpec:
    """Validate a records app and persist it verbatim when it is already complete."""
    if app.app_kind != RECORDS_PRIMITIVE_ID:
        raise ValueError(
            f"records primitive requires app_kind={RECORDS_PRIMITIVE_ID!r}, "
            f"got {app.app_kind!r}"
        )
    if not app.entities:
        raise ValueError("records primitive requires at least one entity")
    if app.roles:
        reserved_tables = {"users", "sessions"}
        for entity in app.entities:
            table = _records_table_name(entity)
            if table in reserved_tables:
                raise ValueError(
                    f"auth-enabled records app cannot declare entity table {table!r}; "
                    "it is reserved for auth infrastructure"
                )
    return AppSpec.model_validate(app.model_dump(mode="json"))


def _records_table_name(entity: Entity) -> str:
    from .generator import _slug

    return _slug(entity.id).replace("-", "_")


def _fk_order_entities(entities: tuple[Entity, ...]) -> tuple[Entity, ...]:
    by_id = {entity.id: entity for entity in entities}
    deps: dict[str, set[str]] = {}
    for entity in entities:
        refs = {field.references for field in entity.fields if field.references is not None}
        missing = sorted(ref for ref in refs if ref not in by_id)
        if missing:
            raise ValueError(
                f"entity {entity.id!r} references unknown entity {missing[0]!r}"
            )
        deps[entity.id] = set(refs)

    ordered: list[Entity] = []
    emitted: set[str] = set()
    pending = dict(deps)
    while pending:
        ready = [
            entity.id
            for entity in entities
            if entity.id in pending and pending[entity.id] <= emitted
        ]
        if not ready:
            cycle = ", ".join(sorted(pending))
            raise ValueError(f"entity foreign key cycle detected among: {cycle}")
        for entity_id in ready:
            ordered.append(by_id[entity_id])
            emitted.add(entity_id)
            del pending[entity_id]
    return tuple(ordered)


def _ts_const_names(entities: tuple[Entity, ...]) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()
    for entity in entities:
        base = re.sub(r"[^0-9A-Za-z_]", "_", _records_table_name(entity))
        if not re.match(r"^[A-Za-z_]", base):
            base = f"table_{base}"
        if base in _TS_RESERVED:
            base = f"{base}_table"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        names[entity.id] = name
    return names


def _function_names(entities: tuple[Entity, ...]) -> dict[str, str]:
    from .generator import _pascal

    names: dict[str, str] = {}
    used: set[str] = set()
    for entity in entities:
        base = _pascal(entity.id)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}{suffix}"
            suffix += 1
        used.add(name)
        names[entity.id] = name
    return names


def _emit_records_schema_sql(entities: tuple[Entity, ...]) -> str:
    from .generator import _sql_type

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
                constraints.append(
                    f'  FOREIGN KEY("{field.name}") REFERENCES "{target}"("id")'
                )
        cols.append('  "created_at" TEXT NOT NULL DEFAULT (datetime(\'now\'))')
        cols.extend(constraints)
        body = ",\n".join(cols)
        blocks.append(
            f'CREATE TABLE IF NOT EXISTS "{tables[entity.id]}" (\n'
            f"{body}\n"
            ");"
        )
    return (
        "-- Auto-generated D1 schema (records primitive).\n"
        "-- Keep this migration in sync with src/db/schema.ts.\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def _emit_auth_tables_schema_sql() -> str:
    return (
        'CREATE TABLE IF NOT EXISTS "users" (\n'
        '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  "email" TEXT NOT NULL UNIQUE,\n'
        '  "password_hash" TEXT NOT NULL,\n'
        '  "password_salt" TEXT NOT NULL,\n'
        '  "role" TEXT NOT NULL,\n'
        '  "created_at" TEXT NOT NULL DEFAULT(datetime(\'now\'))\n'
        ");\n"
        "\n"
        'CREATE TABLE IF NOT EXISTS "sessions" (\n'
        '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  "token_hash" TEXT NOT NULL UNIQUE,\n'
        '  "user_id" INTEGER NOT NULL,\n'
        '  "expires_at" TEXT NOT NULL,\n'
        '  "created_at" TEXT NOT NULL DEFAULT(datetime(\'now\')),\n'
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


def _emit_records_drizzle_ts(entities: tuple[Entity, ...]) -> str:
    from .generator import _drizzle_factory, _ts

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
        cols.append(
            "  created_at: text(\"created_at\").notNull().default(sql`(datetime('now'))`),"
        )
        blocks.append(
            f"export const {consts[entity.id]} = sqliteTable({_ts(tables[entity.id])}, {{\n"
            + "\n".join(cols)
            + "\n});"
        )
    return (
        "/* Auto-generated Drizzle schema - regenerated from .disco/appspec.json. */\n"
        'import { sql } from "drizzle-orm";\n'
        'import { integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";\n'
        "\n"
        + "\n\n".join(blocks)
        + "\n"
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
        "\n"
        + _emit_auth_tables_drizzle_ts()
        + "\n\n"
        + body
    )


def _field_meta(entity: Entity) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    from .generator import _sql_type

    columns = [field.name for field in entity.fields]
    required = [field.name for field in entity.fields if field.required]
    integers: list[str] = []
    reals: list[str] = []
    emails: list[str] = []
    for field in entity.fields:
        sql_type = "INTEGER" if field.references is not None else _sql_type(field.type)
        if sql_type == "INTEGER":
            integers.append(field.name)
        elif sql_type == "REAL":
            reals.append(field.name)
        if field.name.lower() == "email" or field.type.strip().lower() == "email":
            emails.append(field.name)
    return columns, required, integers, reals, emails


def _emit_worker_meta(entities: tuple[Entity, ...], consts: dict[str, str]) -> str:
    from .generator import _ts

    lines: list[str] = []
    for entity in entities:
        columns, required, integers, reals, emails = _field_meta(entity)
        name = consts[entity.id]
        lines.append(
            f"  {name}: {{ columns: {_ts(columns)}, required: {_ts(required)}, "
            f"integers: {_ts(integers)}, reals: {_ts(reals)}, emails: {_ts(emails)} }},"
        )
    return "\n".join(lines)


def _emit_worker_handlers(
    entities: tuple[Entity, ...], consts: dict[str, str], funcs: dict[str, str]
) -> str:
    handlers: list[str] = []
    for entity in entities:
        const = consts[entity.id]
        func = funcs[entity.id]
        table = _records_table_name(entity)
        handlers.append(
            f"async function insert{func}(env: Env, body: unknown): Promise<Response> {{\n"
            f"  const meta = META.{const};\n"
            "  const check = validateRecord(body, meta);\n"
            "  if (!check.ok) return json({ error: check.error }, 400);\n"
            "  try {\n"
            "    const db = drizzle(env.DB);\n"
            f"    await db.insert({const}).values(recordValues(check.rec, meta) as "
            f"typeof {const}.$inferInsert).run();\n"
            "  } catch {\n"
            '    return json({ error: "could not save record" }, 500);\n'
            "  }\n"
            "  return json({ ok: true }, 201);\n"
            "}\n\n"
            f"async function list{func}(env: Env): Promise<Response> {{\n"
            "  const db = drizzle(env.DB);\n"
            f"  const rows = await db.select().from({const}).orderBy(desc({const}.id))"
            ".limit(200).all();\n"
            f"  return json({{ {_ts_key(table)}: rows }});\n"
            "}\n"
        )
    return "\n\n".join(handlers)


def _ts_key(key: str) -> str:
    from .generator import _ts

    return _ts(key)


def _emit_route_table(
    entities: tuple[Entity, ...], funcs: dict[str, str]
) -> str:
    lines: list[str] = []
    for entity in entities:
        table = _records_table_name(entity)
        func = funcs[entity.id]
        lines.append(
            f"  // POST /api/{table}; GET /api/{table}\n"
            f"  {_ts_key('/api/' + table)}: {{\n"
            f"    post: (env, body) => insert{func}(env, body),\n"
            f"    get: (env) => list{func}(env),\n"
            "  },"
        )
    return "\n".join(lines)


def _emit_auth_route_table(
    entities: tuple[Entity, ...], funcs: dict[str, str]
) -> str:
    from .generator import _ts

    lines: list[str] = []
    for entity in entities:
        table = _records_table_name(entity)
        func = funcs[entity.id]
        lines.append(
            f"  // POST /api/{table}; GET /api/{table}\n"
            f"  {_ts_key('/api/' + table)}: {{\n"
            f"    writeRoles: {_ts(list(entity.write_roles))},\n"
            f"    readRoles: {_ts(list(entity.read_roles))},\n"
            f"    post: (env, body) => insert{func}(env, body),\n"
            f"    get: (env) => list{func}(env),\n"
            "  },"
        )
    return "\n".join(lines)


def _emit_records_worker_ts(entities: tuple[Entity, ...]) -> str:
    ordered = _fk_order_entities(entities)
    consts = _ts_const_names(entities)
    funcs = _function_names(entities)
    imports = ", ".join(consts[entity.id] for entity in ordered)
    return (
        "/* Auto-generated Cloudflare Worker (records primitive): public inserts + "
        "AUTH-GATED list reads. */\n"
        'import { desc } from "drizzle-orm";\n'
        'import { drizzle } from "drizzle-orm/d1";\n'
        f'import {{ {imports} }} from "../src/db/schema";\n'
        "\n"
        "export interface Env {\n"
        "  DB: D1Database;\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "  ADMIN_TOKEN?: string;\n"
        "}\n\n"
        "interface EntityMeta {\n"
        "  columns: string[];\n"
        "  required: string[];\n"
        "  integers: string[];\n"
        "  reals: string[];\n"
        "  emails: string[];\n"
        "}\n\n"
        "const META: Record<string, EntityMeta> = {\n"
        + _emit_worker_meta(ordered, consts)
        + "\n};\n"
        "const MAX_FIELD_LEN = 2000;\n"
        "const EMAIL_RE = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;\n\n"
        "function json(data: unknown, status = 200): Response {\n"
        "  return new Response(JSON.stringify(data), {\n"
        "    status,\n"
        '    headers: { "Content-Type": "application/json" },\n'
        "  });\n"
        "}\n\n"
        "function isAuthorized(request: Request, env: Env): boolean {\n"
        "  const expected = env.ADMIN_TOKEN;\n"
        "  if (!expected) return false;\n"
        '  const header = request.headers.get("Authorization") ?? "";\n'
        '  const prefix = "Bearer ";\n'
        "  if (!header.startsWith(prefix)) return false;\n"
        "  return header.slice(prefix.length) === expected;\n"
        "}\n\n"
        "type RecordCheck =\n"
        "  | { ok: true; rec: Record<string, unknown> }\n"
        "  | { ok: false; error: string };\n\n"
        + _emit_validate_record_ts()
        + "\n"
        + _emit_worker_handlers(ordered, consts, funcs)
        + "\n\n"
        "interface RouteHandlers {\n"
        "  post: (env: Env, body: unknown) => Promise<Response>;\n"
        "  get: (env: Env) => Promise<Response>;\n"
        "}\n\n"
        "const ROUTES: Record<string, RouteHandlers> = {\n"
        + _emit_route_table(ordered, funcs)
        + "\n};\n\n"
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    const url = new URL(request.url);\n"
        "    const route = ROUTES[url.pathname];\n"
        '    if (route && request.method === "POST") {\n'
        "      let body: unknown;\n"
        "      try {\n"
        "        body = await request.json();\n"
        "      } catch {\n"
        '        return json({ error: "invalid JSON" }, 400);\n'
        "      }\n"
        "      return route.post(env, body);\n"
        "    }\n"
        '    if (route && request.method === "GET") {\n'
        "      if (!isAuthorized(request, env)) {\n"
        '        return json({ error: "unauthorized" }, 401);\n'
        "      }\n"
        "      return route.get(env);\n"
        "    }\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_records_auth_worker_ts(app: AppSpec) -> str:
    from .generator import _ts

    ordered = _fk_order_entities(app.entities)
    consts = _ts_const_names(app.entities)
    funcs = _function_names(app.entities)
    imports = ", ".join(("users", "sessions", *(consts[entity.id] for entity in ordered)))
    return (
        "/* Auto-generated Cloudflare Worker (records primitive): session auth + RBAC.\n"
        "   When roles are declared, every entity read/write requires a valid per-user\n"
        "   session. Entity readRoles/writeRoles narrow access by user.role. */\n"
        'import { desc, eq } from "drizzle-orm";\n'
        'import { drizzle } from "drizzle-orm/d1";\n'
        f'import {{ {imports} }} from "../src/db/schema";\n'
        "\n"
        "export interface Env {\n"
        "  DB: D1Database;\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "  ADMIN_TOKEN?: string;\n"
        "}\n\n"
        "interface EntityMeta {\n"
        "  columns: string[];\n"
        "  required: string[];\n"
        "  integers: string[];\n"
        "  reals: string[];\n"
        "  emails: string[];\n"
        "}\n\n"
        "const META: Record<string, EntityMeta> = {\n"
        + _emit_worker_meta(ordered, consts)
        + "\n};\n"
        f"const ROLES: string[] = {_ts(list(app.roles))};\n"
        "const MAX_FIELD_LEN = 2000;\n"
        "const MAX_PASSWORD_LEN = 1024;\n"
        "const EMAIL_RE = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;\n\n"
        "function json(data: unknown, status = 200, headers: HeadersInit = {}): Response {\n"
        "  const out = new Headers(headers);\n"
        '  out.set("Content-Type", "application/json");\n'
        "  return new Response(JSON.stringify(data), { status, headers: out });\n"
        "}\n\n"
        "type RecordCheck =\n"
        "  | { ok: true; rec: Record<string, unknown> }\n"
        "  | { ok: false; error: string };\n\n"
        + _emit_validate_record_ts()
        + "\n"
        + _emit_auth_validation_ts()
        + "\n"
        + _emit_auth_crypto_ts()
        + "\n"
        + _emit_auth_session_ts()
        + "\n"
        + _emit_auth_endpoints_ts()
        + "\n"
        + _emit_worker_handlers(ordered, consts, funcs)
        + "\n\n"
        "interface RouteHandlers {\n"
        "  writeRoles: string[];\n"
        "  readRoles: string[];\n"
        "  post: (env: Env, body: unknown) => Promise<Response>;\n"
        "  get: (env: Env) => Promise<Response>;\n"
        "}\n\n"
        "const ROUTES: Record<string, RouteHandlers> = {\n"
        + _emit_auth_route_table(ordered, funcs)
        + "\n};\n\n"
        + _emit_auth_fetch_ts()
    )


def _emit_records_auth_wrangler_toml(app: AppSpec, lead: Entity) -> str:
    from .generator import _emit_wrangler_toml

    base = _emit_wrangler_toml(app, lead)
    old = (
        "# Worker-FIRST routing for the dynamic paths (Epic I): the lead API and the\n"
        "# admin read-back are handled by worker/index.ts, NOT the static-asset/SPA\n"
        "# layer. Without this, single-page-application not_found_handling can shadow a\n"
        "# navigation to /admin (or an /api/* read) with index.html so the Worker never\n"
        "# runs. Requires Wrangler v4.20+ (the array form of run_worker_first).\n"
        'run_worker_first = ["/api/*", "/admin"]\n'
    )
    new = (
        "# Worker-FIRST routing for the full auth app: encoded API-like paths must\n"
        "# reach worker/index.ts so the Worker can fail closed before ASSETS.\n"
        "# Non-API paths are still served by env.ASSETS.fetch from the Worker fallback.\n"
        "run_worker_first = true\n"
    )
    return base.replace(old, new)


def _emit_validate_record_ts() -> str:
    return (
        "function validateRecord(body: unknown, meta: EntityMeta): RecordCheck {\n"
        '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
        '    return { ok: false, error: "request body must be a JSON object" };\n'
        "  }\n"
        "  const rec = body as Record<string, unknown>;\n"
        "  for (const key of Object.keys(rec)) {\n"
        "    if (!meta.columns.includes(key)) {\n"
        "      return { ok: false, error: `unknown field: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of meta.required) {\n"
        "    const v = rec[key];\n"
        '    if (v === undefined || v === null || v === "") {\n'
        "      return { ok: false, error: `missing required field: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of meta.columns) {\n"
        "    const v = rec[key];\n"
        "    if (v === undefined || v === null) continue;\n"
        "    if (meta.integers.includes(key)) {\n"
        '      if (typeof v !== "number" || !Number.isInteger(v)) {\n'
        "        return { ok: false, error: `field must be an integer: ${key}` };\n"
        "      }\n"
        "    } else if (meta.reals.includes(key)) {\n"
        '      if (typeof v !== "number" || !Number.isFinite(v)) {\n'
        "        return { ok: false, error: `field must be a number: ${key}` };\n"
        "      }\n"
        "    } else {\n"
        '      if (typeof v !== "string") {\n'
        "        return { ok: false, error: `field must be a string: ${key}` };\n"
        "      }\n"
        "      if (v.length > MAX_FIELD_LEN) {\n"
        "        return { ok: false, error: `field too long: ${key}` };\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "  for (const key of meta.emails) {\n"
        "    const v = rec[key];\n"
        '    if (typeof v === "string" && v !== "" && !EMAIL_RE.test(v)) {\n'
        "      return { ok: false, error: `invalid email: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  return { ok: true, rec };\n"
        "}\n\n"
        "function recordValues(\n"
        "  rec: Record<string, unknown>,\n"
        "  meta: EntityMeta\n"
        "): Record<string, string | number | null> {\n"
        "  const values: Record<string, string | number | null> = {};\n"
        "  for (const key of meta.columns) {\n"
        "    const v = rec[key];\n"
        '    values[key] = typeof v === "string" || typeof v === "number" ? v : null;\n'
        "  }\n"
        "  return values;\n"
        "}\n"
    )


def _emit_auth_validation_ts() -> str:
    return (
        "type RegisterCheck =\n"
        "  | { ok: true; email: string; password: string; role: string }\n"
        "  | { ok: false; error: string };\n"
        "type LoginCheck =\n"
        "  | { ok: true; email: string; password: string }\n"
        "  | { ok: false; error: string };\n\n"
        "function authObject(\n"
        "  body: unknown,\n"
        "  fields: string[]\n"
        "): { ok: true; rec: Record<string, unknown> } | { ok: false; error: string } {\n"
        '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
        '    return { ok: false, error: "request body must be a JSON object" };\n'
        "  }\n"
        "  const rec = body as Record<string, unknown>;\n"
        "  for (const key of Object.keys(rec)) {\n"
        "    if (!fields.includes(key)) return { ok: false, error: `unknown field: ${key}` };\n"
        "  }\n"
        "  for (const key of fields) {\n"
        '    if (rec[key] === undefined || rec[key] === null || rec[key] === "") {\n'
        "      return { ok: false, error: `missing required field: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  return { ok: true, rec };\n"
        "}\n\n"
        "function validateRegister(body: unknown): RegisterCheck {\n"
        '  const check = authObject(body, ["email", "password", "role"]);\n'
        "  if (!check.ok) return check;\n"
        "  const { email, password, role } = check.rec;\n"
        '  if (typeof email !== "string" || !EMAIL_RE.test(email)) {\n'
        '    return { ok: false, error: "invalid email" };\n'
        "  }\n"
        '  if (typeof password !== "string" || password.length < 8) {\n'
        '    return { ok: false, error: "password must be at least 8 characters" };\n'
        "  }\n"
        "  if (password.length > MAX_PASSWORD_LEN) {\n"
        '    return { ok: false, error: "password too long" };\n'
        "  }\n"
        '  if (typeof role !== "string" || !ROLES.includes(role)) {\n'
        '    return { ok: false, error: "invalid role" };\n'
        "  }\n"
        "  return { ok: true, email, password, role };\n"
        "}\n\n"
        "function validateLogin(body: unknown): LoginCheck {\n"
        '  const check = authObject(body, ["email", "password"]);\n'
        "  if (!check.ok) return check;\n"
        "  const { email, password } = check.rec;\n"
        '  if (typeof email !== "string" || typeof password !== "string") {\n'
        '    return { ok: false, error: "invalid credentials" };\n'
        "  }\n"
        "  if (password.length > MAX_PASSWORD_LEN) {\n"
        '    return { ok: false, error: "invalid credentials" };\n'
        "  }\n"
        "  return { ok: true, email, password };\n"
        "}\n"
    )


def _emit_auth_crypto_ts() -> str:
    return (
        "const TEXT_ENCODER = new TextEncoder();\n"
        "const PBKDF2_ITERATIONS = 100000;\n"
        "const PASSWORD_BITS = 256;\n\n"
        'const DUMMY_LOGIN_SALT_HEX = "000102030405060708090a0b0c0d0e0f";\n\n'
        'const DUMMY_LOGIN_HASH_HEX = "124e4e5ea9e8daf1710f215192eda6bf79ff9ce9b977e7e0d754392319f6fa68";\n\n'
        "function bytesToHex(bytes: Uint8Array): string {\n"
        '  let out = "";\n'
        "  for (const byte of bytes) out += byte.toString(16).padStart(2, \"0\");\n"
        "  return out;\n"
        "}\n\n"
        "function hexToBytes(hex: string): Uint8Array | null {\n"
        "  if (hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) return null;\n"
        "  const bytes = new Uint8Array(hex.length / 2);\n"
        "  for (let i = 0; i < bytes.length; i += 1) {\n"
        "    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);\n"
        "  }\n"
        "  return bytes;\n"
        "}\n\n"
        "function randomHex(byteLength: number): string {\n"
        "  const bytes = new Uint8Array(byteLength);\n"
        "  crypto.getRandomValues(bytes);\n"
        "  return bytesToHex(bytes);\n"
        "}\n\n"
        "async function sha256Hex(value: string): Promise<string> {\n"
        '  const digest = await crypto.subtle.digest("SHA-256", TEXT_ENCODER.encode(value));\n'
        "  return bytesToHex(new Uint8Array(digest));\n"
        "}\n\n"
        "async function derivePasswordHash(password: string, salt: Uint8Array): Promise<string> {\n"
        "  const key = await crypto.subtle.importKey(\n"
        '    "raw",\n'
        "    TEXT_ENCODER.encode(password),\n"
        '    "PBKDF2",\n'
        "    false,\n"
        '    ["deriveBits"]\n'
        "  );\n"
        "  const bits = await crypto.subtle.deriveBits(\n"
        '    { name: "PBKDF2", hash: "SHA-256", salt, iterations: PBKDF2_ITERATIONS },\n'
        "    key,\n"
        "    PASSWORD_BITS\n"
        "  );\n"
        "  return bytesToHex(new Uint8Array(bits));\n"
        "}\n\n"
        "async function hashPassword(password: string): Promise<{ salt: string; hash: string }> {\n"
        "  const salt = new Uint8Array(16);\n"
        "  crypto.getRandomValues(salt);\n"
        "  return { salt: bytesToHex(salt), hash: await derivePasswordHash(password, salt) };\n"
        "}\n\n"
        "async function verifyPassword(\n"
        "  password: string,\n"
        "  saltHex: string,\n"
        "  expectedHashHex: string\n"
        "): Promise<boolean> {\n"
        "  const salt = hexToBytes(saltHex);\n"
        "  if (salt === null) return false;\n"
        "  const actual = await derivePasswordHash(password, salt);\n"
        "  return fixedWorkHexEqual(actual, expectedHashHex);\n"
        "}\n\n"
        "async function spendInvalidLoginWork(password: string): Promise<void> {\n"
        "  const salt = hexToBytes(DUMMY_LOGIN_SALT_HEX);\n"
        "  if (salt === null) return;\n"
        "  const actual = await derivePasswordHash(password, salt);\n"
        "  fixedWorkHexEqual(actual, DUMMY_LOGIN_HASH_HEX);\n"
        "}\n\n"
        "// Workers do not expose timingSafeEqual. Length is checked first, then equal-length\n"
        "// hex strings are compared with XOR accumulation to avoid `===` on derived secrets.\n"
        "function fixedWorkHexEqual(actual: string, expected: string): boolean {\n"
        "  if (actual.length !== expected.length) return false;\n"
        "  let diff = 0;\n"
        "  for (let i = 0; i < actual.length; i += 1) {\n"
        "    diff |= actual.charCodeAt(i) ^ expected.charCodeAt(i);\n"
        "  }\n"
        "  return diff === 0;\n"
        "}\n"
    )


def _emit_auth_session_ts() -> str:
    return (
        "const SESSION_TTL_SECONDS = 7 * 24 * 60 * 60;\n\n"
        "interface UserSession {\n"
        "  tokenHash: string;\n"
        "  userId: number;\n"
        "  role: string;\n"
        "}\n\n"
        "function nowIso(): string {\n"
        "  return new Date().toISOString();\n"
        "}\n\n"
        "function sessionCookie(token: string): string {\n"
        "  return `session=${token}; HttpOnly; SameSite=Strict; Secure; Path=/; "
        "Max-Age=${SESSION_TTL_SECONDS}`;\n"
        "}\n\n"
        "function clearSessionCookie(): string {\n"
        '  return "session=; HttpOnly; SameSite=Strict; Secure; Path=/; Max-Age=0";\n'
        "}\n\n"
        "function readCookie(request: Request, name: string): string | null {\n"
        '  const header = request.headers.get("Cookie") ?? "";\n'
        '  for (const piece of header.split(";")) {\n'
        "    const part = piece.trim();\n"
        "    const eqAt = part.indexOf(\"=\");\n"
        "    if (eqAt <= 0) continue;\n"
        "    if (part.slice(0, eqAt) === name) return part.slice(eqAt + 1);\n"
        "  }\n"
        "  return null;\n"
        "}\n\n"
        "async function mintSession(env: Env, userId: number): Promise<string> {\n"
        "  const token = randomHex(32);\n"
        "  const tokenHash = await sha256Hex(token);\n"
        "  const expiresAt = new Date(Date.now() + SESSION_TTL_SECONDS * 1000).toISOString();\n"
        "  const db = drizzle(env.DB);\n"
        "  await db.insert(sessions).values({\n"
        "    token_hash: tokenHash,\n"
        "    user_id: userId,\n"
        "    expires_at: expiresAt,\n"
        "  }).run();\n"
        "  return token;\n"
        "}\n\n"
        "async function resolveSession(request: Request, env: Env): Promise<UserSession | null> {\n"
        '  const token = readCookie(request, "session");\n'
        "  if (!token) return null;\n"
        "  const tokenHash = await sha256Hex(token);\n"
        "  const db = drizzle(env.DB);\n"
        "  const rows = await db\n"
        "    .select({\n"
        "      userId: users.id,\n"
        "      role: users.role,\n"
        "      expiresAt: sessions.expires_at,\n"
        "    })\n"
        "    .from(sessions)\n"
        "    .innerJoin(users, eq(sessions.user_id, users.id))\n"
        "    .where(eq(sessions.token_hash, tokenHash))\n"
        "    .limit(1)\n"
        "    .all();\n"
        "  const row = rows[0];\n"
        "  if (!row) return null;\n"
        "  if (row.expiresAt <= nowIso()) {\n"
        "    await db.delete(sessions).where(eq(sessions.token_hash, tokenHash)).run();\n"
        "    return null;\n"
        "  }\n"
        "  if (!ROLES.includes(row.role)) return null;\n"
        "  return { tokenHash, userId: row.userId, role: row.role };\n"
        "}\n\n"
        "function authorizeSession(session: UserSession | null, roles: string[]): Response | null {\n"
        '  if (session === null) return json({ error: "unauthorized" }, 401);\n'
        '  if (roles.length > 0 && !roles.includes(session.role)) {\n'
        '    return json({ error: "forbidden" }, 403);\n'
        "  }\n"
        "  return null;\n"
        "}\n"
    )


def _emit_auth_endpoints_ts() -> str:
    return (
        "async function isAdminAuthorized(request: Request, env: Env): Promise<boolean> {\n"
        "  const expected = env.ADMIN_TOKEN;\n"
        "  if (!expected) return false;\n"
        '  const header = request.headers.get("Authorization") ?? "";\n'
        '  const prefix = "Bearer ";\n'
        "  if (!header.startsWith(prefix)) return false;\n"
        "  const presentedHash = await sha256Hex(header.slice(prefix.length));\n"
        "  const expectedHash = await sha256Hex(expected);\n"
        "  return fixedWorkHexEqual(presentedHash, expectedHash);\n"
        "}\n\n"
        "function invalidCredentials(): Response {\n"
        '  return json({ error: "invalid credentials" }, 401);\n'
        "}\n\n"
        "async function registerUser(\n"
        "  request: Request,\n"
        "  env: Env,\n"
        "  body: unknown\n"
        "): Promise<Response> {\n"
        '  if (!(await isAdminAuthorized(request, env))) return json({ error: "unauthorized" }, 401);\n'
        "  const check = validateRegister(body);\n"
        "  if (!check.ok) return json({ error: check.error }, 400);\n"
        "  const db = drizzle(env.DB);\n"
        "  const existing = await db\n"
        "    .select({ id: users.id })\n"
        "    .from(users)\n"
        "    .where(eq(users.email, check.email))\n"
        "    .limit(1)\n"
        "    .all();\n"
        '  if (existing.length > 0) return json({ error: "email already registered" }, 409);\n'
        "  const password = await hashPassword(check.password);\n"
        "  try {\n"
        "    await db.insert(users).values({\n"
        "      email: check.email,\n"
        "      password_hash: password.hash,\n"
        "      password_salt: password.salt,\n"
        "      role: check.role,\n"
        "    }).run();\n"
        "  } catch {\n"
        '    return json({ error: "email already registered" }, 409);\n'
        "  }\n"
        "  return json({ ok: true }, 201);\n"
        "}\n\n"
        "async function loginUser(env: Env, body: unknown): Promise<Response> {\n"
        "  const check = validateLogin(body);\n"
        "  if (!check.ok) {\n"
        '    if (check.error === "invalid credentials") return invalidCredentials();\n'
        "    return json({ error: check.error }, 400);\n"
        "  }\n"
        "  const db = drizzle(env.DB);\n"
        "  const rows = await db\n"
        "    .select({\n"
        "      id: users.id,\n"
        "      passwordHash: users.password_hash,\n"
        "      passwordSalt: users.password_salt,\n"
        "    })\n"
        "    .from(users)\n"
        "    .where(eq(users.email, check.email))\n"
        "    .limit(1)\n"
        "    .all();\n"
        "  const user = rows[0];\n"
        "  if (!user) {\n"
        "    await spendInvalidLoginWork(check.password);\n"
        "    return invalidCredentials();\n"
        "  }\n"
        "  const ok = await verifyPassword(check.password, user.passwordSalt, user.passwordHash);\n"
        "  if (!ok) return invalidCredentials();\n"
        "  const token = await mintSession(env, user.id);\n"
        '  return json({ ok: true }, 200, { "Set-Cookie": sessionCookie(token) });\n'
        "}\n\n"
        "async function logoutUser(request: Request, env: Env): Promise<Response> {\n"
        '  const token = readCookie(request, "session");\n'
        "  if (token) {\n"
        "    const tokenHash = await sha256Hex(token);\n"
        "    const db = drizzle(env.DB);\n"
        "    await db.delete(sessions).where(eq(sessions.token_hash, tokenHash)).run();\n"
        "  }\n"
        '  return json({ ok: true }, 200, { "Set-Cookie": clearSessionCookie() });\n'
        "}\n"
    )


def _emit_auth_fetch_ts() -> str:
    return (
        "type JsonBody = { ok: true; body: unknown } | { ok: false; response: Response };\n\n"
        "function sameOriginOk(request: Request, url: URL): boolean {\n"
        '  const origin = request.headers.get("Origin");\n'
        "  if (origin === null) return true;\n"
        "  try {\n"
        "    return new URL(origin).host === url.host;\n"
        "  } catch {\n"
        "    return false;\n"
        "  }\n"
        "}\n\n"
        "function hasJsonContentType(request: Request): boolean {\n"
        '  const contentType = request.headers.get("Content-Type") ?? "";\n'
        '  return contentType.toLowerCase().startsWith("application/json");\n'
        "}\n\n"
        "function requireJsonContentType(request: Request): Response | null {\n"
        "  if (hasJsonContentType(request)) return null;\n"
        '  return json({ error: "unsupported media type" }, 415);\n'
        "}\n\n"
        "function rawPathname(requestUrl: string, origin: string): string {\n"
        "  const rest = requestUrl.startsWith(origin) ? requestUrl.slice(origin.length) : requestUrl;\n"
        "  const end = rest.search(/[?#]/);\n"
        "  const path = end === -1 ? rest : rest.slice(0, end);\n"
        '  return path === "" ? "/" : path;\n'
        "}\n\n"
        "function decodedPathname(pathname: string): string | null {\n"
        "  try {\n"
        "    return decodeURIComponent(pathname);\n"
        "  } catch {\n"
        "    return null;\n"
        "  }\n"
        "}\n\n"
        "function isApiPath(\n"
        "  rawPath: string,\n"
        "  urlPath: string,\n"
        "  decodedPath: string | null\n"
        "): boolean {\n"
        "  return (\n"
        '    rawPath.startsWith("/api/") ||\n'
        '    urlPath.startsWith("/api/") ||\n'
        "    decodedPath === null ||\n"
        '    decodedPath.startsWith("/api/")\n'
        "  );\n"
        "}\n\n"
        "async function readJsonBody(request: Request): Promise<JsonBody> {\n"
        "  try {\n"
        "    return { ok: true, body: await request.json() };\n"
        "  } catch {\n"
        '    return { ok: false, response: json({ error: "invalid JSON" }, 400) };\n'
        "  }\n"
        "}\n\n"
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    const url = new URL(request.url);\n"
        "    const rawPath = rawPathname(request.url, url.origin);\n"
        "    const decodedPath = decodedPathname(rawPath);\n"
        "    const apiPath = isApiPath(rawPath, url.pathname, decodedPath);\n"
        '    if (request.method === "POST" && apiPath && !sameOriginOk(request, url)) {\n'
        '      return json({ error: "bad origin" }, 403);\n'
        "    }\n"
        '    if (rawPath === "/api/register" && request.method === "POST") {\n'
        "      const contentTypeError = requireJsonContentType(request);\n"
        "      if (contentTypeError !== null) return contentTypeError;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return registerUser(request, env, parsed.body);\n"
        "    }\n"
        '    if (rawPath === "/api/login" && request.method === "POST") {\n'
        "      const contentTypeError = requireJsonContentType(request);\n"
        "      if (contentTypeError !== null) return contentTypeError;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return loginUser(env, parsed.body);\n"
        "    }\n"
        '    if (rawPath === "/api/logout" && request.method === "POST") {\n'
        "      return logoutUser(request, env);\n"
        "    }\n"
        "    const route = ROUTES[rawPath];\n"
        '    if (route && request.method === "POST") {\n'
        "      const contentTypeError = requireJsonContentType(request);\n"
        "      if (contentTypeError !== null) return contentTypeError;\n"
        "      const session = await resolveSession(request, env);\n"
        "      const denied = authorizeSession(session, route.writeRoles);\n"
        "      if (denied !== null) return denied;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return route.post(env, parsed.body);\n"
        "    }\n"
        '    if (route && request.method === "GET") {\n'
        "      const session = await resolveSession(request, env);\n"
        "      const denied = authorizeSession(session, route.readRoles);\n"
        "      if (denied !== null) return denied;\n"
        "      return route.get(env);\n"
        "    }\n"
        "    if (apiPath) {\n"
        '      return json({ error: "not found" }, 404);\n'
        "    }\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_records_owner_guide_md(app: AppSpec, db_name: str) -> str:
    from .generator import _html_text

    app_name = _html_text(app.name)
    return (
        f"# Deploy guide - {app_name}\n"
        "\n"
        "This is a Cloudflare-ready records app: a Vite/React SPA plus a Worker with\n"
        "public POST inserts and Bearer-gated GET list endpoints for each entity.\n"
        "\n"
        "## Verify locally\n"
        "\n"
        "```sh\n"
        "cp .dev.vars.example .dev.vars\n"
        "npm install\n"
        "npm run build\n"
        "npm run db:local\n"
        "npm run cf:dev\n"
        "```\n"
        "\n"
        "The generated `schema.sql` and `migrations/0001_init.sql` contain the same\n"
        "FK-ordered init schema. Public inserts use `/api/<table>`; list reads use the\n"
        "same path with `Authorization: Bearer <ADMIN_TOKEN>`.\n"
        "\n"
        "## Deploy\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 create {db_name}\n"
        f"npx wrangler d1 execute {db_name} --remote --file=./schema.sql\n"
        "npx wrangler secret put ADMIN_TOKEN\n"
        "npm run build\n"
        "npx wrangler deploy\n"
        "```\n"
    )


def _emit_records_auth_owner_guide_md(app: AppSpec, db_name: str) -> str:
    from .generator import _html_text

    app_name = _html_text(app.name)
    first_role = app.roles[0]
    return (
        f"# Deploy guide - {app_name}\n"
        "\n"
        "This is a Cloudflare-ready records app with per-user sessions and RBAC.\n"
        "All record reads and writes require login; per-entity read/write roles narrow\n"
        "access further.\n"
        "\n"
        "## Verify locally\n"
        "\n"
        "```sh\n"
        "cp .dev.vars.example .dev.vars\n"
        "npm install\n"
        "npm run build\n"
        "npm run db:local\n"
        "npm run cf:dev\n"
        "```\n"
        "\n"
        "Create users through `POST /api/register` with `Authorization: Bearer\n"
        "<ADMIN_TOKEN>`. The first declared role is the bootstrap/admin role:\n"
        f" `{first_role}`.\n"
        "\n"
        "## Authorization scope\n"
        "\n"
        "This scaffold enforces role-based route access, such as approver-only\n"
        "approval writes. It does not yet enforce per-row ownership: any\n"
        "authenticated user can read or insert on entities without role gates, and\n"
        "foreign-key fields are trusted from the request body. For multi-tenant use,\n"
        "add ownership checks that link users to records and derive owner fields from\n"
        "the authenticated session.\n"
        "\n"
        "## Deploy\n"
        "\n"
        "```sh\n"
        f"npx wrangler d1 create {db_name}\n"
        f"npx wrangler d1 execute {db_name} --remote --file=./schema.sql\n"
        "npx wrangler secret put ADMIN_TOKEN\n"
        "npm run build\n"
        "npx wrangler deploy\n"
        "```\n"
    )


def _legacy_records_app_dump(app: AppSpec) -> dict[str, object]:
    data = app.model_dump(mode="json")
    data.pop("roles", None)
    entities = data.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, dict):
                entity.pop("write_roles", None)
                entity.pop("read_roles", None)
    return data


def _emit_records_legacy_manifest_ts(
    app: AppSpec, design: DesignSpec, names: dict[tuple[str, str], str]
) -> str:
    from .generator import _comp_name, _iter_sections, _ts

    payload = json.dumps(
        {
            "app": _legacy_records_app_dump(app),
            "design": design.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    sections = [
        {"page": page.id, "section": section.id, "component": _comp_name(names, page, section)}
        for page, section in _iter_sections(app)
    ]
    return (
        "/* Auto-generated build manifest — a digest of the App + Design specs. */\n"
        f"export const SPEC_DIGEST = {_ts(digest)};\n"
        f"export const SECTIONS = {_ts(sections)} as const;\n"
    )


def generate_records(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower a records AppSpec into a Cloudflare app tree."""
    app = prepare_records_app_spec(app)
    ordered = _fk_order_entities(app.entities)
    db_entity = ordered[0]
    from .generator import (
        _comp_name,
        _component_names,
        _db_name,
        _emit_api_client_ts,
        _emit_component,
        _emit_content_ts,
        _emit_dev_vars_example,
        _emit_drizzle_config_ts,
        _emit_gitignore,
        _emit_index_html,
        _emit_main_tsx,
        _emit_manifest_ts,
        _emit_package_json,
        _emit_styles_css,
        _emit_submit_hook_ts,
        _emit_tsconfig,
        _emit_vite_config,
        _emit_wrangler_toml,
        _iter_sections,
    )

    db_name = _db_name(app, db_entity)
    names = _component_names(app)
    auth_enabled = bool(app.roles)
    schema_sql = (
        _emit_records_auth_schema_sql(app.entities)
        if auth_enabled
        else _emit_records_schema_sql(app.entities)
    )
    worker_ts = (
        _emit_records_auth_worker_ts(app)
        if auth_enabled
        else _emit_records_worker_ts(app.entities)
    )
    drizzle_ts = (
        _emit_records_auth_drizzle_ts(app.entities)
        if auth_enabled
        else _emit_records_drizzle_ts(app.entities)
    )
    owner_guide = (
        _emit_records_auth_owner_guide_md(app, db_name)
        if auth_enabled
        else _emit_records_owner_guide_md(app, db_name)
    )
    manifest_ts = (
        _emit_manifest_ts(app, design, names)
        if auth_enabled
        else _emit_records_legacy_manifest_ts(app, design, names)
    )
    files: dict[str, str] = {
        "index.html": _emit_index_html(app, design),
        "package.json": _emit_package_json(app, db_name),
        "drizzle.config.ts": _emit_drizzle_config_ts(),
        "tsconfig.json": _emit_tsconfig(),
        "vite.config.ts": _emit_vite_config(),
        "wrangler.toml": (
            _emit_records_auth_wrangler_toml(app, db_entity)
            if auth_enabled
            else _emit_wrangler_toml(app, db_entity)
        ),
        "schema.sql": schema_sql,
        "migrations/0001_init.sql": schema_sql,
        "worker/index.ts": worker_ts,
        "src/main.tsx": _emit_main_tsx(),
        "src/App.tsx": _emit_index_app_tsx(app, names),
        "src/api/client.ts": _emit_api_client_ts(),
        "src/hooks/useSubmit.ts": _emit_submit_hook_ts(),
        "src/styles.css": _emit_styles_css(design),
        "src/db/schema.ts": drizzle_ts,
        "src/generated/content.ts": _emit_content_ts(app, names),
        "src/generated/manifest.ts": manifest_ts,
        "OWNER_GUIDE.md": owner_guide,
        ".dev.vars.example": _emit_dev_vars_example(),
        ".gitignore": _emit_gitignore(),
    }
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        files[f"src/components/{comp}.tsx"] = _emit_component(
            comp, page, section, db_entity, f"/api/{_records_table_name(db_entity)}"
        )
    return dict(sorted(files.items()))


def _emit_index_app_tsx(
    app: AppSpec, names: dict[tuple[str, str], str]
) -> str:
    from .generator import _emit_app_tsx

    return _emit_app_tsx(app, names)


register_primitive(
    PrimitiveDefinition(
        id=RECORDS_PRIMITIVE_ID,
        default_app_spec=default_records_app_spec,
        prepare_app_spec=prepare_records_app_spec,
        generate=generate_records,
        # verify=None ON PURPOSE (WO-A3): records apps have ALWAYS fallen through to
        # the verifier's lead-gen check bundle (the pre-dispatch else-branch), and the
        # tool's dispatch fallback (`verify is None -> lead_gen_verify`) preserves that
        # exactly. Give records its own verify only with its own contract checks.
        verify=None,
    )
)


__all__ = [
    "RECORDS_PRIMITIVE_ID",
    "default_records_auth_app_spec",
    "default_records_app_spec",
    "generate_records",
    "prepare_records_app_spec",
]
