"""Trusted Worker lowering for opt-in records access policies.

The ordinary records-auth generator stays byte-stable.  This module is selected
only when an AppSpec declares ``record_policy`` or ``role_admin_roles``.
"""

from __future__ import annotations

from ..spec import AppSpec, Entity
from .auth_crypto import _emit_auth_crypto_ts
from .auth_endpoints import _emit_auth_endpoints_ts
from .auth_session import _emit_auth_session_ts
from .auth_validation import _emit_auth_validation_ts
from .naming import (
    _field_meta,
    _fk_order_entities,
    _function_names,
    _records_table_name,
    _ts_const_names,
)
from .worker_meta import _emit_validate_record_ts


def records_policy_enabled(app: AppSpec) -> bool:
    return bool(app.role_admin_roles or any(entity.record_policy for entity in app.entities))


def _entity_policy(
    entity: Entity,
) -> tuple[bool, list[str], bool, list[str], list[str], str | None]:
    policy = entity.record_policy
    if policy is None:
        return (
            False,
            list(entity.write_roles),
            False,
            list(entity.write_roles),
            [],
            None,
        )
    return (
        policy.public_read,
        list(policy.create_roles),
        policy.owner_managed,
        list(policy.manage_roles),
        list(policy.lock_roles),
        policy.parent_lock_field,
    )


def _emit_policy_meta(entities: tuple[Entity, ...], consts: dict[str, str]) -> str:
    from ..generator import _ts

    lines: list[str] = []
    for entity in entities:
        columns, required, integers, reals, emails = _field_meta(entity)
        public_read, create_roles, owner_managed, manage_roles, lock_roles, parent_field = (
            _entity_policy(entity)
        )
        lines.append(
            f"  {consts[entity.id]}: {{ table: {_ts(_records_table_name(entity))}, "
            f"columns: {_ts(columns)}, required: {_ts(required)}, integers: {_ts(integers)}, "
            f"reals: {_ts(reals)}, emails: {_ts(emails)}, "
            f"publicRead: {_ts(public_read)}, readRoles: {_ts(list(entity.read_roles))}, "
            f"createRoles: {_ts(create_roles)}, ownerManaged: {_ts(owner_managed)}, "
            f"manageRoles: {_ts(manage_roles)}, lockRoles: {_ts(lock_roles)}, "
            f"parentLockField: {_ts(parent_field)} }},"
        )
    return "\n".join(lines)


def _emit_patch_validation_ts() -> str:
    return (
        "function validatePatch(body: unknown, meta: EntityMeta): RecordCheck {\n"
        '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
        '    return { ok: false, error: "request body must be a JSON object" };\n'
        "  }\n"
        "  const rec = body as Record<string, unknown>;\n"
        '  if (Object.keys(rec).length === 0) return { ok: false, error: "empty update" };\n'
        "  const optionalMeta = { ...meta, required: [] };\n"
        "  return validateRecord(rec, optionalMeta);\n"
        "}\n\n"
        "function patchValues(\n"
        "  rec: Record<string, unknown>,\n"
        "  meta: EntityMeta\n"
        "): Record<string, string | number | null> {\n"
        "  const values: Record<string, string | number | null> = {};\n"
        "  for (const key of meta.columns) {\n"
        "    if (!Object.prototype.hasOwnProperty.call(rec, key)) continue;\n"
        "    const value = rec[key];\n"
        '    values[key] = typeof value === "string" || typeof value === "number" ? value : null;\n'
        "  }\n"
        "  return values;\n"
        "}\n"
    )


def _parent_guard(
    entity: Entity,
    consts: dict[str, str],
    *,
    record_expr: str,
) -> str:
    policy = entity.record_policy
    if policy is None or policy.parent_lock_field is None:
        return ""
    field = next(field for field in entity.fields if field.name == policy.parent_lock_field)
    assert field.references is not None
    parent_const = consts[field.references]
    field_name = policy.parent_lock_field
    return (
        f"  const parentId = Number({record_expr}[{_ts_string(field_name)}]);\n"
        "  if (!Number.isInteger(parentId) || parentId <= 0) "
        'return json({ error: "invalid parent record" }, 400);\n'
        "  const parentRows = await db.select({ locked: "
        f"{parent_const}.locked }}).from({parent_const}).where(eq({parent_const}.id, parentId))"
        ".limit(1).all();\n"
        '  if (!parentRows[0]) return json({ error: "parent record not found" }, 409);\n'
        "  if (parentRows[0].locked === 1 && !hasAnyRole(session, meta.manageRoles)) "
        'return json({ error: "parent record is locked" }, 409);\n'
    )


