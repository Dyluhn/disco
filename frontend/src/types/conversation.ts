/** A past conversation, as the History library lists it. Ownership-scoped — the
 *  backend's list call filters by the authenticated owner; the UI never sees
 *  another owner's conversations. */
export interface ConversationSummary {
  id: string;
  owner_id: string;
  /** Space folder membership; null/undefined means Unfiled. */
  space_id?: string | null;
  /** the conversation's title / first question */
  title: string;
  /** the conversation's current status (cached library projection) */
  status?: string;
  /** ISO-8601 creation timestamp */
  created_at: string;
  /** which surface produced it — History routes by this (read-only open).
   *  "agent" is the general-task framing of "build" (same machinery). */
  surface?: "research" | "build" | "agent" | "deep_research";
  /** "imported" for a read-only share-bundle import (routes to /imported/:cid and
   *  refuses every mutating action at the server edge); undefined for first-party. */
  origin?: "imported" | null;
}
