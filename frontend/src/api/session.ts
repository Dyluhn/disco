/**
 * Session bootstrap and pairing, for the shell components that gate on it
 * (Amendment A3).
 *
 * `<PairingGate>` and `<DemoDataBadge>` are app-root shell components: one
 * decides whether the UI can reach a backend at all, the other tells the user
 * it cannot. Both were reaching into `./client` directly. Session lifecycle is
 * an api-layer concern, so it is declared here and the view layer asks this
 * module instead of the transport module.
 *
 * `isPairingRequired` narrows structurally rather than exposing
 * `PairingRequiredError`, so callers need neither a value nor a type import
 * from `./client` — the same reasoning as `./errors`.
 */

import {
  PairingRequiredError,
  bootstrapSessions,
  isDemoMode,
  pairWithToken,
} from "./client";

/** True when NEITHER backend is configured, so the UI is running entirely on
 *  fixture data and the shell should say so rather than imply it is live. */
export function isDemoSession(): boolean {
  return isDemoMode();
}

/** Establish sessions against whichever backends are configured. */
export async function bootstrapSession(): Promise<void> {
  return bootstrapSessions();
}

/** Complete pairing with a token the user pasted from the server's console. */
export async function pairSessionWithToken(token: string): Promise<void> {
  return pairWithToken(token);
}

/** True when bootstrap failed because the backend demands an explicit pairing
 *  token (the request did not originate on the server's loopback). The gate
 *  prompts for a token when this is true. */
export function isPairingRequired(error: unknown): error is Error {
  return error instanceof PairingRequiredError;
}
