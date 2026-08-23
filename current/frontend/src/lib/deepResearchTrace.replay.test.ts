/**
 * Gap #55 — REPLAY coverage for the Deep Research derivers, which had ZERO
 * replay tests (`buildTrace.replay.test.ts` covers Build only).
 *
 * Mirrors the W9 build-replay discipline: the INPUT is a minimized event-stream
 * fixture and the assertion is on the real derived OUTPUT of the production
 * selectors in `deepResearchTrace.ts` — no mocking of internals. Covers the
 * streaming/report views: deriveBrief, deriveStats, deriveReport,
 * deriveAssemblingSections, deriveLiveTrace, deriveSourceTiers.
 *
 * v2 (gateless deep research): there is no plan and no approval gate, so the
 * plan derivers (derivePlan / derivePlanProgress) are gone along with the
 * cases that covered them — progress now comes from the search / observation /
 * phase / section_done events the engine really emits, and the model's brief
 * is the first visible output.
 *
 * REGRESSION net only (memory: feedback-live-model-proves-works): green proves
 * the events → DR-views wiring didn't break, NOT that a live DR run works.
 *
 * Run: npx vitest run src/lib/deepResearchTrace.replay.test.ts
 */

import { describe, expect, it } from "vitest";
import type { AgentEvent, ReportEvent } from "@/types/agent";
import {
  deriveBrief,
  deriveStats,
  deriveReport,
  deriveAssemblingSections,
  deriveLiveTrace,
  deriveSourceTiers,
} from "@/lib/deepResearchTrace";

function asEvents(events: unknown[]): AgentEvent[] {
  return events as unknown as AgentEvent[];
}

// The run's opening brief, as the engine emits it: an ActionEvent whose
// tool_name is "brief", mirrored as an assistant chat message.
const BRIEF_TEXT = "I read this as a question about market structure, not headline size.";
const BRIEF_ACTION = {
  kind: "action",
  id: "brief-1",
  seq: 1,
  thought: "",
  tool_call: { tool_name: "brief", arguments: { text: BRIEF_TEXT } },
};

// ---------------------------------------------------------------------------
// deriveBrief — the first visible output of a gateless run.
// ---------------------------------------------------------------------------

describe("deriveBrief — the run's opening brief", () => {
  it("returns null before the brief lands", () => {
    expect(deriveBrief(asEvents([{ kind: "status", id: "s", status: "RUNNING" }]))).toBeNull();
  });

  it("reads the brief ActionEvent's text", () => {
    expect(deriveBrief(asEvents([BRIEF_ACTION]))).toBe(BRIEF_TEXT);
  });

  it("falls back to the first pre-report assistant message when no brief action arrived", () => {
    const events = asEvents([
      { kind: "message", id: "m1", seq: 1, message: { role: "user", content: "the question" } },
      { kind: "message", id: "m2", seq: 2, message: { role: "assistant", content: BRIEF_TEXT } },
    ]);
    expect(deriveBrief(events)).toBe(BRIEF_TEXT);
  });

  it("never mistakes a post-report follow-up answer for the brief", () => {
    const events = asEvents([
      { ...REPORT_EVENT, seq: 5 },
      { kind: "message", id: "m9", seq: 9, message: { role: "assistant", content: "A follow-up answer." } },
    ]);
    expect(deriveBrief(events)).toBeNull();
  });

  it("prefers the typed brief action over a mirrored chat message", () => {
    const events = asEvents([
      BRIEF_ACTION,
      { kind: "message", id: "m2", seq: 2, message: { role: "assistant", content: "something else" } },
    ]);
    expect(deriveBrief(events)).toBe(BRIEF_TEXT);
  });
});

// ---------------------------------------------------------------------------
// deriveStats — sources discovered, active search/round, phase, thought, time.
// ---------------------------------------------------------------------------

describe("deriveStats — live header strip stats", () => {
  it("sums observation `added` counts into sourcesDiscovered", () => {
    const events = asEvents([
      { kind: "observation", id: "o1", action_id: "x", tool_result: { tool_name: "observation", success: true, content: "", structured: { added: 3, total_for_subq: 3, round: 1 } } },
      { kind: "observation", id: "o2", action_id: "y", tool_result: { tool_name: "observation", success: true, content: "", structured: { added: 2, total_for_subq: 5, round: 2 } } },
    ]);
    const stats = deriveStats(events);
    expect(stats.sourcesDiscovered).toBe(5);
  });

  it("counts real search rounds and finalized sections, never plan steps", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size", query: "q1" } } },
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Key competitors", query: "q2" } } },
      { kind: "action", id: "a3", thought: "", tool_call: { tool_name: "section_done", arguments: { title: "Market size" } } },
    ]);
    const stats = deriveStats(events);
    expect(stats.searches).toBe(2);
    expect(stats.sectionsDone).toBe(1);
  });

  it("tracks the active sub-question + round from the latest search", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size", round: 2, rounds_max: 4 } } },
    ]);
    const stats = deriveStats(events);
    expect(stats.activeSubquestion).toEqual({ title: "Market size" });
    expect(stats.activeRound).toEqual({ current: 2, max: 4 });
  });

  it("clears the search indicator once gather is over", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size", round: 2, rounds_max: 4 } } },
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "phase", arguments: { phase: "synthesize" } } },
    ]);
    const stats = deriveStats(events);
    expect(stats.activeSubquestion).toBeNull();
    expect(stats.activeRound).toBeNull();
  });

  it("surfaces the engine phase + a gap_reason rationale as lastThought", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "phase", arguments: { phase: "gather" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "gap_reason", success: true, content: "", structured: { sufficient: false, rationale: "Still missing pricing data" } } },
    ]);
    const stats = deriveStats(events);
    expect(stats.phase).toBe("gather");
    expect(stats.lastThought).toBe("Still missing pricing data");
  });

  it("computes elapsed seconds from the event timestamps, not a hardcoded null", () => {
    const events = asEvents([
      { kind: "status", id: "s1", seq: 1, status: "RUNNING", timestamp: "2026-06-06T12:00:00Z" },
      { kind: "action", id: "a1", seq: 2, thought: "", timestamp: "2026-06-06T12:02:30Z", tool_call: { tool_name: "search", arguments: { query: "q" } } },
    ]);
    expect(deriveStats(events).elapsedSeconds).toBe(150);
  });

  it("reports elapsed as null when no event carries a timestamp", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { query: "q" } } },
    ]);
    expect(deriveStats(events).elapsedSeconds).toBeNull();
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
    expect(deriveReport(asEvents([BRIEF_ACTION]))).toBeNull();
  });
  it("returns the ReportEvent once emitted", () => {
    const r = deriveReport(asEvents([BRIEF_ACTION, REPORT_EVENT]));
    expect(r?.kind).toBe("report");
    expect(r?.sections[0]?.title).toBe("Market size");
  });
});