def _ts_string(value: str) -> str:
    from ..generator import _ts

    return _ts(value)


def _emit_entity_handlers(
    entity: Entity,
    consts: dict[str, str],
    funcs: dict[str, str],
) -> str:
    const = consts[entity.id]
    func = funcs[entity.id]
    table = _records_table_name(entity)
    policy = entity.record_policy
    owner_value = (
        "\n    values.owner_user_id = session.userId;" if policy and policy.owner_managed else ""
    )
    parent_create = _parent_guard(entity, consts, record_expr="check.rec")
    parent_patch = _parent_guard(entity, consts, record_expr="merged")
    parent_delete = _parent_guard(entity, consts, record_expr="row")
    lock_handler = ""
    if policy is not None and policy.lock_roles:
        lock_handler = (
            "\n\n"
            f"async function set{func}Locked(\n"
            "  env: Env, id: number, locked: boolean, session: UserSession\n"
            "): Promise<Response> {\n"
            f"  const meta = META.{const};\n"
            "  if (!hasAnyRole(session, meta.lockRoles)) "
            'return json({ error: "forbidden" }, 403);\n'
            "  const db = drizzle(env.DB);\n"
            f"  const result = await db.update({const}).set({{ locked: locked ? 1 : 0 }})"
            f".where(eq({const}.id, id)).run();\n"
            '  if (result.meta.changes !== 1) return json({ error: "record not found" }, 404);\n'
            "  return json({ ok: true, locked });\n"
            "}\n"
        )
    return (
        f"async function insert{func}(\n"
        "  env: Env, body: unknown, session: UserSession\n"
        "): Promise<Response> {\n"
        f"  const meta = META.{const};\n"
        "  const check = validateRecord(body, meta);\n"
        "  if (!check.ok) return json({ error: check.error }, 400);\n"
        "  const db = drizzle(env.DB);\n" + parent_create + "  try {\n"
        "    const values = recordValues(check.rec, meta);" + owner_value + "\n"
        f"    await db.insert({const}).values(values as typeof {const}.$inferInsert).run();\n"
        "  } catch {\n"
        '    return json({ error: "could not save record" }, 409);\n'
        "  }\n"
        "  return json({ ok: true }, 201);\n"
        "}\n\n"
        f"async function list{func}(env: Env): Promise<Response> {{\n"
        "  const db = drizzle(env.DB);\n"
        f"  const rows = await db.select().from({const})"
        f".orderBy(desc({const}.id)).limit(200).all();\n"
        f"  return json({{ {_ts_string(table)}: rows }});\n"
        "}\n\n"
        f"async function update{func}(\n"
        "  env: Env, id: number, body: unknown, session: UserSession\n"
        "): Promise<Response> {\n"
        f"  const meta = META.{const};\n"
        "  const check = validatePatch(body, meta);\n"
        "  if (!check.ok) return json({ error: check.error }, 400);\n"
        "  const db = drizzle(env.DB);\n"
        f"  const rows = await db.select().from({const})"
        f".where(eq({const}.id, id)).limit(1).all();\n"
        "  const row = rows[0] as Record<string, unknown> | undefined;\n"
        '  if (!row) return json({ error: "record not found" }, 404);\n'
        '  if (!canManageRow(session, meta, row)) return json({ error: "forbidden" }, 403);\n'
        "  if (row.locked === 1 && !hasAnyRole(session, meta.lockRoles)) "
        'return json({ error: "record is locked" }, 409);\n'
        "  const merged = { ...row, ...check.rec };\n" + parent_patch + "  try {\n"
        "    const values = patchValues(check.rec, meta);\n"
        f"    await db.update({const}).set(values as typeof {const}.$inferInsert)"
        f".where(eq({const}.id, id)).run();\n"
        "  } catch {\n"
        '    return json({ error: "could not update record" }, 409);\n'
        "  }\n"
        "  return json({ ok: true });\n"
        "}\n\n"
        f"async function delete{func}(\n"
        "  env: Env, id: number, session: UserSession\n"
        "): Promise<Response> {\n"
        f"  const meta = META.{const};\n"
        "  const db = drizzle(env.DB);\n"
        f"  const rows = await db.select().from({const})"
        f".where(eq({const}.id, id)).limit(1).all();\n"
        "  const row = rows[0] as Record<string, unknown> | undefined;\n"
        '  if (!row) return json({ error: "record not found" }, 404);\n'
        '  if (!canManageRow(session, meta, row)) return json({ error: "forbidden" }, 403);\n'
        + parent_delete
        + "  if (row.locked === 1 && !hasAnyRole(session, meta.lockRoles)) "
        'return json({ error: "record is locked" }, 409);\n'
        "  try {\n"
        f"    await db.delete({const}).where(eq({const}.id, id)).run();\n"
        "  } catch {\n"
        '    return json({ error: "record is in use" }, 409);\n'
        "  }\n"
        "  return json({ ok: true });\n"
        "}\n" + lock_handler
    )


