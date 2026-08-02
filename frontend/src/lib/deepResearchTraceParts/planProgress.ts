/**
 * Per-sub-question plan progress, split out of `deepResearchTrace.ts`
 * (PKG-12-C/TS-0051). `deriveStats` also needs this computation (to tally
 * `subquestionsDone`) and imports it from here directly rather than routing
 * back through the parent.
 */
import type { AgentEvent } from "@/types/agent";
import type { DeepPlanView, StepState } from "@/lib/deepResearchTrace";

/** A step already `"done"` must never regress to `"active"` — search/synthesize
 *  actions that target an already-finished sub-question are no-ops here. */
function markActive(
  map: Map<number, StepState>,
  titleIndex: Map<string, number>,
  title: string,
): void {
  const idx = titleIndex.get(title);
  if (idx !== undefined && map.get(idx) !== "done") {
    map.set(idx, "active");
  }
}

function markDone(
  map: Map<number, StepState>,
  titleIndex: Map<string, number>,
  title: string,
): void {
  const idx = titleIndex.get(title);
  if (idx !== undefined) map.set(idx, "done");
}

/** Per-sub-question progress derived from the engine's emitted ActionEvents.
 *  A sub-question is "done" once `synthesize_section` has fired for it,
 *  "active" once any `search` action targeted it, "pending" otherwise.
 *  Returns a 1-based Map matching PlanPanel's contract. */
export function computePlanProgress(
  events: AgentEvent[],
  plan: DeepPlanView | null,
): Map<number, StepState> {
  const map = new Map<number, StepState>();
  if (!plan) return map;
  const titleIndex = new Map<string, number>();
  plan.steps.forEach((s, i) => titleIndex.set(s.title, i + 1)); // 1-based

  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    const name = e.tool_call.tool_name;
    const args = e.tool_call.arguments;
    if (name === "search") {
      markActive(map, titleIndex, String(args.subquestion ?? ""));
    } else if (name === "synthesize_section") {
      // Writing STARTED — the step is being worked, not finished. Marking it
      // done here (the old behavior) checked steps off ~30-60s early on local
      // models and left the strip lying about progress.
      markActive(map, titleIndex, String(args.section ?? ""));
    } else if (name === "section_done") {
      markDone(map, titleIndex, String(args.title ?? ""));
    }
  }
  return map;
}
