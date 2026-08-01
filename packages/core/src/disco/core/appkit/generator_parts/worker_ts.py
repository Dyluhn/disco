"""`worker/index.ts` for the lead-gen primitive: public lead capture + an
auth-gated admin read-back.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget, and to bring `_emit_worker_ts`
under the 100-logical-line callable cap. See `generator_parts/__init__.py` for
the byte-identity contract this split must hold: every helper below is a
straight, in-order cut of the ORIGINAL single string concatenation — no
character added, removed, or reordered. Byte-identity is verified externally
(hash comparison against the pre-split generator), not by a test in this tree.
"""

from __future__ import annotations

from ..spec import Entity
from .ids import _ts


def _worker_ts_prelude(required_lit: str, cols_lit: str, email_lit: str) -> str:
    return (
        "/* Auto-generated Cloudflare Worker (Epic E): public lead capture + "
        "AUTH-GATED admin read-back.\n"
        "   READS (GET /api/leads, GET /admin) require `Authorization: Bearer "
        "<env.ADMIN_TOKEN>`;\n"
        "   a missing ADMIN_TOKEN FAILS CLOSED (reads denied). POST /api/leads "
        "(submission) stays public. */\n"
        'import { desc } from "drizzle-orm";\n'
        'import { drizzle } from "drizzle-orm/d1";\n'
        'import { leads } from "../src/db/schema";\n'
        "\n"
        "export interface Env {\n"
        "  DB: D1Database;\n"
        "  ASSETS: { fetch: (req: Request) => Promise<Response> };\n"
        "  // Admin read-back secret. Set with `wrangler secret put ADMIN_TOKEN`.\n"
        "  // Optional in the type so a MISSING token fails closed (denies reads)\n"
        "  // rather than throwing — never give this a hardcoded default.\n"
        "  ADMIN_TOKEN?: string;\n"
        "}\n\n"
        f"const REQUIRED: string[] = {required_lit};\n"
        f"const COLUMNS: string[] = {cols_lit};\n"
        f"const EMAIL_FIELDS: string[] = {email_lit};\n"
        "const MAX_FIELD_LEN = 2000;\n"
        "const EMAIL_RE = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/;\n\n"
        "type LeadInsert = typeof leads.$inferInsert;\n\n"
        "function json(data: unknown, status = 200): Response {\n"
        "  return new Response(JSON.stringify(data), {\n"
        "    status,\n"
        '    headers: { "Content-Type": "application/json" },\n'
        "  });\n"
        "}\n\n"
        "// Escape EVERY dynamic value before it goes into HTML (stored-XSS defense).\n"
        "function escapeHtml(value: unknown): string {\n"
        "  return String(value)\n"
        '    .replace(/&/g, "&amp;")\n'
        '    .replace(/</g, "&lt;")\n'
        '    .replace(/>/g, "&gt;")\n'
        '    .replace(/"/g, "&quot;")\n'
        '    .replace(/\'/g, "&#39;");\n'
        "}\n\n"
    )


def _worker_ts_validation() -> str:
    return (
        "// Constant-token check; a missing ADMIN_TOKEN denies all reads (fail closed).\n"
        "function isAuthorized(request: Request, env: Env): boolean {\n"
        "  const expected = env.ADMIN_TOKEN;\n"
        "  if (!expected) return false;\n"
        '  const header = request.headers.get("Authorization") ?? "";\n'
        '  const prefix = "Bearer ";\n'
        "  if (!header.startsWith(prefix)) return false;\n"
        "  return header.slice(prefix.length) === expected;\n"
        "}\n\n"
        "type LeadCheck =\n"
        "  | { ok: true; rec: Record<string, unknown> }\n"
        "  | { ok: false; error: string };\n\n"
        "// Validate the submission: a JSON OBJECT, known keys only, required present,\n"
        "// every value a bounded string, email-shaped where expected.\n"
        "function validateLead(body: unknown): LeadCheck {\n"
        '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
        '    return { ok: false, error: "request body must be a JSON object" };\n'
        "  }\n"
        "  const rec = body as Record<string, unknown>;\n"
        "  for (const key of Object.keys(rec)) {\n"
        "    if (!COLUMNS.includes(key)) {\n"
        "      return { ok: false, error: `unknown field: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of REQUIRED) {\n"
        "    const v = rec[key];\n"
        '    if (v === undefined || v === null || v === "") {\n'
        "      return { ok: false, error: `missing required field: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of COLUMNS) {\n"
        "    const v = rec[key];\n"
        "    if (v === undefined || v === null) continue;\n"
        '    if (typeof v !== "string") {\n'
        "      return { ok: false, error: `field must be a string: ${key}` };\n"
        "    }\n"
        "    if (v.length > MAX_FIELD_LEN) {\n"
        "      return { ok: false, error: `field too long: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  for (const key of EMAIL_FIELDS) {\n"
        "    const v = rec[key];\n"
        '    if (typeof v === "string" && v !== "" && !EMAIL_RE.test(v)) {\n'
        "      return { ok: false, error: `invalid email: ${key}` };\n"
        "    }\n"
        "  }\n"
        "  return { ok: true, rec };\n"
        "}\n\n"
    )


