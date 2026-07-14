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
 * `window.__DISCO_ENV` from the deployer's environment), so ONE built image works at
 * any host/port without rebaking. Build-time `VITE_*` still wins in dev; in
 * vitest/jsdom there's no global, so this is `{}` and fixture mode is preserved.
 * Legacy `__PMX_ENV` is honored as a fallback (the rename compat path). */
type RtEnv = { API_BASE?: string; AGENT_BASE?: string };
const RT: RtEnv =
  (globalThis as { __DISCO_ENV?: RtEnv; __PMX_ENV?: RtEnv }).__DISCO_ENV ??
  (globalThis as { __DISCO_ENV?: RtEnv; __PMX_ENV?: RtEnv }).__PMX_ENV ??
  {};

const BASE = ((RT.API_BASE ?? import.meta.env.VITE_API_BASE) ?? "").replace(/\/+$/, "");

/** The agent-server base (the live WebSocket surface — loops + research). Distinct
 * from VITE_API_BASE (the app-server: settings + library) because they are
 * different services/ports. Unset → research stays fixture-backed (offline/tests). */
const AGENT_BASE = ((RT.AGENT_BASE ?? import.meta.env.VITE_AGENT_BASE) ?? "").replace(/\/+$/, "");

const CSRF_HEADER = "X-Disco-CSRF";
const csrfByBase = new Map<string, string>();
const sessionInitByBase = new Map<string, Promise<void>>();
// Both servers share ONE session cookie (same host + signing secret), so two
// bases minting CONCURRENTLY race: the second Set-Cookie replaces the session
// the first base's CSRF token was bound to → "csrf required" on a fresh visit.
// Serialize all session inits through one chain; a base whose init runs second
// then sees the already-authenticated shared session and adopts its token.
let sessionInitChain: Promise<void> = Promise.resolve();

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

/** ws(s):// base for a configured HTTP base. Handles BOTH base shapes: an
 * absolute URL (split-origin dev: http://host:8000 → ws://host:8000) and the
 * same-origin relative prefix (the single-front-door default: "/svc/agent" →
 * ws://<page-host>/svc/agent — nginx upgrades and forwards to the backend). */
