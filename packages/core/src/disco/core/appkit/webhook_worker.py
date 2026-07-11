"""Disco-owned generic webhook data-plane emitters (WO-F3.3).

Only public endpoint/event metadata is embedded. Signing keys and outbound
targets are host-managed runtime inputs and never enter the generated tree.
"""

from __future__ import annotations

import json

from .spec import AppSpec, WebhookEndpointMeta


def _meta(app: AppSpec) -> tuple[str, tuple[WebhookEndpointMeta, ...]]:
    if app.webhooks is None:
        raise ValueError("webhook emission requires AppSpec.webhooks metadata")
    return app.webhooks.app_binding, app.webhooks.endpoints


def emit_webhook_schema_sql() -> str:
    return (
        'CREATE TABLE IF NOT EXISTS "webhook_events" (\n'
        '  "endpoint_id" TEXT NOT NULL,\n'
        '  "event_id" TEXT NOT NULL,\n'
        '  "event_type" TEXT NOT NULL,\n'
        "  \"received_at\" TEXT NOT NULL DEFAULT(datetime('now')),\n"
        '  PRIMARY KEY("endpoint_id", "event_id")\n'
        ");\n\n"
        'CREATE TABLE IF NOT EXISTS "webhook_effects" (\n'
        '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  "endpoint_id" TEXT NOT NULL,\n'
        '  "event_id" TEXT NOT NULL,\n'
        '  "event_type" TEXT NOT NULL,\n'
        '  "body_sha256" TEXT NOT NULL,\n'
        "  \"applied_at\" TEXT NOT NULL DEFAULT(datetime('now')),\n"
        '  UNIQUE("endpoint_id", "event_id")\n'
        ");"
    )


def emit_webhook_drizzle_ts() -> str:
    return (
        'export const webhookEvents = sqliteTable("webhook_events", {\n'
        '  endpoint_id: text("endpoint_id").notNull(),\n'
        '  event_id: text("event_id").notNull(),\n'
        '  event_type: text("event_type").notNull(),\n'
        "  received_at: text(\"received_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});\n\n"
        'export const webhookEffects = sqliteTable("webhook_effects", {\n'
        '  id: integer("id").primaryKey({ autoIncrement: true }),\n'
        '  endpoint_id: text("endpoint_id").notNull(),\n'
        '  event_id: text("event_id").notNull(),\n'
        '  event_type: text("event_type").notNull(),\n'
        '  body_sha256: text("body_sha256").notNull(),\n'
        "  applied_at: text(\"applied_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});"
    )


def emit_webhook_env_ts() -> str:
    return (
        "  // Host-injected webhook signing secret; never emitted into project files.\n"
        "  WEBHOOK_SIGNING_SECRET?: string;\n"
        "  WEBHOOK_RUNTIME_READY?: string;\n"
    )


