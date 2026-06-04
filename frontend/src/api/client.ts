/**
 * The HTTP client for the data-access layer (the ONE place that talks to the
 * backend; components never import this — only the api modules + hooks do).
 *
 * Live vs fixtures: when `VITE_API_BASE` is set, the api modules call the real
 * endpoints (see api-endpoints.md); when it's unset they replay in-repo fixtures
 * — the "build against fixtures first, then wire live" discipline. Tests run with
 * no base URL, so they exercise the fixtures (no network).
 */

const BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/+$/, "");

/** The owner whose conversations we read/write (no auth in v1; an explicit id). */
export const OWNER_ID = import.meta.env.VITE_OWNER_ID ?? "local";

/** True when a backend base URL is configured — the api modules call it live. */
export function isLive(): boolean {
  return BASE.length > 0;
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
  return (await res.json()) as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return fetch(`${BASE}${path}`, { headers: { accept: "application/json" } }).then(parse<T>);
}

export function apiSend<T>(
  method: "POST" | "PUT" | "DELETE",
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
