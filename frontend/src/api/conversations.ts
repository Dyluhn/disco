import { CONVERSATIONS, CURRENT_OWNER } from "@/fixtures/conversations";
import type { ConversationSummary } from "@/types/conversation";

/**
 * Data-access for the conversation library (History, Prompt 5). The list is
 * OWNER-SCOPED: the backend filters by the authenticated owner and never returns
 * another owner's conversations; the fixture enforces the same here. Components
 * reach this only through hooks. Session-scoped in-memory store (no browser
 * storage); swap for the live list/delete endpoints when wired.
 */

const delay = () => new Promise((r) => setTimeout(r, 20));

let store: ConversationSummary[] = CONVERSATIONS.map((c) => ({ ...c }));

/** List the CURRENT owner's conversations, newest first. */
export async function listConversations(): Promise<ConversationSummary[]> {
  await delay();
  return store
    .filter((c) => c.owner_id === CURRENT_OWNER)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((c) => ({ ...c }));
}

/** Delete a conversation (destructive). Owner-scoped: only the current owner's
 *  conversations are removable here, mirroring the backend's authorization. */
export async function deleteConversation(id: string): Promise<{ id: string }> {
  await delay();
  store = store.filter((c) => !(c.id === id && c.owner_id === CURRENT_OWNER));
  return { id };
}
