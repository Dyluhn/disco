/** A past conversation, as the History library lists it. Ownership-scoped — the
 *  backend's list call filters by the authenticated owner; the UI never sees
 *  another owner's conversations. */
export interface ConversationSummary {
  id: string;
  owner_id: string;
  /** the conversation's title / first question */
  title: string;
  /** ISO-8601 creation timestamp */
  created_at: string;
  /** which surface produced it — History routes by this (read-only open). */
  surface?: "research" | "build" | "deep_research";
}
