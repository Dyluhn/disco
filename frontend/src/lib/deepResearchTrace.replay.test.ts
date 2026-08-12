/**
 * Gap #55 — REPLAY coverage for the Deep Research derivers, which had ZERO
 * replay tests (`buildTrace.replay.test.ts` covers Build only).
 *
 * Mirrors the W9 build-replay discipline: the INPUT is a minimized event-stream
 * fixture and the assertion is on the real derived OUTPUT of the production
 * selectors in `deepResearchTrace.ts` — no mocking of internals. Covers the
 * streaming/iterate/report views: derivePlan, derivePlanProgress, deriveStats,
 * deriveReport, deriveAssemblingSections, deriveLiveTrace, deriveSourceTiers.
 *
 * REGRESSION net only (memory: feedback-live-model-proves-works): green proves
 * the events → DR-views wiring didn't break, NOT that a live DR run works.
 *
 * Run: npx vitest run src/lib/deepResearchTrace.replay.test.ts
 */

import { describe, expect, it } from "vitest";
import type { AgentEvent, ReportEvent } from "@/types/agent";
import {
  derivePlan,
  derivePlanProgress,
  deriveStats,
  deriveReport,
  deriveAssemblingSections,
  deriveLiveTrace,
  deriveSourceTiers,
} from "@/lib/deepResearchTrace";

function asEvents(events: unknown[]): AgentEvent[] {
  return events as unknown as AgentEvent[];
}

// Two-subquestion plan reused across the cases below.
const PLAN_EVENT = {
  kind: "plan",
  id: "plan-1",
  revision: 1,
  summary: "Research plan",
  context: "",
  steps: [{ title: "Market size" }, { title: "Key competitors" }],
};

// ---------------------------------------------------------------------------
// derivePlan — highest revision wins.
// ---------------------------------------------------------------------------

describe("derivePlan — latest plan revision wins", () => {
  it("returns null before any plan", () => {
    expect(derivePlan(asEvents([{ kind: "status", id: "s", status: "RUNNING" }]))).toBeNull();
  });

  it("a re-plan (higher revision) supersedes the first", () => {
    const events = asEvents([
      PLAN_EVENT,
      { ...PLAN_EVENT, id: "plan-2", revision: 2, summary: "Revised plan", steps: [{ title: "Only one" }] },
    ]);
    const plan = derivePlan(events);
    expect(plan?.revision).toBe(2);
    expect(plan?.summary).toBe("Revised plan");
    expect(plan?.steps).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// derivePlanProgress — search→active, section_done→done, honest mid-run.
// ---------------------------------------------------------------------------

describe("derivePlanProgress — per-sub-question progress", () => {
  const plan = derivePlan(asEvents([PLAN_EVENT]))!;

  it("a search action marks its sub-question active (not done)", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size", round: 1, rounds_max: 3 } } },
    ]);
    const p = derivePlanProgress(events, plan);
    expect(p.get(1)).toBe("active");
    expect(p.get(2)).toBeUndefined(); // untouched sub-question stays pending (absent)
  });

  it("section_done marks the matching sub-question done", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size" } } },
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "synthesize_section", arguments: { section: "Market size" } } },
      { kind: "action", id: "a3", thought: "", tool_call: { tool_name: "section_done", arguments: { title: "Market size" } } },
    ]);
    const p = derivePlanProgress(events, plan);
    expect(p.get(1)).toBe("done");
  });

  it("synthesize_section alone reads as active, NOT done (no early check-off)", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "synthesize_section", arguments: { section: "Key competitors" } } },
    ]);
    expect(derivePlanProgress(events, plan).get(2)).toBe("active");
  });

  it("empty map when there is no plan", () => {
    expect(derivePlanProgress(asEvents([]), null).size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// deriveStats — sources discovered, active sub-question/round, phase, thought.
// ---------------------------------------------------------------------------

describe("deriveStats — live header strip stats", () => {
  const plan = derivePlan(asEvents([PLAN_EVENT]))!;

  it("sums observation `added` counts into sourcesDiscovered", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "observation", id: "o1", action_id: "x", tool_result: { tool_name: "observation", success: true, content: "", structured: { added: 3, total_for_subq: 3, round: 1 } } },
      { kind: "observation", id: "o2", action_id: "y", tool_result: { tool_name: "observation", success: true, content: "", structured: { added: 2, total_for_subq: 5, round: 2 } } },
    ]);
    const stats = deriveStats(events, plan);
    expect(stats.sourcesDiscovered).toBe(5);
    expect(stats.subquestionsTotal).toBe(2);
  });

  it("tracks the active sub-question + round from the latest search", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size", round: 2, rounds_max: 4 } } },
    ]);
    const stats = deriveStats(events, plan);
    expect(stats.activeSubquestion).toEqual({ title: "Market size", index: 1 });
    expect(stats.activeRound).toEqual({ current: 2, max: 4 });
  });

  it("surfaces the engine phase + a gap_reason rationale as lastThought", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "phase", arguments: { phase: "gather" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "gap_reason", success: true, content: "", structured: { sufficient: false, rationale: "Still missing pricing data" } } },
    ]);
    const stats = deriveStats(events, plan);
    expect(stats.phase).toBe("gather");
    expect(stats.lastThought).toBe("Still missing pricing data");
  });
});

// ---------------------------------------------------------------------------
// deriveReport + deriveAssemblingSections — the assembling → final swap.
// ---------------------------------------------------------------------------

