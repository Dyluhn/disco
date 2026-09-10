"""Disco-owned Stripe data-plane emitters for records apps (WO-F4.1).

This module emits code; it never handles a credential.  All generated secret
references are Worker ``Env`` binding names, and all price configuration remains
behind the host-side ``payments.checkout`` service.
"""

from __future__ import annotations

import json

from .spec import AppSpec, Section, StripeMeta


def _stripe_meta(app: AppSpec) -> StripeMeta:
    meta = app.stripe
    if meta is None:
        raise ValueError("Stripe worker emission requires AppSpec.stripe metadata")
    return meta


def emit_stripe_schema_sql() -> str:
    """Tables for deduplication, fulfillment state, and source-aware grants."""
    return (
        'CREATE TABLE IF NOT EXISTS "user_role_grants" (\n'
        '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  "user_id" INTEGER NOT NULL,\n'
        '  "role" TEXT NOT NULL,\n'
        '  "source" TEXT NOT NULL,\n'
        '  "source_id" TEXT NOT NULL,\n'
        '  "active" INTEGER NOT NULL DEFAULT 1 CHECK("active" IN (0, 1)),\n'
        "  \"updated_at\" TEXT NOT NULL DEFAULT(datetime('now')),\n"
        '  UNIQUE("user_id", "role", "source", "source_id"),\n'
        '  FOREIGN KEY("user_id") REFERENCES "users"("id")\n'
        ");\n\n"
        'CREATE INDEX IF NOT EXISTS "user_role_grants_active"\n'
        '  ON "user_role_grants"("user_id", "active");\n\n'
        'CREATE TABLE IF NOT EXISTS "stripe_events" (\n'
        '  "event_id" TEXT PRIMARY KEY,\n'
        '  "event_type" TEXT NOT NULL,\n'
        "  \"processed_at\" TEXT NOT NULL DEFAULT(datetime('now'))\n"
        ");\n\n"
        'CREATE TABLE IF NOT EXISTS "stripe_fulfillments" (\n'
        '  "source_id" TEXT PRIMARY KEY,\n'
        '  "user_id" INTEGER NOT NULL,\n'
        '  "entitlement_flag" TEXT NOT NULL,\n'
        '  "active" INTEGER NOT NULL CHECK("active" IN (0, 1)),\n'
        '  "event_created" INTEGER NOT NULL CHECK("event_created" > 0),\n'
        "  \"updated_at\" TEXT NOT NULL DEFAULT(datetime('now')),\n"
        '  FOREIGN KEY("user_id") REFERENCES "users"("id")\n'
        ");"
    )


def emit_stripe_drizzle_ts() -> str:
    """Drizzle declarations mirroring the Stripe-owned migration tables."""
    return (
        'export const userRoleGrants = sqliteTable("user_role_grants", {\n'
        '  id: integer("id").primaryKey({ autoIncrement: true }),\n'
        '  user_id: integer("user_id").notNull().references(() => users.id),\n'
        '  role: text("role").notNull(),\n'
        '  source: text("source").notNull(),\n'
        '  source_id: text("source_id").notNull(),\n'
        '  active: integer("active").notNull().default(1),\n'
        "  updated_at: text(\"updated_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});\n\n"
        'export const stripeEvents = sqliteTable("stripe_events", {\n'
        '  event_id: text("event_id").primaryKey(),\n'
        '  event_type: text("event_type").notNull(),\n'
        "  processed_at: text(\"processed_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});\n\n"
        'export const stripeFulfillments = sqliteTable("stripe_fulfillments", {\n'
        '  source_id: text("source_id").primaryKey(),\n'
        '  user_id: integer("user_id").notNull().references(() => users.id),\n'
        '  entitlement_flag: text("entitlement_flag").notNull(),\n'
        '  active: integer("active").notNull(),\n'
        '  event_created: integer("event_created").notNull(),\n'
        "  updated_at: text(\"updated_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});"
    )