function wsBase(base: string): string {
  if (base.startsWith("http")) return base.replace(/^http/, "ws");
  const loc = globalThis.location;
  const proto = loc?.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${loc?.host ?? "localhost"}${base}`;
}

/** The agent-server research WebSocket URL (ws://… derived from VITE_AGENT_BASE),
 * or null when unconfigured — callers fall back to the fixture stream. */
export function researchWsUrl(): string | null {
  if (!AGENT_BASE) return null;
  return `${wsBase(AGENT_BASE)}/ws/research`;
}

export function ensureAgentSession(): Promise<void> {
  return ensureSessionFor(AGENT_BASE);
}

export function ensureApiSession(): Promise<void> {
  return ensureSessionFor(BASE);
}

/** True when the agent-server is configured — the Build surface runs live. */
export function agentLive(): boolean {
  return AGENT_BASE.length > 0;
}

/** An agent-server WebSocket URL for `path` (e.g. a conversation stream), or null
 * when unconfigured (Build falls back to a fixture trace offline/in tests). */
export function agentWsUrl(path: string): string | null {
  if (!AGENT_BASE) return null;
  return `${wsBase(AGENT_BASE)}${path}`;
}

/** Origin-true preview URL (DC-01): http://{cid8}-{port}.localhost:8000/.
 * cid8 = first 8 chars of the uuid part. 127.0.0.1/localhost bases map to the
 * .localhost zone; any other base (future MagicDNS) gets the same {cid8}-{port}.
 * prefix on its hostname. Null when AGENT_BASE is unconfigured. */
export function previewHostUrl(cid: string, port: number, base: string = AGENT_BASE): string | null {
  if (!base) return null;
  const cid8 = cid.replace(/^conv_/, "").slice(0, 8);
  // Relative (same-origin) bases resolve against the page's own origin — the
  // front-door nginx forwards {cid8}-{port}.* Hosts to the agent-server.
  const url = new URL(base, globalThis.location?.origin ?? "http://localhost");
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
export async function agentGet<T>(path: string): Promise<T> {
  return agentFetch(path, { headers: { accept: "application/json" } }).then(parse<T>);
}

/** A REST call against the AGENT-server (loops/kill) — distinct from apiSend, which
 * targets the app-server (settings/library). */
export async function agentSend<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  return agentFetch(path, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(parse<T>);
}

export async function agentFetch(pathOrUrl: string, init: RequestInit = {}): Promise<Response> {
  return authFetch(AGENT_BASE, pathOrUrl, init);
}

export async function apiFetch(pathOrUrl: string, init: RequestInit = {}): Promise<Response> {
  return authFetch(BASE, pathOrUrl, init);
}

export async function previewBootstrapUrl(
  cid: string,
  port: number,
  targetPath = "/",
): Promise<string | null> {
  if (!agentLive()) return previewHostUrl(cid, port);
  const result = await agentSend<{ bootstrap_url: string }>(
    "POST",
    `/conversations/${encodeURIComponent(cid)}/preview/capability`,
    { port, target_path: targetPath },
  );
  return result.bootstrap_url;
}

/** Capability-gated selected-snapshot URL on an origin isolated from the app session.
 * Used for trusted, committed static sites so relative CSS/JS/media requests carry
 * only a preview cookie and can never inherit the owner's application session. */
export async function staticPreviewBootstrapUrl(
  cid: string,
  targetPath = "/",
): Promise<string | null> {
  if (!agentLive()) return null;
  const result = await agentSend<{ path_bootstrap_url: string }>(
    "POST",
    `/conversations/${encodeURIComponent(cid)}/preview/capability`,
    { port: 8000, target_path: targetPath },
  );
  return result.path_bootstrap_url;
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

export async function apiGet<T>(path: string): Promise<T> {
  return apiFetch(path, { headers: { accept: "application/json" } }).then(parse<T>);
}

export async function apiSend<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  return apiFetch(path, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(parse<T>);
}

async function ensureSessionFor(base: string): Promise<void> {
  if (!base) return;
  const existing = sessionInitByBase.get(base);
  if (existing) return existing;
  const pending = sessionInitChain
    .catch(() => undefined) // one base's failure must not wedge the other
    .then(() => initializeSession(base))
    .finally(() => {
      if (!csrfByBase.has(base)) sessionInitByBase.delete(base);
    });
  sessionInitByBase.set(base, pending);
  sessionInitChain = pending.catch(() => undefined);
  return pending;
}

/** Thrown when a base needs the operator's pairing token and none could be
 * auto-obtained (the containerized / remote self-host case: the browser is not
 * on the server's loopback, so the token-fetch convenience is refused). The
 * app-root <PairingGate> catches this and prompts for the token. */
export class PairingRequiredError extends Error {
  constructor(readonly base: string) {
    super("pairing required");
    this.name = "PairingRequiredError";
  }
}

function mintSession(base: string, token: string | null): Promise<Response> {
  return fetch(`${base}/api/auth/mint`, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(token ? { pairing_token: token } : {}),
  });
}

async function adoptMinted(base: string, minted: Response): Promise<void> {
  const body = (await minted.json()) as { csrf_token?: string };
  if (!body.csrf_token) throw new ApiError("auth mint missing csrf", minted.status);
  csrfByBase.set(base, body.csrf_token);
}

async function initializeSession(base: string): Promise<void> {
  const current = await fetch(`${base}/api/auth/session`, {
    credentials: "include",
    headers: { accept: "application/json" },
  });
  if (current.ok) {
    const body = await current.json().catch(() => null) as
      | { authenticated?: boolean; csrf_token?: string }
      | null;
    if (body?.authenticated && body.csrf_token) {
      csrfByBase.set(base, body.csrf_token);
      return;
    }
  }
  // Tokenless mint: succeeds when a shared cookie already exists, or on a
  // loopback dev host with auto-pair. A 401 means a pairing token is required.
  let minted = await mintSession(base, null);
  if (minted.status === 401) {
    // Loopback convenience: a SAME-HOST browser can read the token off the
    // server directly (the endpoint refuses remote clients). In a container the
    // browser is never loopback, so this simply fails over to the paste prompt.
    const pairing = await fetch(`${base}/api/auth/pairing-token`, {
      credentials: "include",
      headers: { accept: "application/json" },
    });
    if (pairing.ok) {
      const body = await pairing.json().catch(() => null) as
        | { pairing_token?: string }
        | null;
      if (body?.pairing_token) minted = await mintSession(base, body.pairing_token);
    }
  }
  // Still unauthorized → the operator must paste the token (from server logs).
  if (minted.status === 401) throw new PairingRequiredError(base);
  if (!minted.ok) throw new ApiError("auth mint failed", minted.status);
  await adoptMinted(base, minted);
}

/** Explicit auth bootstrap for the app-root <PairingGate>: ensure a session for
 * BOTH live bases. No-op in fixture/demo mode (no base configured). Propagates
 * PairingRequiredError so the gate can prompt for the token. */
export async function bootstrapSessions(): Promise<void> {
  if (isLive()) await ensureApiSession();
  if (agentLive()) await ensureAgentSession();
}

/** Pair with the operator's pasted token. Mints the FIRST live base with the
 * token — which sets the session cookie BOTH servers share (same host + signing
 * secret) — then lets every other base ADOPT that cookie via its own session
 * check. A second mint is deliberately avoided: it would replace the cookie the
 * first base's CSRF token is bound to (the documented shared-cookie race). */
export async function pairWithToken(token: string): Promise<void> {
  const bases = [BASE, AGENT_BASE].filter((b) => b.length > 0);
  if (bases.length === 0) return;
  const minted = await mintSession(bases[0], token);
  if (minted.status === 401) throw new PairingRequiredError(bases[0]);
  if (!minted.ok) throw new ApiError("pairing failed", minted.status);
  await adoptMinted(bases[0], minted);
  sessionInitByBase.set(bases[0], Promise.resolve());
  for (const base of bases.slice(1)) {
    sessionInitByBase.delete(base); // clear any failed init so it re-runs clean
    csrfByBase.delete(base);
    await ensureSessionFor(base); // adopts the shared cookie via /api/auth/session
  }
}

async function authFetch(base: string, pathOrUrl: string, init: RequestInit): Promise<Response> {
  await ensureSessionFor(base);
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  let csrfProtectedRead = false;
  if (method === "GET") {
    try {
      csrfProtectedRead = new URL(pathOrUrl, "http://disco.invalid").pathname.endsWith(
        "/api/storage/browse",
      );
    } catch {
      csrfProtectedRead = false;
    }
  }
  if (base && (["POST", "PUT", "PATCH", "DELETE"].includes(method) || csrfProtectedRead)) {
    const csrf = csrfByBase.get(base);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }
  // Already-based inputs pass through untouched: absolute URLs (split-origin
  // dev), and callers that build `agentHttpBase() + path` themselves (deck
  // export, project download/import) — with a RELATIVE base those would
  // otherwise get the prefix twice ("/svc/agent/svc/agent/…").
  const url =
    pathOrUrl.startsWith("http") || (base.length > 0 && pathOrUrl.startsWith(`${base}/`))
      ? pathOrUrl
      : `${base}${pathOrUrl}`;
  return fetch(url, { ...init, headers, credentials: "include" });
}

/** A small artificial delay for the fixture paths (keeps loading states visible). */
export function fixtureDelay(ms = 20): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
