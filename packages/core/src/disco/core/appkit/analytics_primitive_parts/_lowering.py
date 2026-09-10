"""Analytics primitive: lowering of a folded AppSpec into the generated tree.

Splices the `_hits` D1 table, the beacon/summary Worker routes, and the SPA
beacon helper into the base lead_gen emitters. Extracted from
``analytics_primitive`` to keep that module's public facade under the module
logical-line budget; the facade re-imports these names unchanged so every
caller keeps importing from ``disco.core.appkit.analytics_primitive``.
"""

from __future__ import annotations

from ..analytics_primitive import (
    _BEACON_ROUTE,
    _SUMMARY_ROUTE,
    analytics_enabled,
)
from ..spec import AppSpec


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
    from ..generator import _comp_name, _iter_sections, _ts

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
