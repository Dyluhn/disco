import type { ActivityFeed } from "@/types/activity";
import { agentGet, agentLive, fixtureDelay } from "./client";

/** The background-task dashboard feed (agent-server, GET /api/activity). Offline →
 * a small fixture so the view + the NavRail count render in development/tests/screenshots. */
const fixtureActivity: ActivityFeed = {
  running: [
    {
      id: "fixture-run-1",
      title: "Draft the Q3 competitive brief",
      status: "RUNNING",
      surface: "agent",
      created_at: "2026-06-12T05:40:00Z",
    },
  ],
  recent_runs: [
    {
      run_id: "fr-1",
      schedule_id: "fs-1",
      conversation_id: "fixture-conv-1",
      fired_at: "2026-06-12T09:00:00Z",
      coalesced: 0,
      description: "Morning market digest",
      title: "Market digest",
    },
    {
      run_id: "fr-2",
      schedule_id: "fs-1",
      conversation_id: "fixture-conv-1",
      fired_at: "2026-06-11T09:00:00Z",
      coalesced: 1,
      description: "Morning market digest",
      title: "Market digest",
    },
  ],
  counts: { running: 1 },
};

export async function getActivity(): Promise<ActivityFeed> {
  if (agentLive()) return agentGet<ActivityFeed>("/api/activity");
  await fixtureDelay();
  return { ...fixtureActivity };
}