def _emit_policy_route_table(
    entities: tuple[Entity, ...], consts: dict[str, str], funcs: dict[str, str]
) -> str:
    lines: list[str] = []
    for entity in entities:
        const = consts[entity.id]
        func = funcs[entity.id]
        path = f"/api/{_records_table_name(entity)}"
        policy = entity.record_policy
        lock_handler = (
            f"(env, id, locked, session) => set{func}Locked(env, id, locked, session)"
            if policy is not None and policy.lock_roles
            else "null"
        )
        lines.append(
            f"  {_ts_string(path)}: {{\n"
            f"    meta: META.{const},\n"
            f"    post: (env, body, session) => insert{func}(env, body, session),\n"
            f"    get: (env) => list{func}(env),\n"
            f"    patch: (env, id, body, session) => update{func}(env, id, body, session),\n"
            f"    remove: (env, id, session) => delete{func}(env, id, session),\n"
            f"    lock: {lock_handler},\n"
            "  },"
        )
    return "\n".join(lines)


def _emit_policy_helpers_ts() -> str:
    return (
        "function hasAnyRole(session: UserSession, roles: string[]): boolean {\n"
        "  return roles.includes(session.role);\n"
        "}\n\n"
        "function canManageRow(\n"
        "  session: UserSession, meta: EntityMeta, row: Record<string, unknown>\n"
        "): boolean {\n"
        "  if (hasAnyRole(session, meta.manageRoles)) return true;\n"
        "  return meta.ownerManaged && row.owner_user_id === session.userId;\n"
        "}\n\n"
        "function parsePositiveId(raw: string): number | null {\n"
        "  if (!/^[1-9][0-9]*$/.test(raw)) return null;\n"
        "  const value = Number(raw);\n"
        "  return Number.isSafeInteger(value) ? value : null;\n"
        "}\n"
    )


def _emit_role_admin_ts(app: AppSpec) -> str:
    from ..generator import _ts

    return (
        f"const ROLE_ADMIN_ROLES: string[] = {_ts(list(app.role_admin_roles))};\n\n"
        "async function listUsers(env: Env, session: UserSession): Promise<Response> {\n"
        '  if (!hasAnyRole(session, ROLE_ADMIN_ROLES)) return json({ error: "forbidden" }, 403);\n'
        "  const db = drizzle(env.DB);\n"
        "  const rows = await db.select({ id: users.id, email: users.email, role: users.role, "
        "created_at: users.created_at }).from(users).orderBy(desc(users.id)).limit(200).all();\n"
        "  return json({ users: rows });\n"
        "}\n\n"
        "async function updateUserRole(\n"
        "  env: Env, targetId: number, body: unknown, session: UserSession\n"
        "): Promise<Response> {\n"
        '  if (!hasAnyRole(session, ROLE_ADMIN_ROLES)) return json({ error: "forbidden" }, 403);\n'
        '  if (typeof body !== "object" || body === null || Array.isArray(body)) '
        'return json({ error: "request body must be a JSON object" }, 400);\n'
        "  const rec = body as Record<string, unknown>;\n"
        '  if (Object.keys(rec).length !== 1 || typeof rec.role !== "string" || '
        '!ROLES.includes(rec.role)) return json({ error: "invalid role" }, 400);\n'
        "  const db = drizzle(env.DB);\n"
        "  const result = await db.update(users).set({ role: rec.role })"
        ".where(eq(users.id, targetId)).run();\n"
        '  if (result.meta.changes !== 1) return json({ error: "user not found" }, 404);\n'
        "  return json({ ok: true, role: rec.role });\n"
        "}\n"
    )