def emit_stripe_env_ts() -> str:
    return (
        "  // Stripe runtime bindings are host-injected; values never enter the tree.\n"
        "  STRIPE_WEBHOOK_SECRET?: string;\n"
        "  STRIPE_APP_BINDING_SECRET?: string;\n"
        "  STRIPE_RUNTIME_READY?: string;\n"
    )


def _emit_stripe_runtime_ts(meta: StripeMeta) -> str:
    """Public constants plus fail-closed runtime binding and freshness checks."""
    flag = json.dumps(meta.entitlement_flag)
    app_binding = json.dumps(meta.app_binding)
    selector = json.dumps(meta.plan_selector)
    message = json.dumps(meta.success_message)
    return rf"""const STRIPE_APP_BINDING = {app_binding};
const STRIPE_ENTITLEMENT = {flag};
const STRIPE_PLAN_SELECTOR = {selector};
const STRIPE_SUCCESS_MESSAGE = {message};
const STRIPE_SIGNATURE_TOLERANCE_SECONDS = 300;
const MAX_STRIPE_WEBHOOK_BYTES = 256 * 1024;
const MAX_STRIPE_V1_SIGNATURES = 8;
const STRIPE_EVENT_ID_RE = /^evt_[A-Za-z0-9]{{1,128}}$/;
const STRIPE_OBJECT_ID_RE = /^[A-Za-z][A-Za-z0-9_]{{1,127}}$/;

interface StripeEnvelope {{
  id: string;
  created: number;
  type: string;
  data: {{ object: Record<string, unknown> }};
}}

function stripeBusBindingsValid(env: Env): boolean {{
  const token = env.DISCO_SVC_TOKEN;
  if (typeof token !== "string"
      || !/^(?:a2v0|a4v1)\.[A-Za-z0-9_-]{{22}}\.[A-Za-z0-9_-]{{43}}$/.test(token)) return false;
  if (typeof env.DISCO_SVC_BUS !== "string") return false;
  let bus: URL;
  try {{ bus = new URL(env.DISCO_SVC_BUS); }} catch {{ return false; }}
  const octets = bus.hostname.split(".");
  const loopbackV4 = octets.length === 4 && octets[0] === "127"
    && octets.every((part) => /^(?:0|[1-9][0-9]{{0,2}})$/.test(part))
    && octets.every((part) => Number(part) <= 255);
  const loopback = bus.hostname === "localhost" || bus.hostname === "[::1]"
    || loopbackV4;
  return (bus.protocol === "https:" || (bus.protocol === "http:" && loopback))
    && !bus.username && !bus.password && !bus.search && !bus.hash
    && (bus.pathname === "" || bus.pathname === "/");
}}

function stripeRuntimeBindingsReady(env: Env): boolean {{
  return typeof env.STRIPE_WEBHOOK_SECRET === "string"
    && env.STRIPE_WEBHOOK_SECRET.length > 0
    && typeof env.STRIPE_APP_BINDING_SECRET === "string"
    && env.STRIPE_APP_BINDING_SECRET.length >= 32
    && env.STRIPE_APP_BINDING_SECRET.length <= 512
    && stripeBusBindingsValid(env);
}}

function stripeRuntimeReady(env: Env): boolean {{
  return env.STRIPE_RUNTIME_READY === "1" && stripeRuntimeBindingsReady(env);
}}

async function stripeRuntimeProof(secret: string): Promise<string> {{
  const input = new TextEncoder().encode(
    `stripe-runtime\0${{STRIPE_APP_BINDING}}\0${{STRIPE_PLAN_SELECTOR}}`,
  );
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), {{ name: "HMAC", hash: "SHA-256" }}, false, ["sign"],
  );
  const bytes = new Uint8Array(await crypto.subtle.sign("HMAC", key, input));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}}

async function stripeRuntimeProofs(
  env: Env,
): Promise<{{ binding_proof: string; webhook_proof: string }} | null> {{
  if (!stripeRuntimeBindingsReady(env)) return null;
  const binding = env.STRIPE_APP_BINDING_SECRET;
  const webhook = env.STRIPE_WEBHOOK_SECRET;
  if (typeof binding !== "string" || typeof webhook !== "string") return null;
  return {{
    binding_proof: await stripeRuntimeProof(binding),
    webhook_proof: await stripeRuntimeProof(webhook),
  }};
}}
"""


