"""The records-auth Worker orchestrator + its wrangler.toml override.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import AppSpec, Entity
from .auth_crypto import _emit_auth_crypto_ts
from .auth_endpoints import _emit_auth_endpoints_ts
from .auth_fetch import _emit_auth_fetch_ts
from .auth_session import _emit_auth_session_ts
from .auth_validation import _emit_auth_validation_ts
from .naming import _fk_order_entities, _function_names, _ts_const_names
from .worker_meta import (
    _emit_auth_route_table,
    _emit_validate_record_ts,
    _emit_worker_handlers,
    _emit_worker_meta,
)


def _emit_records_auth_worker_ts(app: AppSpec, form_entities: tuple[Entity, ...] = ()) -> str:
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
            *(_form_const_name(e) for e in form_entities),
        )
    )
    stripe_enabled = app.stripe is not None
    webhook_enabled = app.webhooks is not None
    svc_import = (
        'import { svc } from "./disco-client";\n' if stripe_enabled or webhook_enabled else ""
    )
    if stripe_enabled:
        from ..stripe_worker import emit_stripe_env_ts, emit_stripe_worker_ts

        stripe_env = emit_stripe_env_ts()
        stripe_worker = emit_stripe_worker_ts(app) + "\n"
    else:
        stripe_env = ""
        stripe_worker = ""
    if webhook_enabled:
        from ..webhook_worker import emit_webhook_env_ts, emit_webhook_worker_ts

        webhook_env = emit_webhook_env_ts()
        webhook_worker = emit_webhook_worker_ts(app) + "\n"
    else:
        webhook_env = ""
        webhook_worker = ""
    base_roles = (
        "const BASE_ROLES: string[] = ROLES.filter((role) => role !== STRIPE_ENTITLEMENT);\n"
        if stripe_enabled
        else ""
    )
    return (
        "/* Auto-generated Cloudflare Worker (records primitive): session auth + RBAC.\n"
        "   When roles are declared, every entity read/write requires a valid per-user\n"
        "   session. Entity readRoles/writeRoles narrow access by user.role. */\n"
        'import { desc, eq } from "drizzle-orm";\n'
        'import { drizzle } from "drizzle-orm/d1";\n'
        f'import {{ {imports} }} from "../src/db/schema";\n' + svc_import + "\n"
        "export interface Env {\n"
        "  DB: D1Database;\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "  ADMIN_TOKEN?: string;\n" + stripe_env + webhook_env + "}\n\n"
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
        + base_roles
        + "const MAX_FIELD_LEN = 2000;\n"
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
        + _emit_auth_validation_ts(role_collection="BASE_ROLES" if stripe_enabled else "ROLES")
        + "\n"
        + _emit_auth_crypto_ts()
        + "\n"
        + _emit_auth_session_ts(include_role_grants=stripe_enabled)
        + "\n"
        + _emit_auth_endpoints_ts()
        + "\n"
        + _emit_worker_handlers(ordered, consts, funcs)
        + "\n\n"
        + stripe_worker
        + webhook_worker
        + "interface RouteHandlers {\n"
        "  writeRoles: string[];\n"
        "  readRoles: string[];\n"
        "  post: (env: Env, body: unknown) => Promise<Response>;\n"
        "  get: (env: Env) => Promise<Response>;\n"
        "}\n\n"
        "const ROUTES: Record<string, RouteHandlers> = {\n"
        + _emit_auth_route_table(ordered, funcs)
        + "\n};\n\n"
        + _emit_auth_fetch_ts(
            include_stripe=stripe_enabled, webhook_app=app if webhook_enabled else None
        )
    )


def _emit_records_auth_wrangler_toml(app: AppSpec, lead: Entity) -> str:
    from ..generator import _emit_wrangler_toml

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
