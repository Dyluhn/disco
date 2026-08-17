/**
 * Pure derivations extracted from ResearchSurface's derived-state block
 * (the `started` / `noBlocksYet` / `effectiveLeaderId` / `researchPhase` /
 * `streamState` computations). Each function's branching is exactly what it
 * was in the component — only relocated, so it's measured on its own instead
 * of piling onto ResearchSurface's cyclomatic count.
 */
import type { Phase } from "@/hooks/useResearchStream";
import type { GroundedAnswer } from "@/types/grounded";

export function deriveNoBlocksYet(blocksLength: number, streamingBlockId: string | null): boolean {
  return blocksLength === 0 && streamingBlockId === null;
}

export function deriveEffectiveLeaderId(
  leaderId: string | null | undefined,
  lastSelected: string | null | undefined,
): string | null {
  return leaderId === undefined ? (lastSelected ?? null) : leaderId;
}

// Gap #29 — a stable, assertable phase attribute on the surface. The submit
// answer itself is model+live-search+streaming (non-deterministic), but the
// PHASE TRANSITIONS (idle → running → done) are deterministic and can be
// asserted by the harness without depending on the answer content.
export function deriveResearchPhase(started: boolean, phase: Phase): "idle" | "running" | "done" {
  return !started ? "idle" : phase === "running" ? "running" : "done";
}

// Gap #38 — expose the token/block/final stream reconciliation as a stable
// attribute so a fixture-driven test can assert the standard-search stream
// state machine (idle → token → block → final) without scraping the DOM.
export function deriveStreamState(
  answer: GroundedAnswer | null,
  streamingBlockId: string | null,
  blocksLength: number,
): "final" | "token" | "block" | "idle" {
  return answer
    ? "final"
    : streamingBlockId
      ? "token"
      : blocksLength > 0
        ? "block"
        : "idle";
}