def _emit_stripe_signature_verify_ts() -> str:
    """JSON reply helper, signature-header parsing, raw body read, and HMAC
    signature verification — all envelope-independent."""
    return f"""{""}function stripeJson(
  data: unknown,
  status = 200,
): Response {{
  return json(data, status, {{ "Cache-Control": "no-store" }});
}}

function parseStripeSignature(
  header: string | null,
): {{ timestamp: number; v1: Uint8Array[] }} | null {{
  if (header === null || header.length === 0 || header.length > 4096) return null;
  const fields = header.split(",");
  let timestamp: number | null = null;
  const signatures: Uint8Array[] = [];
  for (const field of fields) {{
    const parts = field.split("=");
    if (parts.length !== 2) return null;
    const [key, value] = parts;
    if (key === "t") {{
      if (timestamp !== null || !/^[1-9][0-9]{{0,11}}$/.test(value)) return null;
      const parsed = Number(value);
      if (!Number.isSafeInteger(parsed)) return null;
      timestamp = parsed;
    }} else if (key === "v1") {{
      if (signatures.length >= MAX_STRIPE_V1_SIGNATURES) return null;
      if (!/^[0-9a-f]{{64}}$/.test(value)) return null;
      const bytes = new Uint8Array(32);
      for (let i = 0; i < bytes.length; i += 1) {{
        bytes[i] = Number.parseInt(value.slice(i * 2, i * 2 + 2), 16);
      }}
      signatures.push(bytes);
    }} else {{
      return null;
    }}
  }}
  if (timestamp === null || signatures.length === 0) return null;
  return {{ timestamp, v1: signatures }};
}}

async function readStripeBody(request: Request): Promise<Uint8Array | null> {{
  if (request.body === null) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {{
    while (true) {{
      const {{ done, value }} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_STRIPE_WEBHOOK_BYTES) {{
        await reader.cancel();
        return null;
      }}
      chunks.push(value);
    }}
  }} catch {{
    try {{ await reader.cancel(); }} catch {{ /* no request data in errors */ }}
    return null;
  }}
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {{ body.set(chunk, offset); offset += chunk.byteLength; }}
  return body;
}}

async function stripeSignatureValid(
  env: Env,
  header: string | null,
  body: Uint8Array,
): Promise<boolean> {{
  const secret = env.STRIPE_WEBHOOK_SECRET;
  if (typeof secret !== "string" || secret.length === 0) return false;
  const parsed = parseStripeSignature(header);
  if (parsed === null) return false;
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - parsed.timestamp) > STRIPE_SIGNATURE_TOLERANCE_SECONDS) return false;
  const timestamp = new TextEncoder().encode(`${{parsed.timestamp}}.`);
  const signed = new Uint8Array(timestamp.byteLength + body.byteLength);
  signed.set(timestamp, 0);
  signed.set(body, timestamp.byteLength);
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), {{ name: "HMAC", hash: "SHA-256" }}, false, ["verify"],
  );
  for (const signature of parsed.v1) {{
    if (await crypto.subtle.verify("HMAC", key, signature, signed)) return true;
  }}
  return false;
}}
"""


