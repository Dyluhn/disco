/**
 * Deep Research no longer has a plan, so there are no plan steps to match
 * progress against.
 *
 * v2 (gateless deep research) removed the decompose → plan → approve gate:
 * research starts on submit, the model's brief is the first visible output,
 * and the engine's search / observation / phase / section_done events are the
 * only honest progress signal (see `./stats.ts`). Matching engine actions back
 * to plan-step titles produced a checklist of things the run never promised.
 *
 * This module is kept as a compatibility stub rather than deleted (the same
 * treatment as `components/research/IterativeToggle.tsx`): no runtime module
 * imports it, and a stale caller gets an empty map rather than a broken build.
 */

/** Retained so a stale import still type-checks. Plan steps no longer exist. */
export type StepState = "pending" | "active" | "done";

/** Always empty — there is no plan to track progress against. */
export function computePlanProgress(): Map<number, StepState> {
  return new Map();
}
