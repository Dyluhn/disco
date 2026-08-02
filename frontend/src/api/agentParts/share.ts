/**
 * Share-link (RP-06) API: create a revocable share token, fetch the redacted
 * versioned bundle for the static viewer, import a bundle as a read-only
 * conversation, and fetch a finished run's log for static (read-only) rendering.
 *
 * Extracted from api/agent.ts (module-size decomposition, PKG-12-FE-BUILD/
 * TS-0002); re-exported from api/agent so the public import path is unchanged.
 */

import { agentGet, agentLive, agentSend } from "../client";
import type { ShareBundle, ShareLink } from "../agent";


/** Create a revocable share token for a conversation. Returns the token + the
 *  URL slug (`/share/<token>`) the frontend can render as a link.
 *  Double-issuing the same conversation is idempotent. */
export async function createShare(conversationId: string): Promise<ShareLink> {
  if (!agentLive()) {
    return {
      ok: false,
      reason: "Share links require the agent server (offline here).",
      token: "",
      conversation_id: conversationId,
    };
  }
  return agentSend<ShareLink>("POST", `/conversations/${conversationId}/share`);
}

/** The redacted, versioned JSON bundle for a shared conversation. The static
 *  viewer (ShareView) consumes this — zero WebSocket dependency. */

/** Fetch the scrubbed bundle for a share token. The endpoint resolves the token,
 *  re-exports the event log, and returns the versioned bundle. Returns null when
 *  the token is invalid or revoked (the two are intentionally conflated — a probe
 *  can't distinguish a revoked link from one that never existed). */
export async function fetchShareBundle(token: string): Promise<ShareBundle | null> {
  if (!agentLive()) return null;
  try {
    const bundle = await agentGet<ShareBundle>(`/api/share/${encodeURIComponent(token)}/bundle`);
    return bundle;
  } catch {
    // HTTP 404 or 403 — token invalid or revoked
    return null;
  }
}

/** Import an exported bundle as a READ-ONLY local conversation. The server
 *  validates + re-scrubs + mints a fresh cid + marks it imported; returns the new
 *  cid. Throws ApiError (422) on an invalid/unsupported bundle. */
export async function importShareBundle(bundle: unknown): Promise<{ conversation_id: string }> {
  return agentSend<{ conversation_id: string }>("POST", "/api/share/import", bundle);
}

/** Fetch a finished conversation's event log + final status for a static (read-only)
 *  render — used by ImportedRunView. No WebSocket: the log is already complete. */
export async function fetchConversationRun(
  cid: string,
): Promise<{ events: Array<Record<string, unknown>>; status: string }> {
  const ev = await agentGet<{ events: Array<Record<string, unknown>> }>(
    `/conversations/${encodeURIComponent(cid)}/events`,
  );
  const state = await agentGet<{ execution_status?: string }>(
    `/conversations/${encodeURIComponent(cid)}/state`,
  );
  return { events: ev.events ?? [], status: state.execution_status ?? "FINISHED" };
}
