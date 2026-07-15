"""The AppKit `analytics` primitive (Epic 6.1): first-party page analytics.

This is an ADD-ON primitive for lead_gen-shaped Cloudflare/D1 apps. The fillable
spec is intentionally small: an optional dashboard page title. The fold persists
resolved metadata on `AppSpec.analytics` and appends an owner-facing `/analytics`
SPA page with a dashboard section. The generated tree then gains:

* a `_hits` D1 table in `schema.sql`;
* a public POST `/api/_hits` beacon route with a simple per-worker rate counter;
* an owner-gated GET `/api/_hits/summary` route that reuses the existing
  `Authorization: Bearer <ADMIN_TOKEN>` convention;
* a dashboard component that reads the summary route only after the owner enters
  the token;
* a tiny no-dependency SPA entry helper that emits a beacon on initial load and
  history route changes.

Layering: pure core only. Generator internals are imported lazily inside emitter
helpers so registration can happen from `generator.py`'s bottom-of-module import.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .primitives import (
    ANALYTICS_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
    resolve_primitive,
)
from .spec import (
    DEFAULT_ANALYTICS_DASHBOARD_PAGE_TITLE,
    MAX_ANALYTICS_DASHBOARD_PAGE_TITLE,
    AnalyticsMeta,
    AppSpec,
    DesignSpec,
    Page,
    Section,
    SectionContent,
)
from .worker_inspect import _route_handler, _strip_ts_comments

if TYPE_CHECKING:
    from .recipes import SiteRecipe

_DASHBOARD_PAGE_ID = "analytics"
_DASHBOARD_ROUTE = "/analytics"
_DASHBOARD_SECTION_ID = "analytics-dashboard"
_HITS_TABLE = "_hits"
_SUMMARY_ROUTE = "/api/_hits/summary"
_BEACON_ROUTE = "/api/_hits"


class AnalyticsSpec(BaseModel):
    """The analytics primitive's declarative spec.

    `dashboard_page_title` is optional via its default. Unknown keys are refused
    so app_add_primitive can return a self-recovering schema error.
    """

    model_config = ConfigDict(extra="forbid")

    dashboard_page_title: str = Field(
        default=DEFAULT_ANALYTICS_DASHBOARD_PAGE_TITLE,
        min_length=1,
        max_length=MAX_ANALYTICS_DASHBOARD_PAGE_TITLE,
        description="Owner dashboard page title (optional; 1-120 chars).",
    )

    @field_validator("dashboard_page_title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dashboard_page_title must not be blank")
        return stripped


# ---- fold: AnalyticsSpec -> AppSpec -------------------------------------------


def analytics_enabled(app: AppSpec) -> bool:
    """True when the folded AppSpec asks the shared emitters to lower analytics."""
    return app.analytics is not None


def is_analytics_dashboard_section(section: Section) -> bool:
    """The fold marker for the generated dashboard component."""
    return section.id == _DASHBOARD_SECTION_ID and section.content_ref == _HITS_TABLE


def _dashboard_section(title: str) -> Section:
    return Section(
        id=_DASHBOARD_SECTION_ID,
        kind="custom",
        content_ref=_HITS_TABLE,
        content=SectionContent(
            heading=title,
            subheading="Owner-only page analytics.",
        ),
    )


def _dashboard_page(title: str) -> Page:
    return Page(
        id=_DASHBOARD_PAGE_ID,
        route=_DASHBOARD_ROUTE,
        title=title,
        sections=(_dashboard_section(title),),
    )


def _upsert_dashboard_page(data: dict[str, Any], title: str) -> None:
    pages = list(data.get("pages") or [])
    page_idx = next(
        (i for i, page in enumerate(pages) if page.get("id") == _DASHBOARD_PAGE_ID),
        None,
    )
    dashboard_json = _dashboard_page(title).model_dump(mode="json")
    section_json = dashboard_json["sections"][0]

    if page_idx is None:
        route_owner = next(
            (page.get("id") for page in pages if page.get("route") == _DASHBOARD_ROUTE),
            None,
        )
        if route_owner is not None:
            raise ValueError(
                f"route {_DASHBOARD_ROUTE!r} is already used by page {route_owner!r}; "
                "rename that page before adding analytics"
            )
        if any(
            section.get("id") == _DASHBOARD_SECTION_ID
            for page in pages
            for section in page.get("sections", [])
        ):
            raise ValueError(
                f"section id {_DASHBOARD_SECTION_ID!r} already exists; rename that "
                "section before adding analytics"
            )
        data["pages"] = [*pages, dashboard_json]
        return

    page = dict(pages[page_idx])
    if page.get("route") != _DASHBOARD_ROUTE:
        raise ValueError(
            f"page {_DASHBOARD_PAGE_ID!r} already exists with route "
            f"{page.get('route')!r}; analytics needs {_DASHBOARD_ROUTE!r}"
        )
    sections = list(page.get("sections") or [])
    section_idx = next(
        (i for i, section in enumerate(sections) if section.get("id") == _DASHBOARD_SECTION_ID),
        None,
    )
    if section_idx is None:
        if any(
            section.get("id") == _DASHBOARD_SECTION_ID
            for i, other in enumerate(pages)
            if i != page_idx
            for section in other.get("sections", [])
        ):
            raise ValueError(f"section id {_DASHBOARD_SECTION_ID!r} already exists on another page")
        sections.append(section_json)
    else:
        existing = dict(sections[section_idx])
        if existing.get("kind") != "custom" or existing.get("content_ref") != _HITS_TABLE:
            raise ValueError(
                f"section id {_DASHBOARD_SECTION_ID!r} is already taken by a non-analytics "
                "section; rename it before adding analytics"
            )
        if existing.get("variant_id") is not None:
            section_json["variant_id"] = existing["variant_id"]
        sections[section_idx] = {
            **existing,
            "kind": "custom",
            "content_ref": _HITS_TABLE,
            "content": section_json["content"],
        }
    page["title"] = title
    page["sections"] = sections
    pages[page_idx] = page
    data["pages"] = pages


def apply_analytics_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold AnalyticsSpec into a lead_gen-shaped AppSpec.

    The fold uses the house dump -> mutate -> re-validate pattern. Directory is
    static and records owns a different Worker contract, so this slice scopes to
    the lead_gen shape that already has D1 plus owner Bearer-token auth.
    """
    if not isinstance(spec, AnalyticsSpec):
        raise TypeError(f"apply_spec for {ANALYTICS_PRIMITIVE_ID!r} needs an AnalyticsSpec")

    base = resolve_primitive(app.app_kind)
    if base.id != LEAD_GEN_PRIMITIVE_ID:
        raise ValueError(
            "the analytics primitive currently folds only into lead_gen-shaped apps; "
            f"this app's app_kind {app.app_kind!r} lowers through {base.id!r}"
        )
    if not app.pages:
        raise ValueError("the app has no pages to place the analytics dashboard on")

    title = spec.dashboard_page_title
    data = app.model_dump(mode="json")
    data["analytics"] = AnalyticsMeta(dashboard_page_title=title).model_dump(mode="json")
    _upsert_dashboard_page(data, title)
    return AppSpec.model_validate(data)


