/**
 * Plan mode — the proposed plan + agent-reported per-step progress. Split out
 * of `buildTrace.ts`. `deriveBuildProgress` (the declarative snapshot-based
 * progress deriver) lives in `./buildProgress` — a distinct concern (full-state
 * rewrite vs. incremental plan_step reports) with its own mccabe row, kept in
 * its own capped module rather than folded in here.
 */
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import type { PlanView, StepState } from "../buildTrace";

// The legacy backend inserted this sentinel step when the model submitted a plan with
// a summary but no real steps; it rendered as a broken "1. (the planner…)" numbered
// step. The backend no longer emits it (it keeps `steps` empty now), but historical
// events still carry it — drop it defensively so old plans render the clean no-steps
// state. MUST stay byte-identical to the former backend constant in plans.py.
const LEGACY_NO_STEPS_SENTINEL = "(the planner returned no concrete steps)";

/** The latest proposed plan (highest revision wins — a re-plan supersedes the prior
 * one). Returns null before any plan exists. */
export function derivePlan(events: AgentEvent[]): PlanView | null {
  let latest: PlanView | null = null;
  for (const e of events) {
    if (e.kind !== "plan") continue;
    if (latest === null || e.revision >= latest.revision) {
      latest = {
        id: e.id,
        summary: e.summary,
        // Drop the legacy no-steps placeholder so a summary-only plan renders cleanly
        // instead of showing a broken numbered "1. (the planner…)" step.
        steps: e.steps.filter((s) => s.title !== LEGACY_NO_STEPS_SENTINEL),
        revision: e.revision,
        context: e.context ?? "",
      };
    }
  }
  return latest;
}

/** Per-step progress (1-based index → state), derived from the agent's `plan_step`
 * reports in the stream. Honest by construction: a step the agent never reported
 * stays "pending" — progress is agent-driven, never inferred from action counts.
 *
 * Status-aware: when the conversation reaches a terminal-without-completion state
 * (FINISHED/STUCK/ERROR — anything that means "the loop stopped before this step
 * could be marked done"), any lingering "active" step is rewritten to "stalled"
 * so the UI doesn't lie with a spinning blue icon on work that isn't progressing. */
export function derivePlanProgress(
  events: AgentEvent[],
  status?: ConversationStatus,
): Map<number, StepState> {
  const progress = new Map<number, StepState>();
  // Only count plan_step marks AFTER the latest plan — a re-plan starts a fresh
  // checklist, so the prior plan's "done" marks must not show on the new one
  // (else every step looks done after a re-plan).
  let latestPlanIdx = -1;
  let latestRev = -1;
  events.forEach((e, i) => {
    if (e.kind === "plan" && e.revision >= latestRev) {
      latestRev = e.revision;
      latestPlanIdx = i;
    }
  });
  events.forEach((e, i) => {
    if (i < latestPlanIdx) return; // belongs to a superseded plan
    if (e.kind !== "action" || !e.tool_call || e.tool_call.tool_name !== "plan_step") return;
    const idx = Number(e.tool_call.arguments.index);
    const state = String(e.tool_call.arguments.state);
    if (!Number.isFinite(idx)) return;
    if (state === "done") progress.set(idx, "done");
    else if (state === "active" && progress.get(idx) !== "done") progress.set(idx, "active");
  });
  // Stalled-step reconciliation: in terminal states, an "active" marker means
  // the agent started a step and the loop stopped before it finished. Show that
  // honestly instead of pretending it's still working.
  const terminalStopped =
    status === "FINISHED" || status === "STUCK" || status === "ERROR" || status === "IDLE";
  if (terminalStopped) {
    for (const [idx, st] of progress) {
      if (st === "active") progress.set(idx, "stalled");
    }
  }
  return progress;
}

/** Cluster 6: aggregate plan progress for a glanceable "N of M" + bar. Built
 * from the same progress Map the PlanPanel already has. */
export function planProgressSummary(
  totalSteps: number,
  progress: Map<number, StepState>,
): { done: number; total: number; fraction: number } {
  let done = 0;
  for (const st of progress.values()) if (st === "done") done += 1;
  const total = Math.max(totalSteps, 0);
  return { done, total, fraction: total > 0 ? done / total : 0 };
}
