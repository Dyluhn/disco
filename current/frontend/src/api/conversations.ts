import { CONVERSATIONS, CURRENT_OWNER } from "@/fixtures/conversations";
import type { ConversationSummary } from "@/types/conversation";
import {
  agentLive,
  agentSend,
  apiGet,
  apiSend,
  fixtureDelay,
  isLive,
} from "./client";

/**
 * Data-access for the conversation library (History). The list is OWNER-SCOPED:
 * the backend filters by owner and never returns another owner's conversations.
 * Live (VITE_API_BASE set) → the app-server filters by the authenticated
 * session owner; otherwise → the in-repo fixture. Components reach this only
 * through hooks.
 */

let fixtureStore: ConversationSummary[] = CONVERSATIONS.map((c) => ({ ...c }));

/** List the current owner's conversations, newest first. */
export async function listConversations(
  spaceId?: string | null,
): Promise<ConversationSummary[]> {
  if (isLive()) {
    const params = new URLSearchParams();
    if (spaceId !== undefined) params.set("space_id", spaceId ?? "");
    const rows = await apiGet<ConversationSummary[]>(`/api/conversations?${params}`);
    return rows.map((c) => ({ ...c, title: c.title ?? "(untitled)" }));
  }
  await fixtureDelay();
  return fixtureStore
    .filter((c) => c.owner_id === CURRENT_OWNER)
    .filter((c) =>
      spaceId === undefined
        ? true
        : spaceId === null
          ? !c.space_id
          : c.space_id === spaceId,
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((c) => ({ ...c }));
}

export async function setConversationSpace(
  id: string,
  spaceId: string | null,
): Promise<{ id: string; space_id: string | null }> {
  if (agentLive()) {
    await agentSend(
      "POST",
      `/api/conversations/${encodeURIComponent(id)}/space`,
      { space_id: spaceId },
    );
    return { id, space_id: spaceId };
  }
  if (isLive()) {
    await apiSend(
      "POST",
      `/api/conversations/${encodeURIComponent(id)}/space`,
      { space_id: spaceId },
    );
    return { id, space_id: spaceId };
  }
  await fixtureDelay();
  fixtureStore = fixtureStore.map((c) =>
    c.id === id && c.owner_id === CURRENT_OWNER ? { ...c, space_id: spaceId } : c,
  );
  return { id, space_id: spaceId };
}

/** Delete a conversation (destructive). Owner-scoped both client- and server-side. */
export async function deleteConversation(id: string): Promise<{ id: string }> {
  if (agentLive()) {
    // The runtime owner cancels and drains active work before deleting its data.
    await agentSend("DELETE", `/conversations/${encodeURIComponent(id)}`);
    return { id };
  }
  if (isLive()) {
    await apiSend<{ id: string; deleted: boolean }>(
      "DELETE",
      `/api/conversations/${encodeURIComponent(id)}`,
    );
    return { id };
  }
  await fixtureDelay();
  fixtureStore = fixtureStore.filter((c) => !(c.id === id && c.owner_id === CURRENT_OWNER));
  return { id };
}
