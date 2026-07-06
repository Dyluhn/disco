"""The AppKit `records` primitive: related entities + per-entity CRUD routes.

This primitive deliberately lives beside lead-gen/directory instead of widening the
lead-gen path into a generic CRUD DSL. It reuses the generator's shared
shape-agnostic emitters and owns only the multi-table schema, Drizzle schema, and
N-entity Worker surface.
"""

from __future__ import annotations

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


def prepare_records_app_spec(app: AppSpec) -> AppSpec:
    """Validate a records app and persist it verbatim when it is already complete."""
    if app.app_kind != RECORDS_PRIMITIVE_ID:
        raise ValueError(
            f"records primitive requires app_kind={RECORDS_PRIMITIVE_ID!r}, "
            f"got {app.app_kind!r}"
        )
    if not app.entities:
        raise ValueError("records primitive requires at least one entity")
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


def generate_records(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower a records AppSpec into a Cloudflare app tree."""
    app = prepare_records_app_spec(app)
    ordered = _fk_order_entities(app.entities)
    db_entity = ordered[0]
    from .generator import (
        _comp_name,
        _component_names,
        _db_name,
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
        _emit_tsconfig,
        _emit_vite_config,
        _emit_wrangler_toml,
        _iter_sections,
    )

    db_name = _db_name(app, db_entity)
    names = _component_names(app)
    schema_sql = _emit_records_schema_sql(app.entities)
    files: dict[str, str] = {
        "index.html": _emit_index_html(app, design),
        "package.json": _emit_package_json(app, db_name),
        "drizzle.config.ts": _emit_drizzle_config_ts(),
        "tsconfig.json": _emit_tsconfig(),
        "vite.config.ts": _emit_vite_config(),
        "wrangler.toml": _emit_wrangler_toml(app, db_entity),
        "schema.sql": schema_sql,
        "migrations/0001_init.sql": schema_sql,
        "worker/index.ts": _emit_records_worker_ts(app.entities),
        "src/main.tsx": _emit_main_tsx(),
        "src/App.tsx": _emit_index_app_tsx(app, names),
        "src/styles.css": _emit_styles_css(design),
        "src/db/schema.ts": _emit_records_drizzle_ts(app.entities),
        "src/generated/content.ts": _emit_content_ts(app, names),
        "src/generated/manifest.ts": _emit_manifest_ts(app, design, names),
        "OWNER_GUIDE.md": _emit_records_owner_guide_md(app, db_name),
        ".dev.vars.example": _emit_dev_vars_example(),
        ".gitignore": _emit_gitignore(),
    }
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        files[f"src/components/{comp}.tsx"] = _emit_component(
            comp, page, section, db_entity
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
    )
)


__all__ = [
    "RECORDS_PRIMITIVE_ID",
    "default_records_app_spec",
    "generate_records",
    "prepare_records_app_spec",
]
