export interface ThrashEvent {
  id?: string;
  kind?: string;
  error?: unknown;
  detail?: unknown;
  action_id?: string;
  tool_call?: {
    tool_name?: unknown;
    arguments?: unknown;
  };
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

function stableValue(value: unknown): unknown {
  if (typeof value === "string") return normalizeAgentError(value);
  if (Array.isArray(value)) return value.map(stableValue);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableValue(item)]),
    );
  }
  return value;
}

function actionFingerprints(events: readonly ThrashEvent[]): Map<string, string> {
  const actions = new Map<string, string>();
  for (const event of events) {
    if (event.kind !== "action" || !event.id || !event.tool_call?.tool_name) continue;
    actions.set(
      event.id,
      `${String(event.tool_call.tool_name)}:${JSON.stringify(stableValue(event.tool_call.arguments ?? {}))}`,
    );
  }
  return actions;
}

/** Strict standalone-agent-error boundary for live reliability trials.
 *
 * A second semantically identical execution error is already retry thrash.  Distinct
 * errors are capped at five so argument/session variation cannot evade the gate.
 */
export function assessAgentErrorThrash(
  events: readonly ThrashEvent[],
): AgentErrorThrashAssessment {
  const actions = actionFingerprints(events);
  const counts = new Map<string, { error: string; count: number }>();
  let total = 0;
  for (const event of events) {
    if (event.kind !== "agent_error") continue;
    total += 1;
    const normalized = normalizeAgentError(event.error ?? event.detail ?? "<missing error>");
    // Shell and other execution tools intentionally use compact canonical error
    // codes. Correlate them to the paired action so two different commands that
    // both exit 1 are not mislabeled as the same retry. Missing correlation stays
    // fail-closed: unpaired equal errors still share one bucket.
    const fingerprint = event.action_id ? actions.get(event.action_id) : undefined;
    const key = `${normalized}\u0000${fingerprint ?? "<uncorrelated>"}`;
    const current = counts.get(key);
    counts.set(key, { error: normalized, count: (current?.count ?? 0) + 1 });
  }

  const repeated = [...counts.values()]
    .filter(({ count }) => count > 1)
    .map(({ error, count }) => ({ error, count }))
    .sort((a, b) => b.count - a.count || a.error.localeCompare(b.error));
  const violations: string[] = [];
  if (total > 5) violations.push(`agent_error total ${total} exceeds 5`);
  for (const item of repeated) {
    violations.push(`agent_error repeated ${item.count} times: ${item.error}`);
  }
  return { total, repeated, violations };
}
