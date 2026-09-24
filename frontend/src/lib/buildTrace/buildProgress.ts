/**
 * runthru-v2 (#3): Build progress for CAPABLE models — derived from the LATEST
 * declarative `update_plan_progress` snapshot (a full-state rewrite, not a
 * delta). Split out of `buildTrace.ts`; the terminal-state reconciliation tail
 * (FINISHED → all-done, STUCK/ERROR/IDLE → stalled) is its own helper so this
 * stays under the callable complexity cap.
 */
import type { ActionEvent, AgentEvent, ConversationStatus } from "@/types/agent";
import type { StepState } from "../buildTrace";
import { progressStepIndex } from "./plan";

/** Current-run host failure blocks synthetic completion, including when a
 * permissive release ends FINISHED. Read durable events, never warning prose or
 * volatile message metadata. A later host pass supersedes an earlier failure. */
interface OutcomeState {
  pending: Set<string>;
  releaseUnverified: boolean;
  ended: boolean;
  intent: string | null;
}

function resetOutcome(state: OutcomeState): void {
  state.pending.clear();
  state.releaseUnverified = false;
  state.ended = false;
}

function newOutcomeIntent(event: AgentEvent, current: string | null): string | null {
  if (event.kind === "workspace_mutation" && event.run_protocol_version === 1
    && event.operation.startsWith("agent.run-intent.")
    && event.run_intent_id && event.run_intent_id !== current) return event.run_intent_id;
  return null;
}

function updateOutcomeBoundary(event: AgentEvent, state: OutcomeState): void {
  const intent = newOutcomeIntent(event, state.intent);
  if (intent) {
    state.intent = intent;
    resetOutcome(state);
  }
  if (event.kind !== "status") return;
  if (state.intent && event.run_intent_id && event.run_intent_id !== state.intent) return;
  const terminal = event.status === "FINISHED" || event.status === "ERROR";
  if (state.ended && !terminal) resetOutcome(state);
  state.ended = terminal;
  if (event.status === "RUNNING" && event.detail === "unverified_release") {
    state.releaseUnverified = true;
  }
}

function updateHostOutcome(event: AgentEvent, state: OutcomeState): void {
  if (event.kind !== "verifier_started" && event.kind !== "verifier_verdict") return;
  if (event.source === "agent" || event.source === "user") return;
  const intent = event.kind === "verifier_started"
    ? event.run_intent_id : event.verification_result?.run_intent_id;
  if (state.intent && intent && intent !== state.intent) return;
  const key = JSON.stringify([event.target_id || event.artifact_path, event.artifact_kind,
    event.check_id || ""]);
  if (event.kind === "verifier_verdict" && event.verified === true && event.verdict === "pass") {
    state.pending.delete(key);
    if (state.pending.size === 0) state.releaseUnverified = false;
  } else {
    state.pending.add(key);
  }
}

function hasUnverifiedOutcome(events: AgentEvent[]): boolean {
  const state: OutcomeState = {
    pending: new Set(), releaseUnverified: false, ended: false, intent: null,
  };
  for (const event of events) {
    updateOutcomeBoundary(event, state);
    updateHostOutcome(event, state);
  }
  return state.releaseUnverified || state.pending.size > 0;
}

/** Terminal reconciliation:
 *  • FINISHED without a current unverified outcome retains the existing all-done
 *    presentation; the model need not send a final 100%-done snapshot.
 *  • FINISHED with host failure preserves explicit progress and stalls active
 *    steps. Ending a run is not itself proof that its work passed verification.
 *  • STUCK/ERROR/IDLE (stopped without completing) → "active" reads as stalled. */
function reconcileBuildProgress(
  progress: Map<number, StepState>,
  status: ConversationStatus | undefined,
  nSteps: number,
  unverified: boolean,
): void {
  if (status === "FINISHED" && nSteps > 0 && !unverified) {
    for (let i = 1; i <= nSteps; i++) progress.set(i, "done");
  } else if (status === "STUCK" || status === "ERROR" || status === "IDLE"
    || (status === "FINISHED" && unverified)) {
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
  const rejected = new Set(events.flatMap((e) =>
    (e.kind === "agent_error" || (e.kind === "observation" && e.tool_result?.success === false))
      && e.action_id ? [e.action_id] : [],
  ));
  let latest: ActionEvent | null = null;
  events.forEach((e, i) => {
    if (i < latestPlanIdx || rejected.has(e.id)) return;
    if (e.kind !== "action" || e.tool_call?.tool_name !== "update_plan_progress") return;
    const rows = e.tool_call.arguments?.steps;
    // Failed validation rejects the entire snapshot, including plausible rows.
    if (Array.isArray(rows) && rows.every((row) => {
      if (!row || typeof row !== "object") return false;
      const index = progressStepIndex(row.index);
      return index !== null && index >= 1 && (nSteps === 0 || index <= nSteps)
        && typeof row.state === "string" && ["pending", "active", "done"].includes(row.state);
    })) latest = e;
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
      const idx = progressStepIndex(s?.index);
      const st = String(s?.state);
      // Bound to the CURRENT plan's range. A malformed or stale index (e.g. index 99
      // on a 3-step plan, or a leftover from a longer prior plan) must NOT create a
      // phantom step or inflate the done-count. The complete snapshot was already
      // validated above; keep the per-row bound as a defensive check.
      if (idx === null || idx < 1 || (nSteps > 0 && idx > nSteps)) continue;
      if (st === "done" || st === "active" || st === "pending") progress.set(idx, st as StepState);
    }
  }
  reconcileBuildProgress(progress, status, nSteps, hasUnverifiedOutcome(events));
  return progress;
}
