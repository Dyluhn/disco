import { CONVERSATIONS, CURRENT_OWNER } from "@/fixtures/conversations";
import type { ConversationSummary } from "@/types/conversation";
import { apiGet, apiSend, fixtureDelay, isLive, OWNER_ID } from "./client";

/**
 * Data-access for the conversation library (History). The list is OWNER-SCOPED:
 * the backend filters by owner and never returns another owner's conversations.
 * Live (VITE_API_BASE set) → the app-server GET /api/conversations?owner_id= and
 * DELETE /api/conversations/{id}?owner_id= (the delete is owner-scoped server-
 * side too); otherwise → the in-repo fixture. Components reach this only through
 * hooks.
 */

let fixtureStore: ConversationSummary[] = CONVERSATIONS.map((c) => ({ ...c }));

/** List the current owner's conversations, newest first. */
export async function listConversations(): Promise<ConversationSummary[]> {
  if (isLive()) {
    const owner = encodeURIComponent(OWNER_ID);
    const rows = await apiGet<ConversationSummary[]>(`/api/conversations?owner_id=${owner}`);
    return rows.map((c) => ({ ...c, title: c.title ?? "(untitled)" }));
  }
  await fixtureDelay();
  return fixtureStore
    .filter((c) => c.owner_id === CURRENT_OWNER)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((c) => ({ ...c }));
}

/** Delete a conversation (destructive). Owner-scoped both client- and server-side. */
export async function deleteConversation(id: string): Promise<{ id: string }> {
  if (isLive()) {
    const owner = encodeURIComponent(OWNER_ID);
    await apiSend<{ id: string; deleted: boolean }>(
      "DELETE",
      `/api/conversations/${encodeURIComponent(id)}?owner_id=${owner}`,
    );
    return { id };
  }
  await fixtureDelay();
  fixtureStore = fixtureStore.filter((c) => !(c.id === id && c.owner_id === CURRENT_OWNER));
  return { id };
}