const REPORT_EVENT = {
  kind: "report",
  id: "r1",
  query: "market overview",
  summary: "Executive summary",
  sections: [
    { id: "sec-1", title: "Market size", markdown: "The market is [[p1]].", cited_passage_ids: ["p1"], confidence: "high", disputed_notes: [], unsupported_count: 0 },
  ],
  passages: [
    { id: "p1", source_url: "https://a.example/report" },
    { id: "p2", source_url: "https://a.example/report" }, // same URL → deduped in cited tier
  ],
  all_hits: [
    { url: "https://a.example/report", status: "ok" }, // cited
    { url: "https://b.example/blog", status: "ok" }, // reviewed (read, not cited)
    { url: "https://c.example/dead", status: "error" }, // discovered (failed extract)
  ],
  unsupported_count: 0,
  bounded_by: null,
  depth_tier: "standard",
};

describe("deriveReport — final ReportEvent extraction", () => {
  it("returns null before the report arrives", () => {
    expect(deriveReport(asEvents([PLAN_EVENT]))).toBeNull();
  });
  it("returns the ReportEvent once emitted", () => {
    const r = deriveReport(asEvents([PLAN_EVENT, REPORT_EVENT]));
    expect(r?.kind).toBe("report");
    expect(r?.sections[0]?.title).toBe("Market size");
  });
});

describe("deriveAssemblingSections — pending → writing → done", () => {
  const plan = derivePlan(asEvents([PLAN_EVENT]))!;

  it("a sub-question with no activity is pending", () => {
    const out = deriveAssemblingSections(asEvents([PLAN_EVENT]), plan);
    expect(out.map((s) => s.state)).toEqual(["pending", "pending"]);
  });

  it("synthesize_section moves a card to writing", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "synthesize_section", arguments: { section: "Market size" } } },
    ]);
    const out = deriveAssemblingSections(events, plan);
    expect(out.find((s) => s.title === "Market size")?.state).toBe("writing");
  });

  it("the final report supersedes a placeholder with the real section (done)", () => {
    const events = asEvents([PLAN_EVENT, REPORT_EVENT]);
    const out = deriveAssemblingSections(events, plan);
    const market = out.find((s) => s.title === "Market size");
    expect(market?.state).toBe("done");
    expect(market?.section?.id).toBe("sec-1");
  });
});

// ---------------------------------------------------------------------------
// deriveLiveTrace — engine internals filtered; section_done checks its row.
// ---------------------------------------------------------------------------

describe("deriveLiveTrace — user-visible operations only", () => {
  it("renders search rows + filters phase internals", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a0", thought: "", tool_call: { tool_name: "phase", arguments: { phase: "gather" } } },
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { query: "market size 2026" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "observation", success: true, content: "", structured: { added: 4, total_for_subq: 4, round: 1 } } },
    ]);
    const items = deriveLiveTrace(events, "RUNNING");
    // phase is filtered out; the search row survives with its observation detail.
    expect(items).toHaveLength(1);
    expect(items[0]?.label).toContain("market size 2026");
    expect(items[0]?.detail).toContain("+4 sources");
    expect(items[0]?.status).toBe("done");
  });

  it("adds an ellipsis only when a gap rationale is truncated", () => {
    const longRationale = "a".repeat(120);
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { query: "market size 2026" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "gap_reason", success: true, content: "", structured: { sufficient: false, rationale: longRationale } } },
    ]);
    const items = deriveLiveTrace(events, "RUNNING");
    expect(items[0]?.detail).toBe(`Gap noted: ${"a".repeat(100)}…`);
  });

  it("does not add an ellipsis when a gap rationale fits", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { query: "market size 2026" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "gap_reason", success: true, content: "", structured: { sufficient: false, rationale: "Still missing pricing data" } } },
    ]);
    const items = deriveLiveTrace(events, "RUNNING");
    expect(items[0]?.detail).toBe("Gap noted: Still missing pricing data");
  });

  it("section_done marks the matching synthesize row done", () => {
    const events = asEvents([
      PLAN_EVENT,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "synthesize_section", arguments: { section: "Market size" } } },
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "section_done", arguments: { title: "Market size" } } },
    ]);
    const items = deriveLiveTrace(events, "RUNNING");
    const synth = items.find((i) => i.label.includes("Market size"));
    expect(synth?.status).toBe("done");
  });
});

// ---------------------------------------------------------------------------
// deriveSourceTiers — cited / reviewed / discovered split.
// ---------------------------------------------------------------------------

describe("deriveSourceTiers — three-tier corpus split", () => {
  it("empty tiers when there is no report", () => {
    expect(deriveSourceTiers(null)).toEqual({ cited: [], reviewed: [], discovered: [] });
  });

  it("splits cited (deduped by URL) / reviewed / discovered", () => {
    const report = {
      ...REPORT_EVENT,
      all_hits: [
        ...REPORT_EVENT.all_hits,
        { url: "https://d.example/unattempted" },
      ],
    } as unknown as ReportEvent;
    const tiers = deriveSourceTiers(report);
    // two passages, same URL → ONE cited row
    expect(tiers.cited).toHaveLength(1);
    // b.example was read (status ok) but not cited → reviewed
    expect(tiers.reviewed.map((h) => h.url)).toEqual(["https://b.example/blog"]);
    // c.example failed extraction → discovered
    expect(tiers.discovered.map((h) => h.url)).toEqual([
      "https://c.example/dead",
      "https://d.example/unattempted",
    ]);
  });
});
