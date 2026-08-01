"""The records-auth Worker's ``fetch`` dispatcher TS emitter.

Extracted verbatim from ``records_primitive.py`` to reduce module size, and
split (PY-0404) into a pure verbatim template partition: the literal segments
below concatenate in the exact original order with every doubled brace, blank
line, indentation character and placeholder idiom preserved exactly. The
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations

from ..spec import AppSpec


def _emit_auth_fetch_stripe_routes(include_stripe: bool) -> tuple[str, str]:
    if not include_stripe:
        return "", ""
    from ..stripe_worker import (
        emit_stripe_browser_routes_ts,
        emit_stripe_webhook_route_ts,
    )

    return emit_stripe_webhook_route_ts(), emit_stripe_browser_routes_ts()


def _emit_auth_fetch_webhook_routes(webhook_app: AppSpec | None) -> tuple[str, str]:
    if webhook_app is None:
        return "", ""
    from ..webhook_worker import emit_webhook_inbound_route_ts, emit_webhook_outbound_routes_ts

    return (
        emit_webhook_inbound_route_ts(webhook_app),
        emit_webhook_outbound_routes_ts(webhook_app),
    )


def _emit_fetch_ts_helpers() -> str:
    return (
        "type JsonBody = { ok: true; body: unknown } | { ok: false; response: Response };\n\n"
        "function sameOriginOk(request: Request, url: URL): boolean {\n"
        '  const origin = request.headers.get("Origin");\n'
        "  if (origin === null) return true;\n"
        "  try {\n"
        "    return new URL(origin).origin === url.origin;\n"
        "  } catch {\n"
        "    return false;\n"
        "  }\n"
        "}\n\n"
        "function hasJsonContentType(request: Request): boolean {\n"
        '  const contentType = request.headers.get("Content-Type") ?? "";\n'
        '  return contentType.toLowerCase().startsWith("application/json");\n'
        "}\n\n"
        "function requireJsonContentType(request: Request): Response | null {\n"
        "  if (hasJsonContentType(request)) return null;\n"
        '  return json({ error: "unsupported media type" }, 415);\n'
        "}\n\n"
        "function rawPathname(requestUrl: string, origin: string): string {\n"
        "  const rest = requestUrl.startsWith(origin) "
        "? requestUrl.slice(origin.length) : requestUrl;\n"
        "  const end = rest.search(/[?#]/);\n"
        "  const path = end === -1 ? rest : rest.slice(0, end);\n"
        '  return path === "" ? "/" : path;\n'
        "}\n\n"
        "function decodedPathname(pathname: string): string | null {\n"
        "  try {\n"
        "    return decodeURIComponent(pathname);\n"
        "  } catch {\n"
        "    return null;\n"
        "  }\n"
        "}\n\n"
        "function isApiPath(\n"
        "  rawPath: string,\n"
        "  urlPath: string,\n"
        "  decodedPath: string | null\n"
        "): boolean {\n"
        "  return (\n"
        '    rawPath.startsWith("/api/") ||\n'
        '    urlPath.startsWith("/api/") ||\n'
        "    decodedPath === null ||\n"
        '    decodedPath.startsWith("/api/")\n'
        "  );\n"
        "}\n\n"
        "async function readJsonBody(request: Request): Promise<JsonBody> {\n"
        "  try {\n"
        "    return { ok: true, body: await request.json() };\n"
        "  } catch {\n"
        '    return { ok: false, response: json({ error: "invalid JSON" }, 400) };\n'
        "  }\n"
        "}\n\n"
    )


def _emit_fetch_ts_dispatch(
    stripe_webhook_route: str,
    webhook_inbound_routes: str,
    stripe_browser_routes: str,
    webhook_outbound_routes: str,
) -> str:
    return (
        "export default {\n"
        "  async fetch(request: Request, env: Env): Promise<Response> {\n"
        "    const url = new URL(request.url);\n"
        "    const rawPath = rawPathname(request.url, url.origin);\n"
        "    const decodedPath = decodedPathname(rawPath);\n"
        "    const apiPath = isApiPath(rawPath, url.pathname, decodedPath);\n"
        + stripe_webhook_route
        + webhook_inbound_routes
        + '    if (request.method === "POST" && apiPath && !sameOriginOk(request, url)) {\n'
        '      return json({ error: "bad origin" }, 403);\n'
        "    }\n"
        + stripe_browser_routes
        + webhook_outbound_routes
        + '    if (rawPath === "/api/register" && request.method === "POST") {\n'
        "      const contentTypeError = requireJsonContentType(request);\n"
        "      if (contentTypeError !== null) return contentTypeError;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return registerUser(request, env, parsed.body);\n"
        "    }\n"
        '    if (rawPath === "/api/login" && request.method === "POST") {\n'
        "      const contentTypeError = requireJsonContentType(request);\n"
        "      if (contentTypeError !== null) return contentTypeError;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return loginUser(env, parsed.body);\n"
        "    }\n"
        '    if (rawPath === "/api/logout" && request.method === "POST") {\n'
        "      return logoutUser(request, env);\n"
        "    }\n"
        "    const route = ROUTES[rawPath];\n"
        '    if (route && request.method === "POST") {\n'
        "      const contentTypeError = requireJsonContentType(request);\n"
        "      if (contentTypeError !== null) return contentTypeError;\n"
        "      const session = await resolveSession(request, env);\n"
        "      const denied = authorizeSession(session, route.writeRoles);\n"
        "      if (denied !== null) return denied;\n"
        "      const parsed = await readJsonBody(request);\n"
        "      if (!parsed.ok) return parsed.response;\n"
        "      return route.post(env, parsed.body);\n"
        "    }\n"
        '    if (route && request.method === "GET") {\n'
        "      const session = await resolveSession(request, env);\n"
        "      const denied = authorizeSession(session, route.readRoles);\n"
        "      if (denied !== null) return denied;\n"
        "      return route.get(env);\n"
        "    }\n"
        "    if (apiPath) {\n"
        '      return json({ error: "not found" }, 404);\n'
        "    }\n"
        "    return env.ASSETS.fetch(request);\n"
        "  },\n"
        "};\n"
    )


def _emit_auth_fetch_ts(*, include_stripe: bool = False, webhook_app: AppSpec | None = None) -> str:
    stripe_webhook_route, stripe_browser_routes = _emit_auth_fetch_stripe_routes(include_stripe)
    webhook_inbound_routes, webhook_outbound_routes = _emit_auth_fetch_webhook_routes(webhook_app)
    return _emit_fetch_ts_helpers() + _emit_fetch_ts_dispatch(
        stripe_webhook_route,
        webhook_inbound_routes,
        stripe_browser_routes,
        webhook_outbound_routes,
    )
