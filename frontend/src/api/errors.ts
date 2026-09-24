/**
 * The api layer's error classification seam (Amendment A3).
 *
 * `ApiError` is declared in `./client`, which components and views must not
 * import — `client.ts` is the ONE place that talks to the backend, and the
 * `no-restricted-imports` boundary rule holds that line. But a component that
 * renders a failure legitimately needs to ask "was this a transport failure,
 * and what did the server say?". That question belongs to the api layer, so
 * the answer is declared here rather than by handing every component the
 * transport module just to run an `instanceof`.
 *
 * These are DECLARED functions, not a re-export of `ApiError`: the campaign's
 * public-API authority records `export … from "…"` as a re-export rather than
 * a declaration, and declaring keeps the surface honest. Narrowing to the
 * structural `ApiFailure` (rather than to the class) is what lets callers stay
 * off `client.ts` entirely — no value import and no type import.
 */

import { ApiError } from "./client";

/** The part of a transport failure a view may read: the server's message and
 *  its HTTP status. Structural on purpose — see the module note. */
export interface ApiFailure {
  readonly message: string;
  readonly status: number;
}

/** True when `error` came from the backend rather than from client code.
 *  Replaces `error instanceof ApiError` at every call site outside `src/api/`. */
export function isApiFailure(error: unknown): error is ApiFailure {
  return error instanceof ApiError;
}

/** The server's message for a transport failure, or `null` for anything else —
 *  the common shape of `{cond ? err.message : "fallback"}` in a view. */
export function apiFailureMessage(error: unknown): string | null {
  return isApiFailure(error) ? error.message : null;
}

/** Render backend failures without exposing an entire JSON response in a form. */
export function readableRequestError(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  if (error instanceof SyntaxError) return "This file is not valid JSON. Choose an exported run bundle.";
  let payload: unknown;
  try { payload = JSON.parse(error.message); } catch { return error.message || fallback; }
  if (!payload || typeof payload !== "object") return fallback;
  const outer = payload as Record<string, unknown>;
  const detail = outer.detail ?? outer;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return validationMessages(detail) || fallback;
  if (!detail || typeof detail !== "object") return fallback;
  const row = detail as Record<string, unknown>;
  for (const key of ["message", "detail"]) if (typeof row[key] === "string") return row[key];
  const reason = typeof row.reason === "string" ? row.reason : "";
  const reasons: Record<string, string> = {
    unsupported_bundle_version: "This run bundle version is not supported. Export it again with a compatible Disco release.",
    bundle_not_an_object: "This file is not a run bundle. Choose the JSON file from Export run.",
    bundle_has_no_events: "This run bundle has no conversation events to import.",
    bundle_too_large: "This run bundle is too large to import.",
    malformed_event: "This run bundle contains an invalid event. No conversation was imported.",
    outside_allowed_roots: "Choose a folder inside the configured project storage directory.",
    request_policy_denied: "This request is blocked by the configured access policy. Review the destination and runtime settings.",
  };
  return reasons[reason] ?? (reason ? `${fallback} (${reason.replaceAll("_", " ")})` : fallback);
}

function validationMessages(detail: unknown[]): string {
  return detail.flatMap((item: unknown) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    return typeof row.msg === "string" ? [row.msg] : [];
  }).slice(0, 3).join("; ");
}