def _emit_stripe_envelope_parse_ts() -> str:
    """Trusted-envelope parsing plus source-id and user-id extraction."""
    return f"""{""}
function parseStripeEnvelope(body: Uint8Array): StripeEnvelope | null {{
  let value: unknown;
  try {{ value = JSON.parse(new TextDecoder("utf-8", {{ fatal: true }}).decode(body)); }} catch {{
    return null;
  }}
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const event = value as Record<string, unknown>;
  if (typeof event.id !== "string" || !STRIPE_EVENT_ID_RE.test(event.id)) return null;
  if (typeof event.created !== "number" || !Number.isSafeInteger(event.created)
      || event.created <= 0) return null;
  if (typeof event.type !== "string" || event.type.length > 128) return null;
  if (typeof event.data !== "object" || event.data === null
      || Array.isArray(event.data)) return null;
  const object = (event.data as Record<string, unknown>).object;
  if (typeof object !== "object" || object === null || Array.isArray(object)) return null;
  return {{
    id: event.id,
    created: event.created,
    type: event.type,
    data: {{ object: object as Record<string, unknown> }},
  }};
}}

function stripeSourceId(event: StripeEnvelope): string | null {{
  const object = event.data.object;
  const candidate = event.type.startsWith("checkout.session.")
    ? (typeof object.subscription === "string" ? object.subscription : object.id)
    : object.id;
  return typeof candidate === "string" && STRIPE_OBJECT_ID_RE.test(candidate) ? candidate : null;
}}

function stripeUserId(event: StripeEnvelope): number | null {{
  const raw = event.data.object.client_reference_id;
  if (typeof raw !== "string" || !/^[1-9][0-9]{{0,14}}$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isSafeInteger(value) ? value : null;
}}
"""


def _emit_stripe_verification_ts(meta: StripeMeta) -> str:
    """Raw-body, signature, and envelope verification code."""
    return (
        _emit_stripe_runtime_ts(meta)
        + _emit_stripe_signature_verify_ts()
        + _emit_stripe_envelope_parse_ts()
    )


def _emit_stripe_correlation_ts() -> str:
    """Immutable app/plan/user metadata correlation verification."""
    return f"""{""}
function stripeMetadata(event: StripeEnvelope): Record<string, string> | null {{
  const raw = event.data.object.metadata;
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const keys = Object.keys(raw).sort();
  const expected = ["disco_app_binding", "disco_correlation", "disco_plan_selector"];
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {{
    return null;
  }}
  const metadata = raw as Record<string, unknown>;
  if (!expected.every((key) => typeof metadata[key] === "string")) return null;
  return metadata as Record<string, string>;
}}

async function stripeCorrelationValid(
  env: Env,
  event: StripeEnvelope,
  userId: number,
): Promise<boolean> {{
  const secret = env.STRIPE_APP_BINDING_SECRET;
  const metadata = stripeMetadata(event);
  if (typeof secret !== "string" || secret.length < 32 || secret.length > 512
      || metadata === null
      || metadata.disco_app_binding !== STRIPE_APP_BINDING
      || metadata.disco_plan_selector !== STRIPE_PLAN_SELECTOR
      || !/^[0-9a-f]{{64}}$/.test(metadata.disco_correlation)) return false;
  const input = new TextEncoder().encode(
    `${{STRIPE_APP_BINDING}}\0${{STRIPE_PLAN_SELECTOR}}\0${{userId}}`,
  );
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), {{ name: "HMAC", hash: "SHA-256" }}, false, ["verify"],
  );
  const signature = new Uint8Array(32);
  for (let i = 0; i < signature.length; i += 1) {{
    signature[i] = Number.parseInt(metadata.disco_correlation.slice(i * 2, i * 2 + 2), 16);
  }}
  return crypto.subtle.verify("HMAC", key, signature, input);
}}
"""


