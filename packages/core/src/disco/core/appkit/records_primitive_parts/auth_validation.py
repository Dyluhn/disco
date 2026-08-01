"""Register/login validation TS emitter for the records-auth Worker.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations


def _emit_auth_validation_ts(*, role_collection: str = "ROLES") -> str:
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
        f'  if (typeof role !== "string" || !{role_collection}.includes(role)) {{\n'
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
