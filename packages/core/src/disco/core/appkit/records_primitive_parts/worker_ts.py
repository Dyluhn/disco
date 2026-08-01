"""The base (non-auth) records Worker emitter.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import Entity
from .naming import _fk_order_entities, _function_names, _ts_const_names
from .worker_meta import (
    _emit_route_table,
    _emit_validate_record_ts,
    _emit_worker_handlers,
    _emit_worker_meta,
)


def _emit_records_worker_ts(
    entities: tuple[Entity, ...], form_entities: tuple[Entity, ...] = ()
) -> str:
    from ..form_primitive import _form_const_name

    ordered = _fk_order_entities(entities)
    consts = _ts_const_names(entities)
    funcs = _function_names(entities)
    imports = ", ".join(
        [*(consts[entity.id] for entity in ordered), *(_form_const_name(e) for e in form_entities)]
    )
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