def _emit_stripe_fulfillment_lookup_ts() -> str:
    """Replay-dedup and known-user lookups shared by fulfillment application."""
    return f"""{""}
async function stripeEventAlreadyRecorded(
  db: D1DatabaseSession,
  eventId: string,
): Promise<boolean> {{
  const row = await db.prepare(
    "SELECT event_id FROM stripe_events WHERE event_id = ? LIMIT 1",
  ).bind(eventId).first();
  return row !== null;
}}

async function stripeKnownUserId(
  db: D1DatabaseSession,
  sourceId: string,
): Promise<number | null> {{
  const row = await db.prepare(
    "SELECT user_id FROM stripe_fulfillments "
    + "WHERE source_id = ? AND entitlement_flag = ? LIMIT 1",
  ).bind(sourceId, STRIPE_ENTITLEMENT).first<{{ user_id: number }}>();
  return row !== null && Number.isSafeInteger(row.user_id) && row.user_id > 0
    ? row.user_id : null;
}}
"""


def _emit_stripe_apply_event_ts() -> str:
    """Atomic grant/revoke application: dedupe insert, fulfillment upsert, and
    the resulting user_role_grants write."""
    return f"""{""}
async function applyStripeEvent(env: Env, event: StripeEnvelope): Promise<Response> {{
  const sourceId = stripeSourceId(event);
  const isGrant = event.type === "checkout.session.completed"
    || event.type === "checkout.session.async_payment_succeeded";
  const isRevoke = event.type === "customer.subscription.deleted"
    || event.type === "checkout.session.expired"
    || event.type === "checkout.session.async_payment_failed";
  if (!isGrant && !isRevoke) return stripeJson({{ ok: true }});
  if (sourceId === null) return stripeJson({{ ok: true }});
  const db = env.DB.withSession("first-primary");
  // A completed Checkout Session is not proof of payment for delayed methods;
  // only paid sessions (including the async-success event) can grant.
  if (isGrant && event.data.object.payment_status !== "paid") {{
    return stripeJson({{ ok: true }});
  }}
  let userId = stripeUserId(event);
  if (userId === null && isRevoke) userId = await stripeKnownUserId(db, sourceId);
  if (userId === null) return stripeJson({{ ok: true }});
  const trustedSubscriptionRevoke = event.type === "customer.subscription.deleted"
    && stripeUserId(event) === null
    && await stripeKnownUserId(db, sourceId) === userId;
  if (!trustedSubscriptionRevoke && !(await stripeCorrelationValid(env, event, userId))) {{
    // Account-wide Stripe endpoints can deliver signed events for other apps.
    // Acknowledge unrelated bindings so Stripe does not retry them forever.
    return stripeJson({{ ok: true }});
  }}

  const statements = [
    db.prepare("INSERT INTO stripe_events (event_id, event_type) VALUES (?, ?)")
      .bind(event.id, event.type),
    db.prepare(
      "INSERT INTO stripe_fulfillments "
      + "(source_id, user_id, entitlement_flag, active, event_created) "
      + "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
      + "active = excluded.active, event_created = excluded.event_created, "
      + "updated_at = datetime('now') WHERE user_id = excluded.user_id "
      + "AND entitlement_flag = excluded.entitlement_flag AND "
      + "(excluded.event_created > stripe_fulfillments.event_created OR "
      + "(excluded.event_created = stripe_fulfillments.event_created "
      + "AND excluded.active < stripe_fulfillments.active))",
    ).bind(sourceId, userId, STRIPE_ENTITLEMENT, isGrant ? 1 : 0, event.created),
  ];
  if (isGrant) {{
    statements.push(
      db.prepare(
        "INSERT INTO user_role_grants (user_id, role, source, source_id, active) "
        + "SELECT ?, ?, 'stripe', ?, 1 FROM stripe_fulfillments "
        + "WHERE source_id = ? AND user_id = ? AND entitlement_flag = ? "
        + "AND active = 1 AND event_created = ? "
        + "ON CONFLICT(user_id, role, source, source_id) DO UPDATE SET "
        + "active = 1, updated_at = datetime('now') WHERE EXISTS "
        + "(SELECT 1 FROM stripe_fulfillments WHERE source_id = ? AND user_id = ? "
        + "AND entitlement_flag = ? AND active = 1 AND event_created = ?)",
      ).bind(
        userId, STRIPE_ENTITLEMENT, sourceId,
        sourceId, userId, STRIPE_ENTITLEMENT, event.created,
        sourceId, userId, STRIPE_ENTITLEMENT, event.created,
      ),
    );
  }} else {{
    statements.push(
      db.prepare(
        "UPDATE user_role_grants SET active = 0, updated_at = datetime('now') "
        + "WHERE source = 'stripe' AND source_id = ? AND role = ? AND user_id = ? "
        + "AND EXISTS (SELECT 1 FROM stripe_fulfillments WHERE source_id = ? "
        + "AND user_id = ? AND entitlement_flag = ? AND active = 0 "
        + "AND event_created = ?)",
      ).bind(
        sourceId, STRIPE_ENTITLEMENT, userId,
        sourceId, userId, STRIPE_ENTITLEMENT, event.created,
      ),
    );
  }}
  try {{
    // D1 batch is atomic.  The unique event INSERT is intentionally first:
    // a replay aborts the whole batch before any fulfillment/grant write.
    await db.batch(statements);
    return stripeJson({{ ok: true }});
  }} catch {{
    // Same primary-session lookup distinguishes a committed replay from a
    // failure whose event insert rolled back.  Never acknowledge lost work.
    if (await stripeEventAlreadyRecorded(db, event.id)) return stripeJson({{ ok: true }});
    return stripeJson({{ error: "fulfillment failed" }}, 500);
  }}
}}
"""


