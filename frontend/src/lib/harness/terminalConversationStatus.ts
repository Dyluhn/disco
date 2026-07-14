export interface ConversationStatusEvent {
  seq?: number;
  kind?: string;
  status?: string;
  detail?: unknown;
}

const STOPPED_WITHOUT_FINISH = new Set(["ERROR", "STUCK", "AWAITING_USER_DECISION"]);

/** Return the latest stopped, non-finished status for the current run segment.
 *
 * AWAITING_USER_DECISION is resumable, so historical occurrences are not enough:
 * a later RUNNING/FINISHED status means the user already resumed the run.  Looking
 * only at the latest status also makes live helpers report the actual stop reason
 * immediately instead of burning their entire completion timeout.
 */
export function latestStoppedStatus<T extends ConversationStatusEvent>(
  events: readonly T[],
  afterSeq = 0,
): T | undefined {
  const latestStatus = [...events]
    .reverse()
    .find((event) => (event.seq ?? 0) > afterSeq && event.kind === "status");
  return latestStatus && STOPPED_WITHOUT_FINISH.has(String(latestStatus.status))
    ? latestStatus
    : undefined;
}