def emit_webhook_worker_ts(app: AppSpec) -> str:
    app_binding, endpoints = _meta(app)
    inbound = {
        endpoint.endpoint_id: list(endpoint.event_types)
        for endpoint in endpoints
        if endpoint.direction == "inbound"
    }
    outbound = {
        endpoint.endpoint_id: list(endpoint.event_types)
        for endpoint in endpoints
        if endpoint.direction == "outbound"
    }
    return f"""const WEBHOOK_APP_BINDING = {json.dumps(app_binding)};
const WEBHOOK_INBOUND: Record<string, string[]> = {json.dumps(inbound, sort_keys=True)};
const WEBHOOK_OUTBOUND: Record<string, string[]> = {json.dumps(outbound, sort_keys=True)};
const WEBHOOK_SIGNATURE_TOLERANCE_SECONDS = 300;
const MAX_WEBHOOK_BODY_BYTES = 64 * 1024;
const WEBHOOK_EVENT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}$/;

interface WebhookEnvelope {{ id: string; type: string; data: unknown; }}

function webhookJson(data: unknown, status = 200): Response {{
  return json(data, status, {{ "Cache-Control": "no-store" }});
}}

async function readWebhookBody(request: Request): Promise<Uint8Array | null> {{
  if (request.body === null) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {{
    while (true) {{
      const {{ done, value }} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_WEBHOOK_BODY_BYTES) {{ await reader.cancel(); return null; }}
      chunks.push(value);
    }}
  }} catch {{
    try {{ await reader.cancel(); }} catch {{ /* never log request data */ }}
    return null;
  }}
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {{ body.set(chunk, offset); offset += chunk.byteLength; }}
  return body;
}}

function parseWebhookSignature(
  header: string | null,
): {{ timestamp: number; mac: Uint8Array }} | null {{
  if (header === null || header.length > 256) return null;
  const match = /^t=([1-9][0-9]{{0,11}}),v1=([0-9a-f]{{64}})$/.exec(header);
  if (match === null) return null;
  const timestamp = Number(match[1]);
  if (!Number.isSafeInteger(timestamp)) return null;
  const mac = new Uint8Array(32);
  for (let i = 0; i < mac.length; i += 1) {{
    mac[i] = Number.parseInt(match[2].slice(i * 2, i * 2 + 2), 16);
  }}
  return {{ timestamp, mac }};
}}

async function webhookSignatureValid(
  env: Env, endpointId: string, header: string | null, body: Uint8Array,
): Promise<boolean> {{
  const secret = env.WEBHOOK_SIGNING_SECRET;
  if (typeof secret !== "string" || secret.length < 32 || secret.length > 512) return false;
  const parsed = parseWebhookSignature(header);
  if (parsed === null) return false;
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - parsed.timestamp) > WEBHOOK_SIGNATURE_TOLERANCE_SECONDS) return false;
  const prefix = new TextEncoder().encode(`${{parsed.timestamp}}.${{endpointId}}.`);
  const signed = new Uint8Array(prefix.byteLength + body.byteLength);
  signed.set(prefix, 0); signed.set(body, prefix.byteLength);
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), {{ name: "HMAC", hash: "SHA-256" }}, false, ["verify"],
  );
  return crypto.subtle.verify("HMAC", key, parsed.mac, signed);
}}

function parseWebhookEnvelope(body: Uint8Array): WebhookEnvelope | null {{
  let raw: unknown;
  try {{
    raw = JSON.parse(new TextDecoder("utf-8", {{ fatal: true }}).decode(body));
  }} catch {{ return null; }}
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  if (Object.keys(value).sort().join(",") !== "data,id,type") return null;
  if (typeof value.id !== "string" || !WEBHOOK_EVENT_ID_RE.test(value.id)) return null;
  if (typeof value.type !== "string" || value.type.length > 64) return null;
  return {{ id: value.id, type: value.type, data: value.data }};
}}

async function webhookAlreadyRecorded(
  db: D1DatabaseSession, endpointId: string, eventId: string,
): Promise<boolean> {{
  const row = await db.prepare(
    "SELECT event_id FROM webhook_events WHERE endpoint_id = ? AND event_id = ? LIMIT 1",
  ).bind(endpointId, eventId).first();
  return row !== null;
}}

async function applyWebhookEvent(
  env: Env, endpointId: string, event: WebhookEnvelope, body: Uint8Array,
): Promise<Response> {{
  const allowed = WEBHOOK_INBOUND[endpointId];
  if (!allowed || !allowed.includes(event.type)) {{
    return webhookJson({{ error: "event type refused" }}, 400);
  }}
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", body));
  const bodySha256 = Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
  const db = env.DB.withSession("first-primary");
  try {{
    await db.batch([
      db.prepare("INSERT INTO webhook_events (endpoint_id, event_id, event_type) VALUES (?, ?, ?)")
        .bind(endpointId, event.id, event.type),
      db.prepare(
        `INSERT INTO webhook_effects (endpoint_id, event_id, event_type, body_sha256)
         VALUES (?, ?, ?, ?)`,
      ).bind(endpointId, event.id, event.type, bodySha256),
    ]);
    return webhookJson({{ ok: true }});
  }} catch {{
    if (await webhookAlreadyRecorded(db, endpointId, event.id)) return webhookJson({{ ok: true }});
    return webhookJson({{ error: "webhook effect failed" }}, 500);
  }}
}}

async function webhookInbound(request: Request, env: Env, endpointId: string): Promise<Response> {{
  if (env.WEBHOOK_RUNTIME_READY !== "1") {{
    return webhookJson({{ error: "delivery unavailable" }}, 503);
  }}
  if (!(endpointId in WEBHOOK_INBOUND)) return webhookJson({{ error: "not found" }}, 404);
  const body = await readWebhookBody(request);
  if (body === null) return webhookJson({{ error: "payload too large" }}, 413);
  if (!(await webhookSignatureValid(
    env, endpointId, request.headers.get("Disco-Webhook-Signature"), body,
  ))) {{
    console.warn("webhook rejected: signature invalid", {{ endpoint: endpointId }});
    return webhookJson({{ error: "invalid signature" }}, 401);
  }}
  const event = parseWebhookEnvelope(body);
  if (event === null) return webhookJson({{ error: "invalid event" }}, 400);
  return applyWebhookEvent(env, endpointId, event, body);
}}

async function webhookOutbound(
  request: Request, env: Env, endpointId: string, body: unknown,
): Promise<Response> {{
  if (env.WEBHOOK_RUNTIME_READY !== "1") {{
    return webhookJson({{ error: "delivery unavailable" }}, 503);
  }}
  const session = await resolveSession(request, env);
  if (session === null) return webhookJson({{ error: "unauthorized" }}, 401);
  if (typeof body !== "object" || body === null || Array.isArray(body)) {{
    return webhookJson({{ error: "invalid delivery" }}, 400);
  }}
  const value = body as Record<string, unknown>;
  if (Object.keys(value).sort().join(",") !== "data,event_type") {{
    return webhookJson({{ error: "invalid delivery" }}, 400);
  }}
  const allowed = WEBHOOK_OUTBOUND[endpointId];
  if (!allowed || typeof value.event_type !== "string" || !allowed.includes(value.event_type)) {{
    return webhookJson({{ error: "event type refused" }}, 400);
  }}
  try {{
    const result = await svc(env, "webhook.emit", {{
      app_binding: WEBHOOK_APP_BINDING,
      endpoint_id: endpointId,
      event_type: value.event_type,
      data: value.data,
    }});
    return webhookJson(result);
  }} catch {{
    return webhookJson({{ error: "delivery unavailable" }}, 502);
  }}
}}
"""