def _emit_stripe_webhook_handler_ts() -> str:
    """Signature verification, host-readiness gate, and event dispatch."""
    return f"""{""}
async function stripeWebhook(request: Request, env: Env): Promise<Response> {{
  if (env.STRIPE_RUNTIME_READY !== "1") {{
    return stripeJson({{ error: "payments unavailable" }}, 503);
  }}
  const body = await readStripeBody(request);
  if (body === null || !(await stripeSignatureValid(
    env, request.headers.get("Stripe-Signature"), body,
  ))) {{
    console.warn("stripe webhook rejected: signature invalid");
    return stripeJson({{ error: "invalid signature" }}, 400);
  }}
  // Signature authentication proves Stripe sent the event, but not that this
  // Worker still carries the current host-managed secret generation. Refuse
  // side effects during a rotation mismatch; Stripe will retry after deploy.
  if (!(await stripeHostReady(env))) {{
    return stripeJson({{ error: "payments unavailable" }}, 503);
  }}
  // Body becomes trusted JSON only after HMAC + freshness verification.
  const event = parseStripeEnvelope(body);
  if (event === null) return stripeJson({{ error: "invalid event" }}, 400);
  return applyStripeEvent(env, event);
}}
"""


def _emit_stripe_fulfillment_ts() -> str:
    """Atomic fulfillment/revocation and replay handling."""
    return (
        _emit_stripe_fulfillment_lookup_ts()
        + _emit_stripe_apply_event_ts()
        + _emit_stripe_webhook_handler_ts()
    )


