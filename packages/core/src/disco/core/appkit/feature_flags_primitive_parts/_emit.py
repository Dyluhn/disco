"""Feature flags primitive: the client hook and owner-facing admin component.

Extracted from ``feature_flags_primitive`` to keep that module's public facade
under the module logical-line budget; the facade re-imports these names
unchanged so every caller keeps importing from
``disco.core.appkit.feature_flags_primitive``.
"""

from __future__ import annotations

from ..feature_flags_primitive import _FLAGS_GET_ROUTE, _FLAGS_TOGGLE_ROUTE, FeatureFlag
from ..spec import Section
from ._lowering import _ts


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
    from ..generator import _disco_field_attr, _disco_section_attrs

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
