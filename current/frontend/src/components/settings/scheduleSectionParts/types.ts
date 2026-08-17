/**
 * Wire types for ScheduleSection — relocated out of the parent (still private,
 * non-exported implementation detail) so the parts components can share them.
 */

export interface ScheduleRow {
  schedule_id: string;
  conversation_id: string;
  rrule: string;
  description: string;
  timezone: string;
  depth: string | null;
  model_override: string | null;
  created_at: string;
  enabled: boolean;
  next_run: string | null;
}

export interface PreviewResult {
  next_runs: string[];
  rrule: string;
  timezone: string;
}
