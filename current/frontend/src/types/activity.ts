/** The background-task dashboard feed — the wire mirror of the agent-server's
 * GET /api/activity. `running` is what's executing RIGHT NOW (the runtime's live
 * tasks, not the cached status); `recent_runs` is the scheduled-run audit history;
 * `counts.running` drives the global "N tasks running" indicator. */

export interface RunningTask {
  id: string; // = conversation_id
  title: string;
  status?: string; // cached ConversationStatus
  surface?: "research" | "build" | "agent" | "deep_research";
  created_at?: string;
}

export interface ScheduleRunRecord {
  run_id: string;
  schedule_id: string;
  conversation_id: string;
  fired_at: string; // ISO-8601
  coalesced: number; // 0 | 1 — 1 when N missed fires were coalesced into this catch-up
  description?: string; // the schedule's description (joined)
  title?: string | null; // the conversation's title (joined)
}

export interface ActivityFeed {
  running: RunningTask[];
  recent_runs: ScheduleRunRecord[];
  counts: { running: number };
}
