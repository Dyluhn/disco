/**
 * The HTTP client for the data-access layer (the ONE place that talks to the
 * backend; components never import this — only the api modules + hooks do).
 *
 * Live vs fixtures: when `VITE_API_BASE` is set, the api modules call the real
 * endpoints (see api-endpoints.md); when it's unset they replay in-repo fixtures
 * — the "build against fixtures first, then wire live" discipline. Tests run with
 * no base URL, so they exercise the fixtures (no network).
 */

/** Runtime config injected by the self-host nginx (a `/env.js` that sets
 * `window.__PMX_ENV` from the deployer's environment), so ONE built image works at
 * any host/port without rebaking. Build-time `VITE_*` still wins in dev; in
 * vitest/jsdom there's no `__PMX_ENV`, so this is `{}` and fixture mode is preserved. */
const RT: { API_BASE?: string; AGENT_BASE?: string; OWNER_ID?: string } =
  (globalThis as { __PMX_ENV?: { API_BASE?: string; AGENT_BASE?: string; OWNER_ID?: string } })
    .__PMX_ENV ?? {};

const BASE = ((RT.API_BASE ?? import.meta.env.VITE_API_BASE) ?? "").replace(/\/+$/, "");

/** The agent-server base (the live WebSocket surface — loops + research). Distinct
 * from VITE_API_BASE (the app-server: settings + library) because they are
 * different services/ports. Unset → research stays fixture-backed (offline/tests). */
const AGENT_BASE = ((RT.AGENT_BASE ?? import.meta.env.VITE_AGENT_BASE) ?? "").replace(/\/+$/, "");

/** The owner whose conversations we read/write (no auth in v1; an explicit id). */
export const OWNER_ID = RT.OWNER_ID ?? import.meta.env.VITE_OWNER_ID ?? "local";

/** True when a backend base URL is configured — the api modules call it live. */
export function isLive(): boolean {
  return BASE.length > 0;
}

/** True when neither backend (app-server nor agent-server) is configured —
 * the UI is running entirely on fixture data because /env.js was absent, failed
 * to load, or yielded no URLs. The shell surfaces a "Demo data — no backend
 * connected" badge whenever this is true. */
export function isDemoMode(): boolean {
  return !isLive() && !agentLive();
}

/** The agent-server research WebSocket URL (ws://… derived from VITE_AGENT_BASE),
 * or null when unconfigured — callers fall back to the fixture stream. */
export function researchWsUrl(): string | null {
  if (!AGENT_BASE) return null;
  return `${AGENT_BASE.replace(/^http/, "ws")}/ws/research`;
}

/** True when the agent-server is configured — the Build surface runs live. */
export function agentLive(): boolean {
  return AGENT_BASE.length > 0;
}

/** An agent-server WebSocket URL for `path` (e.g. a conversation stream), or null
 * when unconfigured (Build falls back to a fixture trace offline/in tests). */
export function agentWsUrl(path: string): string | null {
  if (!AGENT_BASE) return null;
  return `${AGENT_BASE.replace(/^http/, "ws")}${path}`;
}

/** Origin-true preview URL (DC-01): http://{cid8}-{port}.localhost:8000/.
 * cid8 = first 8 chars of the uuid part. 127.0.0.1/localhost bases map to the
 * .localhost zone; any other base (future MagicDNS) gets the same {cid8}-{port}.
 * prefix on its hostname. Null when AGENT_BASE is unconfigured. */
export function previewHostUrl(cid: string, port: number, base: string = AGENT_BASE): string | null {
  if (!base) return null;
  const cid8 = cid.replace(/^conv_/, "").slice(0, 8);
  const url = new URL(base);
  if (url.hostname === "127.0.0.1" || url.hostname === "localhost") {
    url.hostname = `${cid8}-${port}.localhost`;
  } else {
    url.hostname = `${cid8}-${port}.${url.hostname}`;
  }
  return url.toString().replace(/\/+$/, "");
}

/** The agent-server HTTP base (for forming proxied URLs the browser loads directly, e.g.
 * the preview iframe). Empty when unconfigured. */
export function agentHttpBase(): string {
  return AGENT_BASE;
}

/** A GET against the AGENT-server (e.g. the driver model catalogue). */
export function agentGet<T>(path: string): Promise<T> {
  return fetch(`${AGENT_BASE}${path}`, { headers: { accept: "application/json" } }).then(parse<T>);
}

/** A REST call against the AGENT-server (loops/kill) — distinct from apiSend, which
 * targets the app-server (settings/library). */
export function agentSend<T>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  return fetch(`${AGENT_BASE}${path}`, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(parse<T>);
}

/** An API error that carries the real backend message (surfaced to the UI). */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // Preserve the real backend error content — never flatten it.
    const text = await res.text().catch(() => "");
    throw new ApiError(text || `${res.status} ${res.statusText}`, res.status);
  }
  // 204 No Content (e.g. DELETE) has an empty body — res.json() would throw
  // "Unexpected end of JSON input". Short-circuit to undefined for these.
  if (res.status === 204 || res.headers.get("content-length") === "0") {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return fetch(`${BASE}${path}`, { headers: { accept: "application/json" } }).then(parse<T>);
}

export function apiSend<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  return fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(parse<T>);
}

/** A small artificial delay for the fixture paths (keeps loading states visible). */
export function fixtureDelay(ms = 20): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