def emit_webhook_inbound_route_ts(app: AppSpec) -> str:
    _binding, endpoints = _meta(app)
    routes = []
    for endpoint in endpoints:
        if endpoint.direction == "inbound":
            path = json.dumps(f"/api/webhooks/{endpoint.endpoint_id}")
            endpoint_id = json.dumps(endpoint.endpoint_id)
            routes.append(
                f'    if (rawPath === {path} && request.method === "POST") {{\n'
                f"      return webhookInbound(request, env, {endpoint_id});\n"
                "    }\n"
            )
    return "".join(routes)


def emit_webhook_outbound_routes_ts(app: AppSpec) -> str:
    _binding, endpoints = _meta(app)
    routes = []
    for endpoint in endpoints:
        if endpoint.direction == "outbound":
            path = json.dumps(f"/api/webhooks/{endpoint.endpoint_id}/emit")
            endpoint_id = json.dumps(endpoint.endpoint_id)
            routes.append(
                f'    if (rawPath === {path} && request.method === "POST") {{\n'
                "      const contentTypeError = requireJsonContentType(request);\n"
                "      if (contentTypeError !== null) return contentTypeError;\n"
                "      const parsed = await readJsonBody(request);\n"
                "      if (!parsed.ok) return parsed.response;\n"
                f"      return webhookOutbound(request, env, {endpoint_id}, parsed.body);\n"
                "    }\n"
            )
    return "".join(routes)


__all__ = [
    "emit_webhook_drizzle_ts",
    "emit_webhook_env_ts",
    "emit_webhook_inbound_route_ts",
    "emit_webhook_outbound_routes_ts",
    "emit_webhook_schema_sql",
    "emit_webhook_worker_ts",
]