def _emit_policy_fetch_helpers_ts() -> str:
    return (
        "type JsonBody = { ok: true; body: unknown } | { ok: false; response: Response };\n\n"
        "function sameOriginOk(request: Request, url: URL): boolean {\n"
        '  const origin = request.headers.get("Origin");\n'
        "  if (origin === null) return true;\n"
        "  try { return new URL(origin).origin === url.origin; } catch { return false; }\n"
        "}\n\n"
        "function hasJsonContentType(request: Request): boolean {\n"
        '  return (request.headers.get("Content-Type") ?? "").toLowerCase()'
        '.startsWith("application/json");\n'
        "}\n\n"
        "function requireJsonContentType(request: Request): Response | null {\n"
        "  if (hasJsonContentType(request)) return null;\n"
        '  return json({ error: "unsupported media type" }, 415);\n'
        "}\n\n"
        "async function readJsonBody(request: Request): Promise<JsonBody> {\n"
        "  if (!hasJsonContentType(request)) return { ok: false, response: "
        'json({ error: "unsupported media type" }, 415) };\n'
        "  try { return { ok: true, body: await request.json() }; }\n"
        '  catch { return { ok: false, response: json({ error: "invalid JSON" }, 400) }; }\n'
        "}\n\n"
        "function rawPathname(requestUrl: string, origin: string): string {\n"
        "  const rest = requestUrl.startsWith(origin) "
        "? requestUrl.slice(origin.length) : requestUrl;\n"
        "  const end = rest.search(/[?#]/);\n"
        "  const path = end === -1 ? rest : rest.slice(0, end);\n"
        '  return path === "" ? "/" : path;\n'
        "}\n\n"
        "function decodedPathname(pathname: string): string | null {\n"
        "  try { return decodeURIComponent(pathname); } catch { return null; }\n"
        "}\n\n"
        "function isApiPath(\n"
        "  rawPath: string, urlPath: string, decodedPath: string | null\n"
        "): boolean {\n"
        '  return rawPath.startsWith("/api/") || urlPath.startsWith("/api/") || '
        'decodedPath === null || decodedPath.startsWith("/api/");\n'
        "}\n\n"
        "interface ItemPath { route: RouteHandlers; id: number; action: string | null }\n\n"
        "function parseItemPath(rawPath: string): ItemPath | null {\n"
        '  const parts = rawPath.split("/");\n'
        '  if ((parts.length !== 4 && parts.length !== 5) || parts[1] !== "api") return null;\n'
        "  const route = ROUTES[`/api/${parts[2]}`];\n"
        '  const id = parsePositiveId(parts[3] ?? "");\n'
        "  if (!route || id === null) return null;\n"
        "  return { route, id, action: parts.length === 5 ? parts[4] : null };\n"
        "}\n\n"
        "async function requiredSession(request: Request, env: Env, roles: string[]): "
        "Promise<UserSession | Response> {\n"
        "  const session = await resolveSession(request, env);\n"
        "  const denied = authorizeSession(session, roles);\n"
        "  return denied ?? (session as UserSession);\n"
        "}\n\n"
    )


