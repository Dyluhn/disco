import type { ConversationSummary } from "@/types/conversation";

/** The authenticated owner (the backend derives this from auth; here a constant
 *  so the owner-scoping is demonstrable). */
export const CURRENT_OWNER = "owner-me";

/**
 * Fixture conversations. Includes one belonging to a DIFFERENT owner so the
 * owner-scoped list call has something to (correctly) filter out — the UI must
 * never surface cross-owner conversations.
 */
export const CONVERSATIONS: ConversationSummary[] = [
  {
    id: "c1",
    owner_id: CURRENT_OWNER,
    title: "How does reciprocal rank fusion work, and when should I use it?",
    created_at: "2026-05-29T14:12:00Z",
  },
  {
    id: "c2",
    owner_id: CURRENT_OWNER,
    title: "Practical differences between the MIT and Apache 2.0 licenses",
    created_at: "2026-05-28T09:40:00Z",
  },
  {
    id: "c3",
    owner_id: CURRENT_OWNER,
    title: "Current evidence on intermittent fasting and metabolic health",
    created_at: "2026-05-25T19:05:00Z",
  },
  {
    // An imported share bundle — read-only, routes to /imported/:cid, badged.
    id: "c-imported",
    owner_id: CURRENT_OWNER,
    title: "Imported: a colleague's landing-page build",
    created_at: "2026-05-31T11:30:00Z",
    status: "FINISHED",
    surface: "build",
    origin: "imported",
  },
  {
    // Belongs to someone else — must NOT appear in the current owner's list.
    id: "c-other",
    owner_id: "owner-someone-else",
    title: "A conversation owned by a different user",
    created_at: "2026-05-30T08:00:00Z",
  },
];
