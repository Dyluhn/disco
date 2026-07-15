"""The AppKit `feature_flags` primitive (Epic 6.2): D1-backed flags for lead-gen apps.

This is an ADD-ON primitive, not a base scaffold. `app_add_primitive` validates a
`FeatureFlagsSpec`, `apply_feature_flags_spec` folds it into the AppSpec as a
stable admin section marker, and the lead-gen generator lowers that marker into:

* a `_flags` D1 table seeded from the declared keys;
* public `GET /api/_flags`, returning only enabled flags;
* owner-gated `POST /api/_flags/toggle`, reusing the lead-gen `ADMIN_TOKEN`
  bearer-token convention and fail-closed `isAuthorized` helper;
* a small React admin section plus a bundled `useFlag(key)` / `flags.isEnabled(key)`
  helper for app code.

Scope: v1 folds only into lead_gen-shaped apps. Directory is static and records owns
its own data/auth surface, so both are refused with guidance rather than silently
emitting a partial flag plane.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .primitives import (
    FEATURE_FLAGS_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
    resolve_primitive,
)
from .spec import AppSpec, DesignSpec, Section, SectionContent

_KEY_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_MAX_FLAGS = 20
_MAX_KEY = 48
_MAX_DESCRIPTION = 160
_FLAGS_SECTION_ID = "feature_flags"
_FLAGS_CONTENT_REF = "feature_flags"
_FLAGS_TABLE = "_flags"
_FLAGS_GET_ROUTE = "/api/_flags"
_FLAGS_TOGGLE_ROUTE = "/api/_flags/toggle"
_RESERVED_KEYS = frozenset(
    {
        "constructor",
        "prototype",
        "hasownproperty",
        "isprototypeof",
        "propertyisenumerable",
        "tostring",
        "tolocalestring",
        "valueof",
        "proto",
    }
)


class FeatureFlag(BaseModel):
    """One declared feature flag. The key is a bounded lowercase slug that is safe
    as JSON object data, a D1 row value, and a generated TS string literal."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=_MAX_KEY,
        description=(
            "Lowercase slug key (starts with a letter; letters, digits and hyphens only)."
        ),
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_DESCRIPTION,
        description="Optional owner-facing description for the flag.",
    )
    enabled: bool = Field(description="Whether the flag is initially enabled.")

    @field_validator("key")
    @classmethod
    def _key_is_slug(cls, value: str) -> str:
        if not _KEY_RE.match(value):
            raise ValueError(
                "feature flag key must be a lowercase slug "
                f"(pattern {_KEY_RE.pattern!r}), got {value!r}"
            )
        if value in _RESERVED_KEYS:
            raise ValueError(
                f"feature flag key {value!r} is reserved for JS object safety; choose another key"
            )
        return value

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("feature flag description must be non-empty when given")
        return value


class FeatureFlagsSpec(BaseModel):
    """The `feature_flags` primitive's declarative spec. Unknown keys are refused so
    model recovery gets a precise schema error instead of a silent drop."""

    model_config = ConfigDict(extra="forbid")

    flags: list[FeatureFlag] = Field(
        min_length=1,
        max_length=_MAX_FLAGS,
        description=f"1..{_MAX_FLAGS} feature flags; keys must be unique.",
    )

    @model_validator(mode="after")
    def _keys_unique(self) -> FeatureFlagsSpec:
        seen: set[str] = set()
        for flag in self.flags:
            if flag.key in seen:
                raise ValueError(f"duplicate feature flag key: {flag.key!r}")
            seen.add(flag.key)
        return self