# ---- lowering: folded AppSpec -> generated analytics surface ------------------


def lower_analytics_schema_sql(base: str, app: AppSpec) -> str:
    """Append the `_hits` D1 table. Unenabled apps return `base` byte-identically."""
    if not analytics_enabled(app):
        return base
    return (
        base.rstrip() + "\n\n"
        "-- Analytics primitive (Epic 6.1): first-party page hits.\n"
        'CREATE TABLE IF NOT EXISTS "_hits" (\n'
        '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  "path" TEXT NOT NULL,\n'
        "  \"ts\" TEXT NOT NULL DEFAULT (datetime('now')),\n"
        '  "referrer" TEXT,\n'
        '  "ua_class" TEXT NOT NULL\n'
        ");\n"
        'CREATE INDEX IF NOT EXISTS "idx_hits_path_ts" ON "_hits" ("path", "ts");\n'
    )


def _replace_once(source: str, anchor: str, replacement: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise ValueError(
            f"generator invariant broken: expected exactly one occurrence of "
            f"{anchor!r}, found {count}"
        )
    return source.replace(anchor, replacement)


_ANALYTICS_WORKER_DEFS = (
    "// ---- Analytics primitive (Epic 6.1): first-party page hits --------------\n"
    "const HIT_PATH_MAX = 2048;\n"
    "const HIT_REFERRER_MAX = 2048;\n"
    "const HIT_RATE_LIMIT_PER_MINUTE = 240;\n"
    "let hitRateWindow = 0;\n"
    "let hitRateCount = 0;\n\n"
    "interface HitAggregate {\n"
    "  path: string;\n"
    "  day: string;\n"
    "  hits: number;\n"
    "}\n\n"
    "type HitCheck =\n"
    "  | { ok: true; path: string; referrer: string | null }\n"
    "  | { ok: false; error: string };\n\n"
    "function hitRateLimited(): boolean {\n"
    "  const windowStart = Math.floor(Date.now() / 60000);\n"
    "  if (windowStart !== hitRateWindow) {\n"
    "    hitRateWindow = windowStart;\n"
    "    hitRateCount = 0;\n"
    "  }\n"
    "  hitRateCount += 1;\n"
    "  return hitRateCount > HIT_RATE_LIMIT_PER_MINUTE;\n"
    "}\n\n"
    "function uaClass(ua: string): string {\n"
    "  const low = ua.toLowerCase();\n"
    '  if (/bot|crawl|spider|slurp/.test(low)) return "bot";\n'
    '  if (/ipad|tablet/.test(low)) return "tablet";\n'
    '  if (/mobi|android|iphone/.test(low)) return "mobile";\n'
    '  return ua ? "desktop" : "unknown";\n'
    "}\n\n"
    "function cleanOptionalString(value: unknown, max: number): string | null {\n"
    '  if (value === undefined || value === null || value === "") return null;\n'
    '  if (typeof value !== "string" || value.length > max) return null;\n'
    "  return value;\n"
    "}\n\n"
    "function validateHit(body: unknown): HitCheck {\n"
    '  if (typeof body !== "object" || body === null || Array.isArray(body)) {\n'
    '    return { ok: false, error: "request body must be a JSON object" };\n'
    "  }\n"
    "  const rec = body as Record<string, unknown>;\n"
    '  if (typeof rec.path !== "string" || !rec.path.startsWith("/")\n'
    "    || rec.path.length > HIT_PATH_MAX) {\n"
    '    return { ok: false, error: "path must be an absolute app path" };\n'
    "  }\n"
    "  const referrer = cleanOptionalString(rec.referrer, HIT_REFERRER_MAX);\n"
    "  if (\n"
    '    rec.referrer !== undefined && rec.referrer !== null && rec.referrer !== ""\n'
    "    && referrer === null\n"
    "  ) {\n"
    '    return { ok: false, error: "referrer must be a bounded string when provided" };\n'
    "  }\n"
    "  return { ok: true, path: rec.path, referrer };\n"
    "}\n\n"
    "async function recordHit(request: Request, env: Env, body: unknown): Promise<Response> {\n"
    "  if (hitRateLimited()) {\n"
    '    return json({ error: "rate limited" }, 429);\n'
    "  }\n"
    "  const check = validateHit(body);\n"
    "  if (!check.ok) {\n"
    "    return json({ error: check.error }, 400);\n"
    "  }\n"
    "  try {\n"
    "    await env.DB.prepare(\n"
    '      \'INSERT INTO "_hits" ("path", "referrer", "ua_class") VALUES (?1, ?2, ?3)\'\n'
    "    )\n"
    "      .bind(\n"
    "        check.path,\n"
    "        check.referrer,\n"
    '        uaClass(request.headers.get("User-Agent") ?? "")\n'
    "      )\n"
    "      .run();\n"
    "  } catch {\n"
    '    return json({ error: "could not record hit" }, 500);\n'
    "  }\n"
    "  return json({ ok: true }, 201);\n"
    "}\n\n"
    "async function listHitAggregates(env: Env): Promise<HitAggregate[]> {\n"
    "  const rows = await env.DB.prepare(\n"
    '    \'SELECT "path" AS path, substr("ts", 1, 10) AS day, COUNT(*) AS hits \'\n'
    '      + \'FROM "_hits" GROUP BY "path", day ORDER BY day DESC, hits DESC LIMIT 200\'\n'
    "  ).all<HitAggregate>();\n"
    "  return rows.results ?? [];\n"
    "}\n\n"
)

_ANALYTICS_DISPATCH_TS = (
    "    // Analytics primitive (Epic 6.1): public beacon + owner-only summary.\n"
    f'    if (url.pathname === "{_BEACON_ROUTE}" && request.method === "POST") {{\n'
    "      let body: unknown;\n"
    "      try {\n"
    "        body = await request.json();\n"
    "      } catch {\n"
    '        return json({ error: "invalid JSON" }, 400);\n'
    "      }\n"
    "      return recordHit(request, env, body);\n"
    "    }\n"
    f'    if (url.pathname === "{_SUMMARY_ROUTE}" && request.method === "GET") {{\n'
    "      if (!isAuthorized(request, env)) {\n"
    '        return json({ error: "unauthorized" }, 401);\n'
    "      }\n"
    "      const hits = await listHitAggregates(env);\n"
    "      return json({ hits });\n"
    "    }\n"
)


def lower_analytics_worker_ts(base: str, app: AppSpec) -> str:
    """Splice analytics Worker defs/routes into a lead_gen Worker."""
    if not analytics_enabled(app):
        return base
    out = _replace_once(base, "export default {\n", _ANALYTICS_WORKER_DEFS + "export default {\n")
    return _replace_once(
        out,
        "    return env.ASSETS.fetch(request);\n",
        _ANALYTICS_DISPATCH_TS + "    return env.ASSETS.fetch(request);\n",
    )


def lower_analytics_main_tsx(base: str, app: AppSpec) -> str:
    """Append the tiny SPA beacon helper to `src/main.tsx` when enabled."""
    if not analytics_enabled(app):
        return base
    return (
        base.rstrip() + "\n\n"
        'const ANALYTICS_BEACON_PATH = "/api/_hits";\n\n'
        "function analyticsCurrentPath(): string {\n"
        "  return window.location.pathname + window.location.search;\n"
        "}\n\n"
        "function sendAnalyticsHit(path: string): void {\n"
        "  const payload = JSON.stringify({ path, referrer: document.referrer || null });\n"
        "  if (navigator.sendBeacon) {\n"
        '    const blob = new Blob([payload], { type: "application/json" });\n'
        "    if (navigator.sendBeacon(ANALYTICS_BEACON_PATH, blob)) return;\n"
        "  }\n"
        "  void fetch(ANALYTICS_BEACON_PATH, {\n"
        '    method: "POST",\n'
        '    headers: { "Content-Type": "application/json" },\n'
        "    body: payload,\n"
        "    keepalive: true,\n"
        "  }).catch(() => undefined);\n"
        "}\n\n"
        "function installAnalyticsBeacon(): void {\n"
        '  let lastPath = "";\n'
        "  const emit = () => {\n"
        "    const path = analyticsCurrentPath();\n"
        "    if (path === lastPath) return;\n"
        "    lastPath = path;\n"
        "    sendAnalyticsHit(path);\n"
        "  };\n"
        "  const originalPushState: typeof window.history.pushState =\n"
        "    window.history.pushState;\n"
        "  window.history.pushState = function (...args: Parameters<typeof originalPushState>) {\n"
        "    const ret = originalPushState.apply(window.history, args);\n"
        "    queueMicrotask(emit);\n"
        "    return ret;\n"
        "  };\n"
        "  const originalReplaceState: typeof window.history.replaceState =\n"
        "    window.history.replaceState;\n"
        "  window.history.replaceState = function (\n"
        "    ...args: Parameters<typeof originalReplaceState>\n"
        "  ) {\n"
        "    const ret = originalReplaceState.apply(window.history, args);\n"
        "    queueMicrotask(emit);\n"
        "    return ret;\n"
        "  };\n"
        '  window.addEventListener("popstate", emit);\n'
        "  emit();\n"
        "}\n\n"
        "installAnalyticsBeacon();\n"
    )


def lower_analytics_app_tsx(base: str, app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    """Make the lead-gen shell route-aware once analytics adds `/analytics`."""
    if not analytics_enabled(app):
        return base
    from .generator import _comp_name, _iter_sections, _ts

    imports: list[str] = []
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        imports.append(f'import {comp} from "./components/{comp}";')
    page_funcs: list[str] = []
    route_entries: list[str] = []
    for i, page in enumerate(app.pages):
        fn = f"Page{i}"
        renders = "\n".join(
            f"      <{_comp_name(names, page, section)} />" for section in page.sections
        )
        body = (renders + "\n") if renders else ""
        page_funcs.append(
            f"function {fn}(): ReactElement {{\n  return (\n    <>\n{body}    </>\n  );\n}}\n"
        )
        route_entries.append(f"  {_ts(page.route.rstrip('/') or '/')}: {fn},")
    fallback = "Page0" if app.pages else "() => null"
    return (
        "/* Auto-generated analytics route-aware app shell - regenerated from "
        ".disco/appspec.json. */\n"
        'import { type ReactElement } from "react";\n'
        + "\n".join(imports)
        + "\n\n"
        + "\n".join(page_funcs)
        + "\n"
        "const ROUTES: Record<string, () => ReactElement> = {\n"
        + "\n".join(route_entries)
        + "\n};\n\n"
        "export default function App(): ReactElement {\n"
        '  const path = window.location.pathname.replace(/\\/+$/, "") || "/";\n'
        f"  const Page = ROUTES[path] ?? {fallback};\n"
        "  return (\n"
        '    <div className="app-main">\n'
        "      <Page />\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


def emit_analytics_dashboard_component(comp: str, section: Section) -> str:
    """Emit the owner-token dashboard section component."""
    from .generator import _disco_field_attr, _disco_section_attrs, _ts, _variant_layout

    layout = _variant_layout(section)
    classes = f"section kind-custom variant-{layout} analytics-dashboard"
    disco_attrs = _disco_section_attrs(section)
    heading_attr = _disco_field_attr("heading")
    subheading_attr = _disco_field_attr("subheading")
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


# ---- verify hook --------------------------------------------------------------


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    n_fail = sum(1 for check in checks if not check.passed)
    return PrimitiveVerifyResult(
        ok=n_fail == 0,
        detail=f"{len(checks) - n_fail} passed / {n_fail} failed",
        checks=tuple(checks),
    )


def _schema_verify(schema_sql: str | None) -> VerifyCheck:
    if schema_sql is None:
        return VerifyCheck("analytics_schema", False, "schema.sql is missing.")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(":memory:")
        conn.executescript(schema_sql)
        rows = conn.execute('PRAGMA table_info("_hits")').fetchall()
        columns = {str(row[1]): row for row in rows}
        expected = {"path", "ts", "referrer", "ua_class"}
        missing = sorted(expected - set(columns))
        if missing:
            return VerifyCheck(
                "analytics_schema",
                False,
                f"_hits table is missing column(s): {', '.join(missing)}.",
            )
        not_null = {name for name, row in columns.items() if int(row[3]) == 1}
        if not {"path", "ts", "ua_class"} <= not_null:
            return VerifyCheck(
                "analytics_schema",
                False,
                "_hits path, ts and ua_class columns must be NOT NULL.",
            )
        conn.execute(
            'INSERT INTO "_hits" ("path", "referrer", "ua_class") VALUES (?, ?, ?)',
            ("/", None, "desktop"),
        )
        count = conn.execute('SELECT COUNT(*) FROM "_hits"').fetchone()
        if count is None or int(count[0]) != 1:
            return VerifyCheck(
                "analytics_schema",
                False,
                "a representative _hits row could not be read back.",
            )
    except sqlite3.Error as exc:
        return VerifyCheck("analytics_schema", False, f"schema.sql is not valid: {exc}")
    finally:
        if conn is not None:
            conn.close()
    return VerifyCheck(
        "analytics_schema",
        True,
        "_hits table exists with path/ts/referrer/ua_class and accepts a hit row.",
    )


def _summary_route_guarded(block: str | None) -> bool:
    if block is None:
        return False
    return (
        re.search(
            r"^\s*\{\s*if\s*\(\s*!\s*isAuthorized\s*\(\s*request\s*,\s*env\s*\)\s*\)"
            r"\s*\{\s*return\s+json\s*\([^;]*,\s*401\s*\)",
            block,
            re.DOTALL,
        )
        is not None
    )


def _worker_verify(worker_ts: str | None) -> VerifyCheck:
    if worker_ts is None:
        return VerifyCheck("analytics_worker_routes", False, "worker/index.ts is missing.")
    src = _strip_ts_comments(worker_ts)
    post_block = _route_handler(
        src,
        r'url\.pathname\s*===\s*"/api/_hits"\s*&&\s*request\.method\s*===\s*"POST"',
    )
    summary_block = _route_handler(
        src,
        r'url\.pathname\s*===\s*"/api/_hits/summary"\s*&&\s*request\.method\s*===\s*"GET"',
    )
    post_ok = (
        post_block is not None
        and "recordHit(request, env, body)" in post_block
        and "hitRateLimited()" in src
        and 'INSERT INTO "_hits"' in src
        and ".bind(" in src
    )
    read_ok = (
        summary_block is not None
        and _summary_route_guarded(summary_block)
        and "listHitAggregates(env)" in summary_block
    )
    if post_ok and read_ok:
        return VerifyCheck(
            "analytics_worker_routes",
            True,
            "worker has public /api/_hits POST with rate counter + parameterized insert "
            "and owner-gated /api/_hits/summary read.",
        )
    reasons: list[str] = []
    if not post_ok:
        reasons.append("missing or incomplete public /api/_hits POST beacon route")
    if not read_ok:
        reasons.append("missing or unguarded owner /api/_hits/summary read route")
    return VerifyCheck("analytics_worker_routes", False, "; ".join(reasons))


def _dashboard_verify(app: AppSpec | None, tree: Mapping[str, str]) -> VerifyCheck:
    app_has_section = bool(
        app is not None
        and app.analytics is not None
        and any(
            page.route == _DASHBOARD_ROUTE
            and any(is_analytics_dashboard_section(section) for section in page.sections)
            for page in app.pages
        )
    )
    component_sources = [
        source
        for path, source in tree.items()
        if path.startswith("src/components/") and path.endswith(".tsx")
    ]
    tree_has_section = any(
        f'id="{_DASHBOARD_SECTION_ID}"' in source
        and _SUMMARY_ROUTE in source
        and "Authorization" in source
        and "Bearer" in source
        for source in component_sources
    )
    ok = app_has_section and tree_has_section
    return VerifyCheck(
        "analytics_dashboard_section",
        ok,
        "AppSpec carries the analytics dashboard section and the generated component "
        "fetches the owner-gated summary route."
        if ok
        else "analytics dashboard section is missing from the AppSpec or generated component.",
    )


def _beacon_verify(main_tsx: str | None) -> VerifyCheck:
    src = _strip_ts_comments(main_tsx or "")
    ok = all(
        needle in src
        for needle in (
            "installAnalyticsBeacon",
            '"/api/_hits"',
            "sendBeacon",
            "pushState",
            "popstate",
        )
    )
    return VerifyCheck(
        "analytics_spa_beacon",
        ok,
        "src/main.tsx installs a no-dependency beacon helper for initial load and route changes."
        if ok
        else "src/main.tsx does not contain the analytics beacon route-change helper.",
    )


def analytics_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Verify the generated analytics surface structurally and with sqlite."""
    del design
    checks = [
        _schema_verify(tree.get("schema.sql")),
        _worker_verify(tree.get("worker/index.ts")),
        _dashboard_verify(app, tree),
        _beacon_verify(tree.get("src/main.tsx")),
    ]
    return _result(checks)


# ---- registration ------------------------------------------------------------


def default_analytics_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The analytics primitive is an add-on, never a base scaffold."""
    del name, recipe
    raise ValueError(
        "the 'analytics' primitive is an ADD-ON, not a base scaffold: app_create a "
        "base lead_gen app first, then app_add_primitive(primitive_id='analytics', spec={})."
    )


def prepare_analytics_app_spec(app: AppSpec) -> AppSpec:
    return app


def generate_analytics(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    del app, design
    raise ValueError(
        "the 'analytics' primitive does not generate a tree of its own; the host "
        "app's base primitive regenerates from the folded AppSpec."
    )


register_primitive(
    PrimitiveDefinition(
        id=ANALYTICS_PRIMITIVE_ID,
        default_app_spec=default_analytics_app_spec,
        prepare_app_spec=prepare_analytics_app_spec,
        generate=generate_analytics,
        tier="fillable",
        host_contract=(),
        spec_schema=AnalyticsSpec,
        verify=analytics_verify,
        apply_spec=apply_analytics_spec,
    )
)


__all__ = [
    "ANALYTICS_PRIMITIVE_ID",
    "AnalyticsSpec",
    "analytics_enabled",
    "analytics_verify",
    "apply_analytics_spec",
    "default_analytics_app_spec",
    "emit_analytics_dashboard_component",
    "generate_analytics",
    "is_analytics_dashboard_section",
    "lower_analytics_app_tsx",
    "lower_analytics_main_tsx",
    "lower_analytics_schema_sql",
    "lower_analytics_worker_ts",
    "prepare_analytics_app_spec",
]
