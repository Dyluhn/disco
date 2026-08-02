/**
 * runthru-v2 (#3): Build progress for CAPABLE models — derived from the LATEST
 * declarative `update_plan_progress` snapshot (a full-state rewrite, not a
 * delta). Split out of `buildTrace.ts`; the terminal-state reconciliation tail
 * (FINISHED → all-done, STUCK/ERROR/IDLE → stalled) is its own helper so this
 * stays under the callable complexity cap.
 */
import type { ActionEvent, AgentEvent, ConversationStatus } from "@/types/agent";
import type { StepState } from "../buildTrace";

/** Terminal reconciliation:
 *  • FINISHED — the build passed the real finish gate, so the deliverable is
 *    complete: EVERY plan step reads done. This also stops a stale early snapshot
 *    from showing e.g. "2/3" (with no Done signal) after the build actually
 *    finished — the model isn't required to send a final 100%-done snapshot.
 *  • STUCK/ERROR/IDLE (stopped without completing) → "active" reads as stalled. */
function reconcileBuildProgress(
  progress: Map<number, StepState>,
  status: ConversationStatus | undefined,
  nSteps: number,
): void {
  if (status === "FINISHED" && nSteps > 0) {
    for (let i = 1; i <= nSteps; i++) progress.set(i, "done");
  } else if (status === "STUCK" || status === "ERROR" || status === "IDLE") {
    for (const [idx, st] of progress) {
      if (st === "active") progress.set(idx, "stalled");
    }
  }
}

/** Because the latest snapshot carries the COMPLETE state of every step, it is
 * self-correcting: a dropped/garbled prior update can't leave a step wrong, the
 * next snapshot re-establishes truth. Returns an EMPTY map when no snapshot exists
 * (small models on the assist path never emit it, or none yet) — the UI then falls
 * back to the honest status chip rather than a lying empty checklist. Never inferred
 * from action counts. */
export function deriveBuildProgress(
  events: AgentEvent[],
  status?: ConversationStatus,
): Map<number, StepState> {
  const progress = new Map<number, StepState>();
  // The latest plan supersedes prior ones; only count snapshots after it (a re-plan
  // starts a fresh checklist). Capture its step COUNT to bound the snapshot.
  let latestPlanIdx = -1;
  let latestRev = -1;
  let nSteps = 0;
  events.forEach((e, i) => {
    if (e.kind === "plan" && e.revision >= latestRev) {
      latestRev = e.revision;
      latestPlanIdx = i;
      nSteps = Array.isArray(e.steps) ? e.steps.length : 0;
    }
  });
  // Declarative: the LAST update_plan_progress action wins — it's the full picture.
  // ActionEvent-typed (the forEach guard only assigns action events); the cast at the
  // use site defeats TS's callback-assignment blindness (it narrows `latest` to null).
  let latest: ActionEvent | null = null;
  events.forEach((e, i) => {
    if (i < latestPlanIdx) return;
    if (e.kind === "action" && e.tool_call?.tool_name === "update_plan_progress") latest = e;
  });
  // Apply the declarative snapshot IF one exists — but do NOT early-return when it's
  // absent. A SMALL model (the NL-done-at-finish tier, e.g. local Qwen) never calls
  // update_plan_progress, so `latest` stays null; a `return` here skipped the FINISHED
  // reconciliation below and left every step unchecked on a build that actually finished
  // (the live #3 bug on Qwen). Fall through so terminal reconciliation runs regardless.
  const steps = ((latest as ActionEvent | null)?.tool_call?.arguments?.steps ?? []) as Array<{
    index: number;
    state: string;
  }>;
  if (Array.isArray(steps)) {
    for (const s of steps) {
      const idx = Number(s?.index);
      const st = String(s?.state);
      // Bound to the CURRENT plan's range. A malformed or stale index (e.g. index 99
      // on a 3-step plan, or a leftover from a longer prior plan) must NOT create a
      // phantom step or inflate the done-count into a lying checklist (a snapshot of
      // [{index:99,state:"done"}] would otherwise render "1/3" with every real step
      // pending). Out-of-range / non-integer indices are dropped.
      if (!Number.isInteger(idx) || idx < 1 || (nSteps > 0 && idx > nSteps)) continue;
      if (st === "done" || st === "active" || st === "pending") progress.set(idx, st as StepState);
    }
  }
  reconcileBuildProgress(progress, status, nSteps);
  return progress;
}