def _flag_item(flag: FeatureFlag) -> str:
    return json.dumps(
        {
            "description": flag.description,
            "enabled": flag.enabled,
            "key": flag.key,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def is_feature_flags_admin_section(section: Section) -> bool:
    """The folded-section marker the generator lowers to the admin component."""
    return section.id == _FLAGS_SECTION_ID and section.content_ref == _FLAGS_CONTENT_REF


def feature_flags_for(app: AppSpec) -> tuple[FeatureFlag, ...]:
    """Read the folded flags back from the AppSpec marker section. Empty tuple means
    the primitive is not applied. Malformed marker content fails hard because the
    generated D1/Worker plane would otherwise drift from the persisted spec."""
    matches = [
        section
        for page in app.pages
        for section in page.sections
        if is_feature_flags_admin_section(section)
    ]
    if not matches:
        return ()
    if len(matches) > 1:
        raise ValueError("feature_flags primitive marker appears more than once")
    content = matches[0].content
    if content is None:
        raise ValueError("feature_flags primitive marker has no content")
    flags: list[FeatureFlag] = []
    for raw in content.items:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("feature_flags marker contains invalid JSON") from exc
        flags.append(FeatureFlag.model_validate(payload))
    FeatureFlagsSpec(flags=flags)
    return tuple(flags)


def apply_feature_flags_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated FeatureFlagsSpec into the AppSpec as one stable admin
    section. Re-applying updates that section in place, so app_add_primitive's
    no-op check naturally catches identical specs."""
    if not isinstance(spec, FeatureFlagsSpec):
        raise TypeError(f"apply_spec for {FEATURE_FLAGS_PRIMITIVE_ID!r} needs a FeatureFlagsSpec")

    base = resolve_primitive(app.app_kind)
    if base.id != LEAD_GEN_PRIMITIVE_ID:
        raise ValueError(
            "the feature_flags primitive currently folds only into lead_gen-shaped "
            f"apps; this app's app_kind {app.app_kind!r} lowers through {base.id!r}. "
            "Use a lead_gen app for v1 flags."
        )
    if not app.pages:
        raise ValueError("the app has no pages to place the feature flags admin section on")

    section = Section(
        id=_FLAGS_SECTION_ID,
        kind="custom",
        content_ref=_FLAGS_CONTENT_REF,
        content=SectionContent(
            heading="Feature flags",
            items=tuple(_flag_item(flag) for flag in spec.flags),
        ),
    )
    section_json = section.model_dump(mode="json")
    data = app.model_dump(mode="json")
    found: list[tuple[int, int, dict[str, object]]] = []
    for page_index, page in enumerate(data["pages"]):
        for section_index, current in enumerate(page["sections"]):
            if current["id"] == _FLAGS_SECTION_ID:
                found.append((page_index, section_index, current))

    if len(found) > 1:
        raise ValueError(
            f"section id {_FLAGS_SECTION_ID!r} appears more than once; cannot fold flags"
        )
    if found:
        page_index, section_index, current = found[0]
        if current.get("kind") != "custom" or current.get("content_ref") != _FLAGS_CONTENT_REF:
            raise ValueError(
                f"section id {_FLAGS_SECTION_ID!r} is already taken by a non-flags "
                "section; choose or move that section before adding feature_flags"
            )
        data["pages"][page_index]["sections"][section_index] = {
            **current,
            "content": section_json["content"],
        }
    else:
        sections = list(data["pages"][0]["sections"])
        insert_at = len(sections)
        if sections and sections[-1].get("kind") == "footer":
            insert_at -= 1
        sections.insert(insert_at, section_json)
        data["pages"][0]["sections"] = sections

    return AppSpec.model_validate(data)


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


def _ts(value: object) -> str:
    from .generator import _ts as generator_ts

    return generator_ts(value)


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


def emit_feature_flags_hook_ts(flags: tuple[FeatureFlag, ...]) -> str:
    keys = [flag.key for flag in flags]
    return (
        'import { useEffect, useState } from "react";\n\n'
        f"const KNOWN_FLAG_KEYS: string[] = {_ts(keys)};\n"
        f"const FLAGS_PATH = {_ts(_FLAGS_GET_ROUTE)};\n\n"
        "export type FlagMap = Record<string, boolean>;\n\n"
        "let cachedFlags: FlagMap = {};\n"
        "let inFlight: Promise<FlagMap> | null = null;\n\n"
        "function normalizeFlags(body: unknown): FlagMap {\n"
        "  const next: FlagMap = {};\n"
        '  if (typeof body !== "object" || body === null || !("flags" in body)) {\n'
        "    return next;\n"
        "  }\n"
        "  const raw = (body as { flags: unknown }).flags;\n"
        '  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {\n'
        "    return next;\n"
        "  }\n"
        "  const values = raw as Record<string, unknown>;\n"
        "  for (const key of KNOWN_FLAG_KEYS) {\n"
        "    if (values[key] === true) next[key] = true;\n"
        "  }\n"
        "  return next;\n"
        "}\n\n"
        "async function requestFlags(force: boolean): Promise<FlagMap> {\n"
        "  if (force) inFlight = null;\n"
        "  if (inFlight) return inFlight;\n"
        "  inFlight = fetch(FLAGS_PATH)\n"
        "    .then(async (response) => {\n"
        "      if (!response.ok) return {};\n"
        "      return normalizeFlags(await response.json());\n"
        "    })\n"
        "    .then((next) => {\n"
        "      cachedFlags = next;\n"
        "      return next;\n"
        "    })\n"
        "    .catch(() => ({}))\n"
        "    .finally(() => {\n"
        "      inFlight = null;\n"
        "    });\n"
        "  return inFlight;\n"
        "}\n\n"
        "export const flags = {\n"
        "  knownKeys: KNOWN_FLAG_KEYS,\n"
        "  isEnabled(key: string): boolean {\n"
        "    return cachedFlags[key] === true;\n"
        "  },\n"
        "  ready(): Promise<FlagMap> {\n"
        "    return requestFlags(false);\n"
        "  },\n"
        "  refresh(): Promise<FlagMap> {\n"
        "    return requestFlags(true);\n"
        "  },\n"
        "};\n\n"
        "export function useFlag(key: string): boolean {\n"
        "  const [enabled, setEnabled] = useState(() => flags.isEnabled(key));\n"
        "  useEffect(() => {\n"
        "    let alive = true;\n"
        "    void flags.ready().then((map) => {\n"
        "      if (alive) setEnabled(map[key] === true);\n"
        "    });\n"
        "    return () => {\n"
        "      alive = false;\n"
        "    };\n"
        "  }, [key]);\n"
        "  return enabled;\n"
        "}\n"
    )


def emit_feature_flags_admin_component(
    comp: str, section: Section, flags: tuple[FeatureFlag, ...]
) -> str:
    """The tiny owner-facing admin section. The section is visible in the SPA, but
    the mutation route is still owner-gated by the Worker before it reads the body."""
    from .generator import _disco_field_attr, _disco_section_attrs

    defs = [
        {"key": flag.key, "description": flag.description, "enabled": flag.enabled}
        for flag in flags
    ]
    disco_attrs = _disco_section_attrs(section)
    return (
        "/* Auto-generated feature-flags admin section (feature_flags primitive) - "
        "do NOT hand-edit. */\n"
        'import { useEffect, useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        'import { flags as flagStore } from "../hooks/useFlag";\n\n'
        "interface FlagDef {\n"
        "  key: string;\n"
        "  description: string | null;\n"
        "  enabled: boolean;\n"
        "}\n\n"
        f"const FLAG_DEFS: FlagDef[] = {_ts(defs)};\n"
        f"const TOGGLE_PATH = {_ts(_FLAGS_TOGGLE_ROUTE)};\n\n"
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp)}] ?? {{}};\n"
        "  const [enabled, setEnabled] = useState<Record<string, boolean>>({});\n"
        '  const [token, setToken] = useState("");\n'
        '  const [message, setMessage] = useState("");\n\n'
        "  async function refreshFlags() {\n"
        "    const next = await flagStore.refresh();\n"
        "    setEnabled(next);\n"
        "  }\n\n"
        "  useEffect(() => {\n"
        "    void refreshFlags();\n"
        "  }, []);\n\n"
        "  async function toggleFlag(key: string, nextEnabled: boolean) {\n"
        "    if (!token.trim()) {\n"
        '      setMessage("Enter the admin token to change flags.");\n'
        "      return;\n"
        "    }\n"
        "    const response = await fetch(TOGGLE_PATH, {\n"
        '      method: "POST",\n'
        "      headers: {\n"
        '        "Content-Type": "application/json",\n'
        "        Authorization: `Bearer ${token.trim()}`,\n"
        "      },\n"
        "      body: JSON.stringify({ key, enabled: nextEnabled }),\n"
        "    });\n"
        "    if (!response.ok) {\n"
        "      setMessage(\n"
        '        response.status === 401 ? "Token rejected." : "Could not update flag."\n'
        "      );\n"
        "      return;\n"
        "    }\n"
        "    setEnabled((current) => ({ ...current, [key]: nextEnabled }));\n"
        "    await refreshFlags();\n"
        '    setMessage("Saved.");\n'
        "  }\n\n"
        "  return (\n"
        f'    <section className="section kind-custom feature-flags-admin" id={_ts(section.id)}'
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        '      <div className="app-main flag-admin-panel">\n'
        f"        {{c.heading ? <h2{_disco_field_attr('heading')}>{{c.heading}}</h2> : null}}\n"
        '        <label className="flag-token">\n'
        "          <span>Admin token</span>\n"
        '          <input type="password" value={token} autoComplete="off"\n'
        "            onChange={(event) => setToken(event.target.value)} />\n"
        "        </label>\n"
        '        <ul className="flag-list">\n'
        "          {FLAG_DEFS.map((flag) => {\n"
        "            const isEnabled = enabled[flag.key] === true;\n"
        "            return (\n"
        '              <li className="flag-row" key={flag.key}>\n'
        '                <span className="flag-meta">\n'
        '                  <strong className="flag-key">{flag.key}</strong>\n'
        "                  {flag.description ? (\n"
        '                    <span className="flag-description">{flag.description}</span>\n'
        "                  ) : null}\n"
        "                </span>\n"
        '                <label className="flag-toggle">\n'
        '                  <span>{isEnabled ? "Enabled" : "Disabled"}</span>\n'
        '                  <input type="checkbox" checked={isEnabled}\n'
        "                    onChange={(event) => toggleFlag(flag.key, event.target.checked)} />\n"
        "                </label>\n"
        "              </li>\n"
        "            );\n"
        "          })}\n"
        "        </ul>\n"
        '        {message ? <p className="form-status" aria-live="polite">{message}</p> : null}\n'
        "      </div>\n"
        "    </section>\n"
        "  );\n"
        "}\n"
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


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    n_fail = sum(1 for c in checks if not c.passed)
    return PrimitiveVerifyResult(
        ok=n_fail == 0,
        detail=f"{len(checks) - n_fail} passed / {n_fail} failed",
        checks=tuple(checks),
    )


def _verify_seeded_table(
    app: AppSpec | None, tree: Mapping[str, str]
) -> tuple[VerifyCheck, tuple[FeatureFlag, ...]]:
    if app is None:
        return (
            VerifyCheck(
                "feature_flags_seeded",
                False,
                "no .disco/appspec.json - run app_create and app_add_primitive first.",
            ),
            (),
        )
    try:
        flags = feature_flags_for(app)
    except ValueError as exc:
        return VerifyCheck("feature_flags_seeded", False, str(exc)), ()
    if not flags:
        return (
            VerifyCheck(
                "feature_flags_seeded",
                False,
                "the AppSpec has no folded feature_flags admin marker.",
            ),
            (),
        )
    schema_sql = tree.get("schema.sql")
    if schema_sql is None:
        return VerifyCheck("feature_flags_seeded", False, "missing schema.sql."), flags
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_sql)
        rows = conn.execute(
            f'SELECT "key", "description", "enabled" FROM "{_FLAGS_TABLE}" ORDER BY "key"'
        ).fetchall()
    except sqlite3.Error as exc:
        return (
            VerifyCheck(
                "feature_flags_seeded",
                False,
                f"schema.sql did not create and seed {_FLAGS_TABLE}: {exc}",
            ),
            flags,
        )
    finally:
        conn.close()
    expected = sorted((f.key, f.description, 1 if f.enabled else 0) for f in flags)
    ok = rows == expected
    evidence = (
        f"{_FLAGS_TABLE} contains the spec keys ({', '.join(f.key for f in flags)})."
        if ok
        else (f"{_FLAGS_TABLE} rows do not match the spec (rows={rows!r}, expected={expected!r}).")
    )
    return (
        VerifyCheck(
            "feature_flags_seeded",
            ok,
            evidence,
        ),
        flags,
    )


def _verify_public_route(worker_ts: str | None) -> VerifyCheck:
    if worker_ts is None:
        return VerifyCheck("feature_flags_public_route", False, "missing worker/index.ts.")
    enabled_filter = 'WHERE "enabled" = 1' in worker_ts or 'WHERE \\"enabled\\" = 1' in worker_ts
    ok = (
        f'url.pathname === "{_FLAGS_GET_ROUTE}" && request.method === "GET"' in worker_ts
        and enabled_filter
        and "return json({ flags: enabled });" in worker_ts
    )
    return VerifyCheck(
        "feature_flags_public_route",
        ok,
        "GET /api/_flags selects only enabled rows and returns the enabled map."
        if ok
        else "GET /api/_flags is missing or does not filter to enabled rows.",
    )


def _verify_toggle_route(worker_ts: str | None) -> VerifyCheck:
    if worker_ts is None:
        return VerifyCheck("feature_flags_toggle_owner_gated", False, "missing worker/index.ts.")
    route = f'url.pathname === "{_FLAGS_TOGGLE_ROUTE}" && request.method === "POST"'
    route_pos = worker_ts.find(route)
    auth_pos = worker_ts.find("if (!isAuthorized(request, env))", route_pos)
    body_pos = worker_ts.find("body = await request.json();", route_pos)
    token_ok = (
        "const expected = env.ADMIN_TOKEN;" in worker_ts
        and "if (!expected) return false;" in worker_ts
    )
    ok = route_pos >= 0 and auth_pos > route_pos and body_pos > auth_pos and token_ok
    return VerifyCheck(
        "feature_flags_toggle_owner_gated",
        ok,
        "POST /api/_flags/toggle checks the ADMIN_TOKEN-backed isAuthorized guard "
        "before reading the body."
        if ok
        else (
            "POST /api/_flags/toggle is missing or does not guard with ADMIN_TOKEN before mutation."
        ),
    )


def _verify_admin_section(tree: Mapping[str, str], flags: tuple[FeatureFlag, ...]) -> VerifyCheck:
    components = [
        src
        for path, src in tree.items()
        if path.startswith("src/components/") and path.endswith(".tsx")
    ]
    found = next((src for src in components if "feature-flags-admin" in src), None)
    ok = found is not None and all(flag.key in found for flag in flags)
    return VerifyCheck(
        "feature_flags_admin_section",
        ok,
        "the SPA emits a feature-flags admin section with every spec key."
        if ok
        else "no generated feature-flags admin section contains every spec key.",
    )


def feature_flags_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Verify the applied feature_flags primitive using pure tree/spec checks."""
    del design
    checks: list[VerifyCheck] = []
    seeded, flags = _verify_seeded_table(app, tree)
    checks.append(seeded)
    worker_ts = tree.get("worker/index.ts")
    checks.append(_verify_public_route(worker_ts))
    checks.append(_verify_toggle_route(worker_ts))
    checks.append(_verify_admin_section(tree, flags))
    return _result(checks)


def default_feature_flags_app_spec(name: str, recipe: object) -> AppSpec:
    del name, recipe
    raise ValueError(
        "the 'feature_flags' primitive is an ADD-ON, not a base scaffold: "
        "app_create a lead_gen app first, then "
        "app_add_primitive(primitive_id='feature_flags', spec={...})."
    )


def prepare_feature_flags_app_spec(app: AppSpec) -> AppSpec:
    return app


def generate_feature_flags(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    del app, design
    raise ValueError(
        "the 'feature_flags' primitive does not generate a tree of its own; the "
        "host app's base primitive regenerates from the folded AppSpec."
    )


register_primitive(
    PrimitiveDefinition(
        id=FEATURE_FLAGS_PRIMITIVE_ID,
        default_app_spec=default_feature_flags_app_spec,
        prepare_app_spec=prepare_feature_flags_app_spec,
        generate=generate_feature_flags,
        tier="fillable",
        host_contract=(),
        spec_schema=FeatureFlagsSpec,
        verify=feature_flags_verify,
        apply_spec=apply_feature_flags_spec,
    )
)


__all__ = [
    "FEATURE_FLAGS_PRIMITIVE_ID",
    "FeatureFlag",
    "FeatureFlagsSpec",
    "apply_feature_flags_spec",
    "emit_feature_flags_admin_component",
    "emit_feature_flags_hook_ts",
    "feature_flags_for",
    "feature_flags_verify",
    "generate_feature_flags",
    "is_feature_flags_admin_section",
    "lower_feature_flags_drizzle_ts",
    "lower_feature_flags_schema_sql",
    "lower_feature_flags_styles_css",
    "lower_feature_flags_worker_ts",
    "prepare_feature_flags_app_spec",
]
