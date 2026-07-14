export interface ThrashEvent {
  kind?: string;
  error?: unknown;
  detail?: unknown;
}

export interface AgentErrorThrashAssessment {
  total: number;
  repeated: Array<{ error: string; count: number }>;
  violations: string[];
}

const WORKSPACE_PATH = /\/tmp\/(?:disco|pmx)-sbx-[^/\s"'`]+\/sbx_[^/\s"'`]+/gi;
const VOLATILE_ID = /\b(?:evt|act|call|sbx)_[a-z0-9_-]+\b/gi;

/** Collapse only volatile identifiers that can make the same execution failure look
 * different across attempts.  Preserve ports, tool names, and substantive text so
 * genuinely different failures are never merged merely to satisfy the oracle. */
export function normalizeAgentError(error: unknown): string {
  return String(error ?? "")
    .replace(WORKSPACE_PATH, "<workspace>")
    .replace(VOLATILE_ID, "<id>")
    .replace(/\s+/g, " ")
    .trim();
}

/** Strict standalone-agent-error boundary for live reliability trials.
 *
 * A second semantically identical execution error is already retry thrash.  Distinct
 * errors are capped at five so argument/session variation cannot evade the gate.
 */
export function assessAgentErrorThrash(
  events: readonly ThrashEvent[],
): AgentErrorThrashAssessment {
  const counts = new Map<string, number>();
  let total = 0;
  for (const event of events) {
    if (event.kind !== "agent_error") continue;
    total += 1;
    const normalized = normalizeAgentError(event.error ?? event.detail ?? "<missing error>");
    counts.set(normalized, (counts.get(normalized) ?? 0) + 1);
  }

  const repeated = [...counts.entries()]
    .filter(([, count]) => count > 1)
    .map(([error, count]) => ({ error, count }))
    .sort((a, b) => b.count - a.count || a.error.localeCompare(b.error));
  const violations: string[] = [];
  if (total > 5) violations.push(`agent_error total ${total} exceeds 5`);
  for (const item of repeated) {
    violations.push(`agent_error repeated ${item.count} times: ${item.error}`);
  }
  return { total, repeated, violations };
}