def _emit_stripe_endpoints_ts() -> str:
    """Verified status and host-service checkout endpoints."""
    return f"""{""}
async function stripeHostReady(env: Env): Promise<boolean> {{
  const proofs = await stripeRuntimeProofs(env);
  if (proofs === null) return false;
  try {{
    const result = await svc(env, "payments.ready", {{
      plan_selector: STRIPE_PLAN_SELECTOR,
      ...proofs,
    }});
    return typeof result === "object" && result !== null && !Array.isArray(result)
      && Object.keys(result).length === 1
      && (result as Record<string, unknown>).ready === true;
  }} catch {{
    return false;
  }}
}}

async function stripeStatus(request: Request, env: Env): Promise<Response> {{
  const session = await resolveSession(request, env);
  if (session === null) {{
    return stripeJson({{
      ready: false, authenticated: false, entitled: false, message: null,
    }});
  }}
  const ready = stripeRuntimeReady(env) && await stripeHostReady(env);
  const entitled = session.roles.includes(STRIPE_ENTITLEMENT);
  return stripeJson({{
    ready,
    authenticated: true,
    entitled,
    message: entitled ? STRIPE_SUCCESS_MESSAGE : null,
  }});
}}

async function stripeCheckout(
  request: Request,
  env: Env,
  body: unknown,
): Promise<Response> {{
  if (typeof body !== "object" || body === null || Array.isArray(body)
      || Object.keys(body).length !== 0) {{
    return stripeJson({{ error: "invalid checkout request" }}, 400);
  }}
  if (!stripeRuntimeReady(env)) return stripeJson({{ error: "payments unavailable" }}, 503);
  const session = await resolveSession(request, env);
  if (session === null) return stripeJson({{ error: "unauthorized" }}, 401);
  let result: unknown;
  try {{
    const proofs = await stripeRuntimeProofs(env);
    if (proofs === null) return stripeJson({{ error: "payments unavailable" }}, 503);
    result = await svc(env, "payments.checkout", {{
      plan_selector: STRIPE_PLAN_SELECTOR,
      user_id: session.userId,
      success_path: "/",
      cancel_path: "/",
      ...proofs,
    }});
  }} catch {{
    return stripeJson({{ error: "checkout unavailable" }}, 502);
  }}
  if (typeof result !== "object" || result === null || Array.isArray(result)) {{
    return stripeJson({{ error: "checkout unavailable" }}, 502);
  }}
  const checkoutUrl = (result as Record<string, unknown>).url;
  if (typeof checkoutUrl !== "string") return stripeJson({{ error: "checkout unavailable" }}, 502);
  let parsed: URL;
  try {{ parsed = new URL(checkoutUrl); }} catch {{
    return stripeJson({{ error: "checkout unavailable" }}, 502);
  }}
  if (parsed.protocol !== "https:" || parsed.hostname !== "checkout.stripe.com"
      || parsed.username || parsed.password || parsed.port !== "") {{
    return stripeJson({{ error: "checkout unavailable" }}, 502);
  }}
  return stripeJson({{ url: parsed.toString() }});
}}

async function stripeRuntimeProbe(request: Request, env: Env): Promise<Response> {{
  if (!(await isAdminAuthorized(request, env))) {{
    return stripeJson({{ error: "unauthorized" }}, 401);
  }}
  return stripeJson({{ ready: await stripeHostReady(env) }});
}}
"""


def emit_stripe_worker_ts(app: AppSpec) -> str:
    """Webhook, checkout proxy, and verified-entitlement status functions."""
    meta = _stripe_meta(app)
    return (
        _emit_stripe_verification_ts(meta)
        + _emit_stripe_correlation_ts()
        + _emit_stripe_fulfillment_ts()
        + _emit_stripe_endpoints_ts()
    )


def emit_stripe_webhook_route_ts() -> str:
    """Signature-authenticated inbound route; must precede browser CSRF gates."""
    return (
        '    if (rawPath === "/api/stripe/webhook" && request.method === "POST") {\n'
        "      return stripeWebhook(request, env);\n"
        "    }\n"
    )


def emit_stripe_browser_routes_ts() -> str:
    """Same-origin browser routes inserted after the shared CSRF gate."""
    return (
        '    if (rawPath === "/api/stripe/runtime-probe" && request.method === "GET") {\n'
        "      return stripeRuntimeProbe(request, env);\n"
        "    }\n"
        '    if (rawPath === "/api/stripe/status" && request.method === "GET") {\n'
        "      return stripeStatus(request, env);\n"
        "    }\n"
        '    if (rawPath === "/api/stripe/checkout" && request.method === "POST") {\n'
        "      const contentTypeError = requireJsonContentType(request);\n"
        "      if (contentTypeError !== null) return contentTypeError;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return stripeCheckout(request, env, parsed.body);\n"
        "    }\n"
    )