def _emit_policy_fetch_dispatch_ts() -> str:
    return (
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    const url = new URL(request.url);\n"
        "    const rawPath = rawPathname(request.url, url.origin);\n"
        "    const decodedPath = decodedPathname(rawPath);\n"
        "    const apiPath = isApiPath(rawPath, url.pathname, decodedPath);\n"
        '    if (["POST", "PATCH", "DELETE"].includes(request.method) && apiPath && '
        '!sameOriginOk(request, url)) return json({ error: "bad origin" }, 403);\n'
        '    if (rawPath === "/api/register" && request.method === "POST") {\n'
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return registerUser(request, env, parsed.body);\n"
        "    }\n"
        '    if (rawPath === "/api/login" && request.method === "POST") {\n'
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return loginUser(env, parsed.body);\n"
        "    }\n"
        '    if (rawPath === "/api/logout" && request.method === "POST") '
        "return logoutUser(request, env);\n"
        '    if (rawPath === "/api/session" && request.method === "GET") {\n'
        "      const session = await resolveSession(request, env);\n"
        "      return session === null ? json({ authenticated: false }) : "
        "json({ authenticated: true, user_id: session.userId, role: session.role });\n"
        "    }\n"
        '    if (rawPath === "/api/users" && request.method === "GET" '
        "&& ROLE_ADMIN_ROLES.length > 0) {\n"
        "      const session = await requiredSession(request, env, ROLE_ADMIN_ROLES);\n"
        "      return session instanceof Response ? session : listUsers(env, session);\n"
        "    }\n"
        "    const userRoleMatch = rawPath.match(/^\\/api\\/users\\/([1-9][0-9]*)\\/role$/);\n"
        '    if (userRoleMatch && request.method === "PATCH" && ROLE_ADMIN_ROLES.length > 0) {\n'
        "      const session = await requiredSession(request, env, ROLE_ADMIN_ROLES);\n"
        "      if (session instanceof Response) return session;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return updateUserRole(env, Number(userRoleMatch[1]), parsed.body, session);\n"
        "    }\n"
        "    const route = ROUTES[rawPath];\n"
        '    if (route && request.method === "GET") {\n'
        "      if (!route.meta.publicRead) {\n"
        "        const session = await requiredSession(request, env, route.meta.readRoles);\n"
        "        if (session instanceof Response) return session;\n"
        "      }\n"
        "      return route.get(env);\n"
        "    }\n"
        '    if (route && request.method === "POST") {\n'
        "      const session = await requiredSession(request, env, route.meta.createRoles);\n"
        "      if (session instanceof Response) return session;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return route.post(env, parsed.body, session);\n"
        "    }\n"
        "    const item = parseItemPath(rawPath);\n"
        '    if (item && item.action === null && request.method === "PATCH") {\n'
        "      const session = await requiredSession(request, env, []);\n"
        "      if (session instanceof Response) return session;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return item.route.patch(env, item.id, parsed.body, session);\n"
        "    }\n"
        '    if (item && item.action === null && request.method === "DELETE") {\n'
        "      const session = await requiredSession(request, env, []);\n"
        "      return session instanceof Response ? session : "
        "item.route.remove(env, item.id, session);\n"
        "    }\n"
        '    if (item && (item.action === "lock" || item.action === "unlock") && '
        'request.method === "POST" && item.route.lock !== null) {\n'
        "      const session = await requiredSession(request, env, item.route.meta.lockRoles);\n"
        "      return session instanceof Response ? session : "
        'item.route.lock(env, item.id, item.action === "lock", session);\n'
        "    }\n"
        '    if (apiPath) return json({ error: "not found" }, 404);\n'
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_policy_fetch_ts() -> str:
    return _emit_policy_fetch_helpers_ts() + _emit_policy_fetch_dispatch_ts()


def emit_records_policy_worker_ts(app: AppSpec, form_entities: tuple[Entity, ...] = ()) -> str:
    """Emit the policy-aware auth Worker for a prepared records AppSpec."""
    from ..form_primitive import _form_const_name
    from ..generator import _ts

    ordered = _fk_order_entities(app.entities)
    consts = _ts_const_names(app.entities)
    funcs = _function_names(app.entities)
    imports = ", ".join(
        (
            "users",
            "sessions",
            *(consts[entity.id] for entity in ordered),
            *(_form_const_name(entity) for entity in form_entities),
        )
    )
    handlers = "\n\n".join(_emit_entity_handlers(entity, consts, funcs) for entity in ordered)
    return (
        "/* Auto-generated Cloudflare Worker (records primitive): trusted row policy. */\n"
        'import { desc, eq } from "drizzle-orm";\n'
        'import { drizzle } from "drizzle-orm/d1";\n'
        f'import {{ {imports} }} from "../src/db/schema";\n\n'
        "export interface Env {\n"
        "  DB: D1Database;\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "  ADMIN_TOKEN?: string;\n"
        "}\n\n"
        "interface EntityMeta {\n"
        "  table: string; columns: string[]; required: string[]; integers: string[];\n"
        "  reals: string[]; emails: string[]; publicRead: boolean; readRoles: string[];\n"
        "  createRoles: string[]; ownerManaged: boolean; manageRoles: string[];\n"
        "  lockRoles: string[]; parentLockField: string | null;\n"
        "}\n\n"
        "const META: Record<string, EntityMeta> = {\n"
        + _emit_policy_meta(ordered, consts)
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
        + _emit_patch_validation_ts()
        + "\n"
        + _emit_auth_validation_ts()
        + "\n"
        + _emit_auth_crypto_ts()
        + "\n"
        + _emit_auth_session_ts()
        + "\n"
        + _emit_auth_endpoints_ts()
        + "\n"
        + _emit_policy_helpers_ts()
        + "\n\n"
        + _emit_role_admin_ts(app)
        + "\n\n"
        + handlers
        + "\n\n"
        "interface RouteHandlers {\n"
        "  meta: EntityMeta;\n"
        "  post: (env: Env, body: unknown, session: UserSession) => Promise<Response>;\n"
        "  get: (env: Env) => Promise<Response>;\n"
        "  patch: (env: Env, id: number, body: unknown, session: UserSession) "
        "=> Promise<Response>;\n"
        "  remove: (env: Env, id: number, session: UserSession) => Promise<Response>;\n"
        "  lock: ((env: Env, id: number, locked: boolean, session: UserSession) "
        "=> Promise<Response>) | null;\n"
        "}\n\n"
        "const ROUTES: Record<string, RouteHandlers> = {\n"
        + _emit_policy_route_table(ordered, consts, funcs)
        + "\n};\n\n"
        + _emit_policy_fetch_ts()
    )


__all__ = ["emit_records_policy_worker_ts", "records_policy_enabled"]
