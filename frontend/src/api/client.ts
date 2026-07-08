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

/** The agent-server research WebSocket URL (ws://… derived from VITE_AGENT_BASE),
 * or null when unconfigured — callers fall back to the fixture stream. */
export function researchWsUrl(): string | null {
  if (!AGENT_BASE) return null;
  return `${AGENT_BASE.replace(/^http/, "ws")}/ws/research`;
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

type AuthFailureBody = {
  reason?: string;
  detail?: string | { reason?: string };
};

let activePairingPrompt: Promise<string | null> | null = null;

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

async function authFailureReason(response: Response): Promise<string | undefined> {
  const body = await response.clone().json().catch(() => null) as AuthFailureBody | null;
  if (!body || typeof body !== "object") return undefined;
  if (typeof body.reason === "string") return body.reason;
  const detail = body.detail;
  if (detail && typeof detail === "object" && typeof detail.reason === "string") {
    return detail.reason;
  }
  return undefined;
}

function mintAuthSession(base: string, pairingToken?: string): Promise<Response> {
  return fetch(`${base}/api/auth/mint`, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: pairingToken ? JSON.stringify({ pairing_token: pairingToken }) : "{}",
  });
}

function requestPairingToken(): Promise<string | null> {
  if (activePairingPrompt) return activePairingPrompt;
  activePairingPrompt = renderPairingPrompt().finally(() => {
    activePairingPrompt = null;
  });
  return activePairingPrompt;
}

function renderPairingPrompt(): Promise<string | null> {
  const doc = globalThis.document;
  if (!doc?.body) {
    const token = globalThis.prompt?.(
      "Enter the one-time pairing token from the server boot banner.",
    );
    return Promise.resolve(token?.trim() || null);
  }

  return new Promise((resolve) => {
    const priorFocus = doc.activeElement instanceof HTMLElement ? doc.activeElement : null;
    const overlay = doc.createElement("div");
    overlay.setAttribute("data-disco-pairing-prompt", "true");
    overlay.className = "fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-body";

    const panel = doc.createElement("div");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-labelledby", "disco-pairing-title");
    panel.className =
      "flex w-[min(32rem,92vw)] flex-col rounded-card border border-hairline bg-bg p-body pmx-rise";

    const title = doc.createElement("h2");
    title.id = "disco-pairing-title";
    title.className = "font-ui text-[0.98rem] font-semibold text-text";
    title.textContent = "Pair this browser";

    const description = doc.createElement("p");
    description.className = "mt-hair font-ui text-[0.8rem] text-text-muted";
    description.textContent =
      "Enter the one-time pairing token shown in the server boot banner.";

    const form = doc.createElement("form");
    form.className = "mt-body flex flex-col gap-inline";

    const label = doc.createElement("label");
    label.className = "font-ui text-[0.75rem] font-medium text-text-muted";
    label.setAttribute("for", "disco-pairing-token");
    label.textContent = "One-time pairing token";

    const input = doc.createElement("input");
    input.id = "disco-pairing-token";
    input.name = "pairing_token";
    input.autocomplete = "one-time-code";
    input.className =
      "h-10 rounded-control border border-hairline bg-bg-elevated px-inline font-mono text-[0.85rem] text-text outline-none transition-colors focus:border-accent";

    const actions = doc.createElement("div");
    actions.className = "mt-inline flex justify-end gap-inline";

    const cancel = doc.createElement("button");
    cancel.type = "button";
    cancel.className =
      "h-9 rounded-control border border-hairline px-inline font-ui text-[0.8rem] text-text-muted transition-colors hover:text-text";
    cancel.textContent = "Cancel";

    const submit = doc.createElement("button");
    submit.type = "submit";
    submit.className =
      "h-9 rounded-control bg-accent px-inline font-ui text-[0.8rem] font-medium text-accent-foreground transition-opacity hover:opacity-90";
    submit.textContent = "Pair";

    const cleanup = (token: string | null) => {
      overlay.remove();
      priorFocus?.focus();
      resolve(token);
    };

    cancel.addEventListener("click", () => cleanup(null));
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "Escape") cleanup(null);
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const token = input.value.trim();
      if (!token) {
        input.focus();
        return;
      }
      cleanup(token);
    });

    actions.append(cancel, submit);
    form.append(label, input, actions);
    panel.append(title, description, form);
    overlay.append(panel);
    doc.body.appendChild(overlay);
    queueMicrotask(() => input.focus());
  });
}

async function mintWithEnteredPairingToken(base: string): Promise<Response | null> {
  const token = await requestPairingToken();
  if (!token) return null;
  return mintAuthSession(base, token);
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
  let minted = await mintAuthSession(base);
  if (!minted.ok && minted.status === 401) {
    const pairing = await fetch(`${base}/api/auth/pairing-token`, {
      credentials: "include",
      headers: { accept: "application/json" },
    });
    let fetchedLoopbackToken = false;
    if (pairing.ok) {
      const body = await pairing.json().catch(() => null) as
        | { pairing_token?: string }
        | null;
      if (body?.pairing_token) {
        fetchedLoopbackToken = true;
        minted = await mintAuthSession(base, body.pairing_token);
      }
    }
    if (!fetchedLoopbackToken && !minted.ok) {
      minted = (await mintWithEnteredPairingToken(base)) ?? minted;
    }
  } else if (
    !minted.ok
    && minted.status === 403
    && (await authFailureReason(minted)) === "loopback_required"
  ) {
    minted = (await mintWithEnteredPairingToken(base)) ?? minted;
  }
  if (!minted.ok) throw new ApiError("auth mint failed", minted.status);
  const body = await minted.json() as { csrf_token?: string };
  if (!body.csrf_token) throw new ApiError("auth mint missing csrf", minted.status);
  csrfByBase.set(base, body.csrf_token);
}

async function authFetch(base: string, pathOrUrl: string, init: RequestInit): Promise<Response> {
  await ensureSessionFor(base);
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (base && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    const csrf = csrfByBase.get(base);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }
  const url = pathOrUrl.startsWith("http") ? pathOrUrl : `${base}${pathOrUrl}`;
  return fetch(url, { ...init, headers, credentials: "include" });
}

/** A small artificial delay for the fixture paths (keeps loading states visible). */
export function fixtureDelay(ms = 20): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