def _worker_ts_data_ops(lead_values: str) -> str:
    return (
        "function leadValues(rec: Record<string, unknown>): LeadInsert {\n"
        "  return {\n"
        f"{lead_values}\n"
        "  } as LeadInsert;\n"
        "}\n\n"
        "async function insertLead(env: Env, body: unknown): Promise<Response> {\n"
        "  const check = validateLead(body);\n"
        "  if (!check.ok) {\n"
        "    return json({ error: check.error }, 400);\n"
        "  }\n"
        "  const rec = check.rec;\n"
        "  try {\n"
        "    const db = drizzle(env.DB);\n"
        "    await db.insert(leads).values(leadValues(rec)).run();\n"
        "  } catch {\n"
        '    return json({ error: "could not save lead" }, 500);\n'
        "  }\n"
        "  return json({ ok: true }, 201);\n"
        "}\n\n"
        "async function listLeads(env: Env): Promise<Record<string, unknown>[]> {\n"
        "  const db = drizzle(env.DB);\n"
        "  const rows = await db.select().from(leads).orderBy(desc(leads.id)).limit(200).all();\n"
        "  return rows as Record<string, unknown>[];\n"
        "}\n\n"
    )


def _worker_ts_admin_ui() -> str:
    return (
        "// Server-rendered leads table — EVERY cell escaped via escapeHtml (no raw\n"
        "// interpolation of lead values into HTML).\n"
        "function adminTable(rows: Record<string, unknown>[]): Response {\n"
        '  const head = COLUMNS.map((c) => `<th>${escapeHtml(c)}</th>`).join("");\n'
        "  const body = rows\n"
        "    .map(\n"
        "      (r) =>\n"
        "        `<tr>${COLUMNS.map((c) => "
        '`<td>${escapeHtml(r[c] ?? "")}</td>`).join("")}</tr>`\n'
        "    )\n"
        '    .join("");\n'
        "  const html =\n"
        '    "<!doctype html><html><head><meta charset=\\"utf-8\\"><title>Leads</title>" +\n'
        '    "</head><body><h1>Leads</h1><table border=\\"1\\" cellpadding=\\"6\\">" +\n'
        "    `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></body></html>`;\n"
        '  return new Response(html, { headers: { "Content-Type": "text/html" } });\n'
        "}\n\n"
        "// 401 sign-in shell: NO lead data, just a token prompt that re-requests\n"
        "// /admin with the Bearer header (token never embedded in the HTML).\n"
        "function adminLoginPage(): Response {\n"
        "  const html =\n"
        '    "<!doctype html><html><head><meta charset=\\"utf-8\\">" +\n'
        '    "<title>Admin sign-in</title></head><body><h1>Admin sign-in</h1>" +\n'
        '    "<p>Enter the admin token to view leads.</p>" +\n'
        '    "<form id=\\"login\\"><input id=\\"token\\" type=\\"password\\" " +\n'
        '    "placeholder=\\"Admin token\\" autocomplete=\\"off\\" />" +\n'
        '    "<button type=\\"submit\\">View leads</button></form><script>" +\n'
        '    "var f=document.getElementById(\\"login\\");" +\n'
        '    "f.addEventListener(\\"submit\\",async function(e){" +\n'
        '    "e.preventDefault();" +\n'
        '    "var t=document.getElementById(\\"token\\").value;" +\n'
        '    "var r=await fetch(\\"/admin\\",{headers:{Authorization:\\"Bearer \\"+t}});" +\n'
        '    "var h=await r.text();" +\n'
        '    "document.open();document.write(h);document.close();" +\n'
        '    "});</script></body></html>";\n'
        '  return new Response(html, { status: 401, headers: { "Content-Type": "text/html" } });\n'
        "}\n\n"
    )


def _worker_ts_fetch_handler() -> str:
    return (
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    const url = new URL(request.url);\n"
        '    if (url.pathname === "/api/leads" && request.method === "POST") {\n'
        "      let body: unknown;\n"
        "      try {\n"
        "        body = await request.json();\n"
        "      } catch {\n"
        '        return json({ error: "invalid JSON" }, 400);\n'
        "      }\n"
        "      return insertLead(env, body);\n"
        "    }\n"
        '    if (url.pathname === "/api/leads" && request.method === "GET") {\n'
        "      if (!isAuthorized(request, env)) {\n"
        '        return json({ error: "unauthorized" }, 401);\n'
        "      }\n"
        "      const rows = await listLeads(env);\n"
        "      return json({ leads: rows });\n"
        "    }\n"
        '    if (url.pathname === "/admin" && request.method === "GET") {\n'
        "      if (!isAuthorized(request, env)) {\n"
        "        return adminLoginPage();\n"
        "      }\n"
        "      const rows = await listLeads(env);\n"
        "      return adminTable(rows);\n"
        "    }\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_worker_ts(lead: Entity) -> str:
    cols = [f.name for f in lead.fields]
    required = [f.name for f in lead.fields if f.required]
    email_fields = [
        f.name
        for f in lead.fields
        if f.name.lower() == "email" or f.type.strip().lower() == "email"
    ]
    required_lit = _ts(required)
    cols_lit = _ts(cols)
    email_lit = _ts(email_fields)
    lead_values = "\n".join(f"    {_ts(c)}: rec[{_ts(c)}] ?? null," for c in cols)
    return (
        _worker_ts_prelude(required_lit, cols_lit, email_lit)
        + _worker_ts_validation()
        + _worker_ts_data_ops(lead_values)
        + _worker_ts_admin_ui()
        + _worker_ts_fetch_handler()
    )
