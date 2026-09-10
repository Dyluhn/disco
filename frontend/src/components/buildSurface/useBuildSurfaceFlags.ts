/**
 * Status-derived UI gating flags for the Build surface: whether the feed is in
 * replay mode, whether steering/re-planning is available, whether the run has
 * settled into a terminal state, and whether the verbose feed should collapse
 * to the compact stage card. Pure derivations of `b.status`/`b.plan`/`verboseChat`,
 * extracted verbatim from BuildSurface.tsx into their own hook (a real move off
 * the component's own callable — see the module doc for why that matters for
 * the AST cyclomatic-complexity count, not just line count).
 */

import { useMemo } from "react";
import type { BuildController } from "./types";

export interface BuildSurfaceFlags {
  isReplaying: boolean;
  steerable: boolean;
  settled: boolean;
  collapseFeed: boolean;
  draftingPlan: boolean;
  terminalIncomplete: boolean;
}

export function useBuildSurfaceFlags(b: BuildController, verboseChat: boolean): BuildSurfaceFlags {
  return useMemo(() => {
    // RP-06 replay: when not live (RUNNING), allow stepping through event history.
    const isReplaying = b.started && b.status !== "RUNNING";
    // Steer is available whenever a conversation EXISTS to steer — including
    // STUCK/FINISHED, since the engine reopens on send_message (engine.py:671).
    // Plan-approval is the only state where steering is wrong (the plan IS the
    // input). Keeping SteerInput disabled mid-confirmation matches the prior shape.
    const steerable =
      b.status === "RUNNING" ||
      b.status === "WAITING_FOR_CONFIRMATION" ||
      b.status === "AWAITING_USER_DECISION" ||
      b.status === "STUCK" ||
      b.status === "FINISHED" ||
      b.status === "PAUSED";
    const settled = b.status === "FINISHED" || b.status === "IDLE" || b.status === "STUCK";
    // W-43 — Verbose Agent Chat. Default ON (full feed). When OFF, once the first plan
    // is approved (a plan exists and we're past the approval gate), collapse the
    // running narrative to the compact AgentStageCard. The gates (confirm/decision/
    // question) and the deliverable download/open panel render OUTSIDE this branch, so
    // they always show; the full feed stays reachable via the card's expand and the
    // inspector's "Agent History" tab (codex P1 — no affordance is hidden).
    const collapseFeed = !verboseChat && b.plan != null && !b.awaitingPlan;
    // Front-door "drafting…" moment: the run is live but no plan exists yet (the
    // agent is exploring + composing the first plan). Show a skeleton so the surface
    // never reads as frozen between submit and the plan-approval gate.
    const draftingPlan = b.status === "RUNNING" && !b.plan;
    const terminalIncomplete = b.status === "STUCK" || b.status === "ERROR";
    return { isReplaying, steerable, settled, collapseFeed, draftingPlan, terminalIncomplete };
  }, [b.started, b.status, b.plan, b.awaitingPlan, verboseChat]);
}
