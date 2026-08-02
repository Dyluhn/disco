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
