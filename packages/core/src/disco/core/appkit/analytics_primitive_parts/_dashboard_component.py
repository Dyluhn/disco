"""Analytics primitive: the owner-token dashboard section component emitter.

Extracted from ``analytics_primitive`` to keep that module's public facade
under the module logical-line budget. The emitted TypeScript ships to real
user apps, so this split is a PURE VERBATIM TEMPLATE PARTITION: each helper
below owns one contiguous slice of the original literal, concatenated in the
original order with no content, whitespace, or ordering changes.
"""

from __future__ import annotations

from ..analytics_primitive import _SUMMARY_ROUTE
from ..spec import Section


def _dashboard_component_head() -> str:
    from ..generator import _ts

    return (
        "/* Auto-generated analytics dashboard component - regenerated from "
        ".disco/appspec.json. */\n"
        'import type { FormEvent } from "react";\n'
        'import { useState } from "react";\n'
        'import { CONTENT } from "../generated/content";\n\n'
        f"const SUMMARY_PATH = {_ts(_SUMMARY_ROUTE)};\n\n"
        "interface HitAggregate {\n"
        "  path: string;\n"
        "  day: string;\n"
        "  hits: number;\n"
        "}\n\n"
        "interface SummaryResponse {\n"
        "  hits?: HitAggregate[];\n"
        "}\n\n"
        "type LoadState =\n"
        '  | { kind: "idle" }\n'
        '  | { kind: "loading" }\n'
        '  | { kind: "ready" }\n'
        '  | { kind: "error"; message: string };\n\n'
    )


def _dashboard_component_state_and_handler(comp: str) -> str:
    from ..generator import _ts

    return (
        f"export default function {comp}() {{\n"
        f"  const c = CONTENT[{_ts(comp)}] ?? {{}};\n"
        '  const [token, setToken] = useState("");\n'
        "  const [rows, setRows] = useState<HitAggregate[]>([]);\n"
        '  const [state, setState] = useState<LoadState>({ kind: "idle" });\n\n'
        "  async function loadDashboard(e: FormEvent<HTMLFormElement>) {\n"
        "    e.preventDefault();\n"
        '    setState({ kind: "loading" });\n'
        "    try {\n"
        "      const response = await fetch(SUMMARY_PATH, {\n"
        "        headers: { Authorization: `Bearer ${token}` },\n"
        "      });\n"
        "      if (!response.ok) {\n"
        "        setState({\n"
        '          kind: "error",\n'
        "          message: response.status === 401\n"
        '            ? "Unauthorized"\n'
        '            : "Could not load analytics",\n'
        "        });\n"
        "        return;\n"
        "      }\n"
        "      const data = (await response.json()) as SummaryResponse;\n"
        "      setRows(Array.isArray(data.hits) ? data.hits : []);\n"
        '      setState({ kind: "ready" });\n'
        "    } catch (error: unknown) {\n"
        "      setState({\n"
        '        kind: "error",\n'
        '        message: error instanceof Error ? error.message : "Network error",\n'
        "      });\n"
        "    }\n"
        "  }\n\n"
    )


def _dashboard_component_markup(
    section: Section,
    classes: str,
    disco_attrs: str,
    heading_attr: str,
    subheading_attr: str,
) -> str:
    from ..generator import _ts

    return (
        "  return (\n"
        f"    <section className={_ts(classes)} id={_ts(section.id)}"
        f" data-appkit-section={_ts(section.id)}{disco_attrs}>\n"
        '      <div className="app-main">\n'
        '        {c.eyebrow ? <p className="eyebrow">{c.eyebrow}</p> : null}\n'
        f"        {{c.heading ? <h2{heading_attr}>{{c.heading}}</h2> : null}}\n"
        f'        {{c.subheading ? <p className="subheading"{subheading_attr}>'
        "{c.subheading}</p> : null}\n"
        '        <form className="lead-form analytics-auth" onSubmit={loadDashboard}>\n'
        '          <label htmlFor="analytics-token">Admin token\n'
        '            <input id="analytics-token" type="password" value={token}\n'
        '              autoComplete="off" onChange={(e) => setToken(e.target.value)} />\n'
        "          </label>\n"
        '          <button className="btn" type="submit"\n'
        '            disabled={state.kind === "loading"}>\n'
        '            {state.kind === "loading" ? "Loading..." : "View analytics"}\n'
        "          </button>\n"
        "        </form>\n"
        '        <div className="form-feedback" aria-live="polite">\n'
        '          {state.kind === "error" ? (\n'
        '            <p className="form-status form-status-error">{state.message}</p>\n'
        "          ) : null}\n"
        '          {state.kind === "ready" && rows.length === 0 ? (\n'
        "            <p>No hits recorded yet.</p>\n"
        "          ) : null}\n"
        "        </div>\n"
        "        {rows.length > 0 ? (\n"
        '          <table className="analytics-table">\n'
        "            <thead><tr><th>Page</th><th>Day</th><th>Hits</th></tr></thead>\n"
        "            <tbody>\n"
        "              {rows.map((row) => (\n"
        "                <tr key={`${row.path}-${row.day}`}>\n"
        "                  <td>{row.path}</td>\n"
        "                  <td>{row.day}</td>\n"
        "                  <td>{row.hits}</td>\n"
        "                </tr>\n"
        "              ))}\n"
        "            </tbody>\n"
        "          </table>\n"
        "        ) : null}\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n"
        "}\n"
    )


def emit_analytics_dashboard_component(comp: str, section: Section) -> str:
    """Emit the owner-token dashboard section component."""
    from ..generator import _disco_field_attr, _disco_section_attrs, _variant_layout

    layout = _variant_layout(section)
    classes = f"section kind-custom variant-{layout} analytics-dashboard"
    disco_attrs = _disco_section_attrs(section)
    heading_attr = _disco_field_attr("heading")
    subheading_attr = _disco_field_attr("subheading")
    return (
        _dashboard_component_head()
        + _dashboard_component_state_and_handler(comp)
        + _dashboard_component_markup(section, classes, disco_attrs, heading_attr, subheading_attr)
    )
