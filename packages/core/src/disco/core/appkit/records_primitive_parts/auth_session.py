"""Session cookie/lookup TS emitter for the records-auth Worker.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations


def _emit_auth_session_ts(*, include_role_grants: bool = False) -> str:
    if include_role_grants:
        session_role_field = "  roles: string[];\n"
        role_check = (
            "  if (roles.length > 0 && !roles.some((role) => session.roles.includes(role))) {\n"
        )
    else:
        session_role_field = "  role: string;\n"
        role_check = "  if (roles.length > 0 && !roles.includes(session.role)) {\n"
    grant_lookup = (
        "  const grants = await env.DB.prepare(\n"
        '    "SELECT role FROM user_role_grants WHERE user_id = ? AND active = 1",\n'
        "  ).bind(row.userId).all<{ role: string }>();\n"
        "  const roles = [row.role, ...grants.results.map((grant) => grant.role)]\n"
        "    .filter((role, index, all) => ROLES.includes(role) && all.indexOf(role) === index);\n"
        "  if (roles.length === 0) return null;\n"
        "  return { tokenHash, userId: row.userId, roles };\n"
        if include_role_grants
        else (
            "  if (!ROLES.includes(row.role)) return null;\n"
            "  return { tokenHash, userId: row.userId, role: row.role };\n"
        )
    )
    return (
        "const SESSION_TTL_SECONDS = 7 * 24 * 60 * 60;\n\n"
        "interface UserSession {\n"
        "  tokenHash: string;\n"
        "  userId: number;\n" + session_role_field + "}\n\n"
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
        '    const eqAt = part.indexOf("=");\n'
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
        "  }\n" + grant_lookup + "}\n\n"
        "function authorizeSession("
        "session: UserSession | null, roles: string[]): Response | null {\n"
        '  if (session === null) return json({ error: "unauthorized" }, 401);\n'
        + role_check
        + '    return json({ error: "forbidden" }, 403);\n'
        "  }\n"
        "  return null;\n"
        "}\n"
    )
