"""Feature flags primitive: lowering of the folded flag list into schema/Worker/CSS.

Extracted from ``feature_flags_primitive`` to keep that module's public facade
under the module logical-line budget; the facade re-imports these names
unchanged so every caller keeps importing from
``disco.core.appkit.feature_flags_primitive``.
"""

from __future__ import annotations

from ..feature_flags_primitive import (
    _FLAGS_GET_ROUTE,
    _FLAGS_TABLE,
    _FLAGS_TOGGLE_ROUTE,
    FeatureFlag,
)


def _sql_str(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def lower_feature_flags_schema_sql(base: str, flags: tuple[FeatureFlag, ...]) -> str:
    """Append the `_flags` table and deterministic seed rows. Empty flags leave the
    base schema byte-identical."""
    if not flags:
        return base
    values = ",\n".join(
        f"  ({_sql_str(flag.key)}, {_sql_str(flag.description)}, {1 if flag.enabled else 0})"
        for flag in flags
    )
    return (
        base + "\n" + "-- Feature flags primitive (Epic 6.2): D1-backed runtime toggles.\n"
        f'CREATE TABLE IF NOT EXISTS "{_FLAGS_TABLE}" (\n'
        '  "key" TEXT PRIMARY KEY,\n'
        '  "description" TEXT,\n'
        '  "enabled" INTEGER NOT NULL DEFAULT 0 CHECK ("enabled" IN (0, 1)),\n'
        "  \"updated_at\" TEXT NOT NULL DEFAULT (datetime('now'))\n"
        ");\n"
        f'INSERT INTO "{_FLAGS_TABLE}" ("key", "description", "enabled") VALUES\n'
        f"{values}\n"
        'ON CONFLICT("key") DO UPDATE SET\n'
        '  "description" = excluded."description",\n'
        '  "enabled" = excluded."enabled",\n'
        "  \"updated_at\" = datetime('now');\n"
    )


def _ts(value: object) -> str:
    from ..generator import _ts as generator_ts

    return generator_ts(value)


def lower_feature_flags_drizzle_ts(base: str, flags: tuple[FeatureFlag, ...]) -> str:
    """Append a typed Drizzle table for `_flags`. Empty flags leave the base schema
    byte-identical."""
    if not flags:
        return base
    return (
        base + "\n" + "/* Feature flags primitive (Epic 6.2): D1-backed runtime toggles. */\n"
        f"export const featureFlags = sqliteTable({_ts(_FLAGS_TABLE)}, {{\n"
        '  key: text("key").primaryKey(),\n'
        '  description: text("description"),\n'
        '  enabled: integer("enabled").notNull().default(0),\n'
        "  updated_at: text(\"updated_at\").notNull().default(sql`(datetime('now'))`),\n"
        "});\n"
    )


def _replace_once(source: str, anchor: str, replacement: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise ValueError(
            f"generator invariant broken: expected exactly one occurrence of "
            f"{anchor!r}, found {count}"
        )
    return source.replace(anchor, replacement)


def _feature_flags_worker_defs(flags: tuple[FeatureFlag, ...]) -> str:
    keys = [flag.key for flag in flags]
    select_enabled_sql = 'SELECT "key" FROM "_flags" WHERE "enabled" = 1 ORDER BY "key"'
    update_flag_sql = (
        'UPDATE "_flags" SET "enabled" = ?, "updated_at" = datetime(\'now\') WHERE "key" = ?'
    )
    return (
        "// ---- Feature flags primitive (Epic 6.2): D1-backed flags ----------------\n"
        f"const FEATURE_FLAG_KEYS: string[] = {_ts(keys)};\n\n"
        "type FeatureFlagToggleCheck =\n"
        "  | { ok: true; key: string; enabled: boolean }\n"
        "  | { ok: false; error: string };\n\n"
        "async function listEnabledFeatureFlags(env: Env): Promise<Response> {\n"
        "  const rows = await env.DB.prepare(\n"
        f"    {_ts(select_enabled_sql)}\n"
        "  ).all<{ key: string }>();\n"
        "  const enabled: Record<string, true> = {};\n"
        "  for (const row of rows.results ?? []) {\n"
        '    if (typeof row.key === "string" && FEATURE_FLAG_KEYS.includes(row.key)) {\n'
        "      enabled[row.key] = true;\n"
        "    }\n"
        "  }\n"
        "  return json({ flags: enabled });\n"
        "}\n\n"
        "function validateFeatureFlagToggle(body: unknown): FeatureFlagToggleCheck {\n"
        '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
        '    return { ok: false, error: "request body must be a JSON object" };\n'
        "  }\n"
        "  const rec = body as Record<string, unknown>;\n"
        '  if (typeof rec.key !== "string" || !FEATURE_FLAG_KEYS.includes(rec.key)) {\n'
        '    return { ok: false, error: "unknown feature flag key" };\n'
        "  }\n"
        '  if (typeof rec.enabled !== "boolean") {\n'
        '    return { ok: false, error: "enabled must be a boolean" };\n'
        "  }\n"
        "  return { ok: true, key: rec.key, enabled: rec.enabled };\n"
        "}\n\n"
        "async function toggleFeatureFlag(\n"
        "  env: Env,\n"
        "  key: string,\n"
        "  enabled: boolean\n"
        "): Promise<Response> {\n"
        "  try {\n"
        "    await env.DB.prepare(\n"
        f"      {_ts(update_flag_sql)}\n"
        "    ).bind(enabled ? 1 : 0, key).run();\n"
        "  } catch {\n"
        '    return json({ error: "could not update feature flag" }, 500);\n'
        "  }\n"
        "  return json({ ok: true, flags: { [key]: enabled } });\n"
        "}\n\n"
    )


_FEATURE_FLAGS_DISPATCH_TS = (
    "    // Feature flags primitive (Epic 6.2): public read, owner-gated toggle.\n"
    f'    if (url.pathname === "{_FLAGS_GET_ROUTE}" && request.method === "GET") {{\n'
    "      return listEnabledFeatureFlags(env);\n"
    "    }\n"
    f'    if (url.pathname === "{_FLAGS_TOGGLE_ROUTE}" && request.method === "POST") {{\n'
    "      if (!isAuthorized(request, env)) {\n"
    '        return json({ error: "unauthorized" }, 401);\n'
    "      }\n"
    "      let body: unknown;\n"
    "      try {\n"
    "        body = await request.json();\n"
    "      } catch {\n"
    '        return json({ error: "invalid JSON" }, 400);\n'
    "      }\n"
    "      const check = validateFeatureFlagToggle(body);\n"
    "      if (!check.ok) return json({ error: check.error }, 422);\n"
    "      return toggleFeatureFlag(env, check.key, check.enabled);\n"
    "    }\n"
)


def lower_feature_flags_worker_ts(source: str, flags: tuple[FeatureFlag, ...]) -> str:
    """Splice the flags route plane into an already-emitted lead-gen worker. Empty
    flags leave the worker byte-identical."""
    if not flags:
        return source
    out = _replace_once(
        source,
        "export default {\n",
        _feature_flags_worker_defs(flags) + "export default {\n",
    )
    return _replace_once(
        out,
        "    return env.ASSETS.fetch(request);\n",
        _FEATURE_FLAGS_DISPATCH_TS + "    return env.ASSETS.fetch(request);\n",
    )


def lower_feature_flags_styles_css(base: str, flags: tuple[FeatureFlag, ...]) -> str:
    if not flags:
        return base
    return (
        base
        + ".flag-admin-panel { display: grid; gap: var(--space); max-width: 46rem; }\n"
        + ".flag-token { display: grid; gap: 0.35rem; max-width: 24rem; font-weight: 600; }\n"
        + ".flag-token input {\n"
        + "  font: inherit; padding: 0.6rem; border-radius: var(--radius);\n"
        + "  border: 1px solid color-mix(in srgb, var(--color-text) 30%, transparent);\n"
        + "  background: var(--color-surface); color: var(--color-text);\n"
        + "}\n"
        + ".flag-list { list-style: none; margin: 0; padding: 0; display: grid;"
        + " gap: 0.6rem; }\n"
        + ".flag-row {\n"
        + "  display: flex; align-items: center; justify-content: space-between;"
        + " gap: var(--space);\n"
        + "  border: 1px solid color-mix(in srgb, var(--color-text) 14%, transparent);\n"
        + "  border-radius: var(--radius); padding: 0.75rem;\n"
        + "}\n"
        + ".flag-meta { display: grid; gap: 0.15rem; min-width: 0; }\n"
        + ".flag-key { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }\n"
        + ".flag-description { color: color-mix(in srgb, var(--color-text) 70%, transparent); }\n"
        + ".flag-toggle { display: flex; align-items: center; gap: 0.5rem; white-space: nowrap; }\n"
        + ".flag-toggle input { inline-size: 1.2rem; block-size: 1.2rem; }\n"
    )
