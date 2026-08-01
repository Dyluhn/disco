"""Per-entity Worker metadata/handler/route-table emitters for the records
primitive, plus the shared record-validation TS block.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import Entity
from .naming import _field_meta, _records_table_name


def _emit_worker_meta(entities: tuple[Entity, ...], consts: dict[str, str]) -> str:
    from ..generator import _ts

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
    from ..generator import _ts

    return _ts(key)


def _emit_route_table(entities: tuple[Entity, ...], funcs: dict[str, str]) -> str:
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


def _emit_auth_route_table(entities: tuple[Entity, ...], funcs: dict[str, str]) -> str:
    from ..generator import _ts

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