def emit_stripe_pricing_component(component_name: str, section: Section) -> str:
    """Browser UI: relative Worker endpoints only; never host-bus env or secrets."""
    content = section.content
    heading = json.dumps(content.heading if content and content.heading else "Your plan")
    price = json.dumps(content.subheading if content and content.subheading else "")
    items = json.dumps(list(content.items if content else ()))
    return f"""import {{ useEffect, useState }} from "react";

type PaymentStatus = {{
  ready: boolean; authenticated: boolean; entitled: boolean; message: string | null;
}};

export function {component_name}() {{
  const [status, setStatus] = useState<PaymentStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {{
    let live = true;
    fetch("/api/stripe/status", {{ credentials: "same-origin" }})
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((value: unknown) => {{
        if (!live || typeof value !== "object" || value === null || Array.isArray(value)) return;
        const rec = value as Record<string, unknown>;
        if (typeof rec.ready === "boolean" && typeof rec.authenticated === "boolean"
            && typeof rec.entitled === "boolean"
            && (typeof rec.message === "string" || rec.message === null)) {{
          setStatus({{
            ready: rec.ready, authenticated: rec.authenticated,
            entitled: rec.entitled, message: rec.message,
          }});
        }}
      }})
      .catch(() => {{ if (live) setStatus({{
        ready: false, authenticated: false, entitled: false, message: null,
      }}); }});
    return () => {{ live = false; }};
  }}, []);

  async function checkout() {{
    if (!status?.ready || !status.authenticated || busy) return;
    setBusy(true);
    setError(null);
    try {{
      const response = await fetch("/api/stripe/checkout", {{
        method: "POST", credentials: "same-origin",
        headers: {{ "Content-Type": "application/json" }}, body: "{{}}",
      }});
      const value: unknown = await response.json();
      if (!response.ok || typeof value !== "object" || value === null || Array.isArray(value)) {{
        throw new Error("checkout unavailable");
      }}
      const url = (value as Record<string, unknown>).url;
      if (typeof url !== "string") throw new Error("checkout unavailable");
      window.location.assign(url);
    }} catch {{
      setError("Checkout is temporarily unavailable.");
      setBusy(false);
    }}
  }}

  const features = {items};
  const ready = status?.ready === true && status.authenticated;
  const buttonLabel = status === null
    ? "Checking payment status…"
    : !status.authenticated
      ? "Sign in to choose plan"
      : !status.ready
        ? "Payments setup pending"
        : busy ? "Opening checkout…" : "Choose plan";
  return <section className="section pricing-section" data-appkit-section="stripe_pricing">
    <h2>{heading}</h2>
    <p className="section-subheading">{price}</p>
    {{features.length > 0 && <ul className="feature-grid">
      {{features.map((item) => <li key={{item}}>{{item}}</li>)}}
    </ul>}}
    {{status?.entitled && status.message
      ? <p role="status">{{status.message}}</p>
      : <>
        <button type="button" onClick={{checkout}} disabled={{!ready || busy}}
          aria-disabled={{!ready || busy}}>
          {{buttonLabel}}
        </button>
        {{status !== null && !status.authenticated
          ? <p>Sign in to continue to secure checkout.</p>
          : !ready && <p>Checkout activates after payment setup.</p>}}
        {{error && <p role="alert">{{error}}</p>}}
      </>}}
  </section>;
}}
"""


__all__ = [
    "emit_stripe_browser_routes_ts",
    "emit_stripe_drizzle_ts",
    "emit_stripe_env_ts",
    "emit_stripe_pricing_component",
    "emit_stripe_schema_sql",
    "emit_stripe_worker_ts",
    "emit_stripe_webhook_route_ts",
]