describe("deriveAssemblingSections — pending → writing → done", () => {
  it("a sub-question with no activity is pending", () => {
    const out = deriveAssemblingSections(asEvents([BRIEF_ACTION]));
    expect(out).toEqual([]);
  });

  it("synthesize_section moves a card to writing", () => {
    // Mid-run activity NEVER fabricates section placeholders — the writer owns
    // section structure and only the ReportEvent supplies it.
    const events = asEvents([
      BRIEF_ACTION,
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "synthesize_section", arguments: { section: "Market size" } } },
    ]);
    const out = deriveAssemblingSections(events);
    expect(out).toEqual([]);
  });

  it("the final report supersedes a placeholder with the real section (done)", () => {
    const events = asEvents([BRIEF_ACTION, REPORT_EVENT]);
    const out = deriveAssemblingSections(events);
    const market = out.find((s) => s.title === "Market size");
    expect(market?.state).toBe("done");
    expect(market?.section?.id).toBe("sec-1");
  });
});

// ---------------------------------------------------------------------------
// deriveLiveTrace — engine internals filtered; section_done checks its row.
// ---------------------------------------------------------------------------

describe("deriveLiveTrace — user-visible operations only", () => {
  it("labels the brief instead of leaking its raw internal tool name", () => {
    const items = deriveLiveTrace(asEvents([BRIEF_ACTION]), "RUNNING");
    expect(items).toHaveLength(1);
    // The unknown-kind fallback would have rendered the bare tool name.
    expect(items[0]?.label).not.toBe("brief");
    expect(items[0]?.label).toBe("Framed the question");
    expect(items[0]?.detail).toContain("market structure");
    // No observation ever pairs with a brief — it must not spin forever.
    expect(items[0]?.status).toBe("done");
  });

  it("renders search rows + filters phase internals", () => {
    const events = asEvents([
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
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { query: "market size 2026" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "gap_reason", success: true, content: "", structured: { sufficient: false, rationale: longRationale } } },
    ]);
    const items = deriveLiveTrace(events, "RUNNING");
    expect(items[0]?.detail).toBe(`Gap noted: ${"a".repeat(100)}…`);
  });

  it("does not add an ellipsis when a gap rationale fits", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { query: "market size 2026" } } },
      { kind: "observation", id: "o1", action_id: "a1", tool_result: { tool_name: "gap_reason", success: true, content: "", structured: { sufficient: false, rationale: "Still missing pricing data" } } },
    ]);
    const items = deriveLiveTrace(events, "RUNNING");
    expect(items[0]?.detail).toBe("Gap noted: Still missing pricing data");
  });

  it("section_done marks the matching synthesize row done", () => {
    const events = asEvents([
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

  it("uses canonical source identity across tracking and fragment variants", () => {
    const report = {
      ...REPORT_EVENT,
      passages: [
        {
          id: "p-canonical",
          source_url: "https://example.com/release?utm_source=feed#details",
        },
      ],
      all_hits: [
        { url: "http://example.com/release/", status: "ok" },
        { url: "https://other.example/story?utm_campaign=x", status: "ok" },
        { url: "http://other.example/story#top", status: "ok" },
      ],
    } as unknown as ReportEvent;

    const tiers = deriveSourceTiers(report);

    expect(tiers.cited).toHaveLength(1);
    expect(tiers.reviewed.map((hit) => hit.url)).toEqual([
      "https://other.example/story?utm_campaign=x",
    ]);
    expect(tiers.discovered).toEqual([]);
  });

  it("keeps durable reviewed passages that have no discovery hit", () => {
    const report = {
      ...REPORT_EVENT,
      reviewed_passages: [
        {
          id: "upload-only",
          source_url: "file://uploads/model-card.pdf",
          source_title: "Uploaded model card",
        },
        {
          id: "duplicate-hit",
          source_url: "https://b.example/blog#reviewed",
          source_title: "Reviewed blog",
        },
      ],
    } as unknown as ReportEvent;

    const tiers = deriveSourceTiers(report);

    expect(tiers.reviewed.map((source) => source.url)).toEqual([
      "file://uploads/model-card.pdf",
      "https://b.example/blog#reviewed",
    ]);
    expect(tiers.reviewed.map((source) => source.title)).toEqual([
      "Uploaded model card",
      "Reviewed blog",
    ]);
  });
});
