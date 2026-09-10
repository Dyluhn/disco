/**
 * Deck-export transport for `DeckExportBar` (Amendment A3). The bar built the
 * `/deck/export` URL and called `agentFetch`/`agentLive` directly against
 * `@/api/client`; that put the ONE transport module in the import graph of the
 * view layer. This module is a relocation of that transport, not a redesign of
 * it — same URL shape, same method, same headers, same error handling stays in
 * the caller.
 */

import { agentFetch, agentHttpBase } from "@/api/client";

/** The render-on-demand deck export URL: the jailed `base` path re-rendered
 *  with `template` and produced as `fmt`. Byte-identical to the URL
 *  `DeckExportBar` used to build inline off `agentHttpBase()`. */
export function deckExportUrl(
  conversationId: string,
  base: string,
  template: string,
  fmt: string,
): string {
  return (
    `${agentHttpBase()}/conversations/${conversationId}/deck/export` +
    `?path=${encodeURIComponent(base)}` +
    `&template=${encodeURIComponent(template)}&fmt=${fmt}`
  );
}

/** Fetch a deck export at `url` (from `deckExportUrl`). Kept as a thin
 *  passthrough so the caller's existing response handling (ok/blob/json error
 *  parsing) is unchanged. */
export async function fetchDeckExport(url: string): Promise<Response> {
  return agentFetch(url, { headers: { accept: "*/*" } });
}

/** Fetch a conversation's raw state (the export bar reads `sandbox_backend`
 *  off it to gate the PDF affordance). Returns the raw `Response` so the
 *  caller's existing `.then((r) => r.json())` chain is unchanged. */
export async function fetchConversationState(conversationId: string): Promise<Response> {
  return agentFetch(`/conversations/${conversationId}/state`);
}
