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
import REFUSED_TURN from "@/lib/__fixtures__/query-refused-turn.json";
import NARROWED_SEARCH from "@/lib/__fixtures__/narrowed-search-action.json";
import ZERO_YIELD_AND_RERUN from "@/lib/__fixtures__/zero-yield-and-rerun.json";
import type { AgentEvent, ReportEvent } from "@/types/agent";
import {
  deriveBrief,
  deriveStats,
  deriveReport,
  deriveResearchCheckpoint,
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

  it("does not invent a sub-question or round denominator", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size", round: 2, rounds_max: 4 } } },
    ]);
    const stats = deriveStats(events);
    expect(stats).not.toHaveProperty("activeSubquestion");
    expect(stats).not.toHaveProperty("activeRound");
  });

  it("maps actual writing and review phases", () => {
    const events = asEvents([
      { kind: "action", id: "a1", thought: "", tool_call: { tool_name: "search", arguments: { subquestion: "Market size", round: 2, rounds_max: 4 } } },
      { kind: "action", id: "a2", thought: "", tool_call: { tool_name: "phase", arguments: { phase: "synthesize" } } },
    ]);
    const stats = deriveStats(events);
    expect(stats.phase).toBe("synthesize");
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

  it("does not turn an empty report-shaped event into a report", () => {
    expect(
      deriveReport(
        asEvents([{ ...REPORT_EVENT, summary: "", sections: [] }]),
      ),
    ).toBeNull();
  });

  it("derives a paused checkpoint separately from a report", () => {
    const checkpoint = {
      kind: "research_checkpoint",
      id: "cp-1",
      query: "market overview",
      passages: [{ id: "p1" }],
      all_hits: [{ url: "https://example.com" }],
      trail: [{ query: "market" }],
      completed_queries: ["market"],
      depth_tier: "standard_deep",
      recency_window: "month",
    };
    expect(deriveResearchCheckpoint(asEvents([checkpoint]))?.id).toBe("cp-1");
    expect(deriveReport(asEvents([checkpoint]))).toBeNull();
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
    expect(items[0]?.detail).toContain("Added 4 sources");
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
// deriveLiveTrace — refused queries (T1's `query_refused`).
//
// `REFUSED_TURN` is a VERBATIM slice of a real run: turn 12 of
// `lanes/T1-evidence/live/run-01/events.jsonl`, seq 137-142, where the model
// proposed three queries, the host refused two as near-duplicates and only the
// third reached an engine. The `exhausted` / `queued` / `retry_budget_spent` /
// `empty` classes never occurred in that run, so those are built here to the
// payload contract (`lanes/T1-evidence/F5-query-refused-contract.md`) and
// labelled as constructed.
// ---------------------------------------------------------------------------

function refusal(id: string, args: Record<string, unknown>) {
  return { kind: "action", id, thought: "", tool_call: { tool_name: "query_refused", arguments: args } };
}

/** One search and the empty observation that answered it, built to the payload
 *  contract the real events carry (`_search_turn.py::_record_result`). Used for
 *  the four empty classes T3's pass-3 run did not itself produce. */
function observationFor(
  id: string,
  yieldReason: string,
  structured: Record<string, unknown> = {},
) {
  return [
    { kind: "action", id, thought: "", tool_call: { tool_name: "search", arguments: { query: `q-${id}`, origin: "model" } } },
    {
      kind: "observation",
      id: `${id}-obs`,
      action_id: id,
      tool_result: {
        tool_name: "observation",
        content: "",
        structured: { added: 0, total_for_subq: 7, yield_reason: yieldReason, ...structured },
      },
    },
  ];
}

describe("deriveLiveTrace — a refused query is a row, not silence", () => {
  it("renders the real turn's two refusals beside the one search that ran", () => {
    const items = deriveLiveTrace(asEvents(REFUSED_TURN), "RUNNING");
    expect(items.map((i) => i.label)).toEqual([
      'Not searched: "China Integrated Circuit Industry Investment Fund Big Fund III 344 billion yuan May 2024 Caixin registration" — the same as turn 1\'s "China National Integrated Circuit Industry Investment Fund Big Fund III 2024 344 billion yuan"',
      'Not searched: "SemiAnalysis TechInsights SMIC N+3 N+2 wafer economics cost per wafer defect density yield quad patterning independent analysis" — the same as turn 8\'s "TechInsights SemiAnalysis SMIC N+3 wafer cost economics quad patterning 50 percent penalty yield defect density independent"',
      'Searching: "TechInsights SemiAnalysis Huawei Ascend 910C 910B HBM bottleneck benchmark versus Nvidia H100 independent teardown 2025"',
    ]);
  });

  it("settles a refusal on arrival — nothing ever pairs with it", () => {
    const items = deriveLiveTrace(asEvents(REFUSED_TURN), "RUNNING");
    expect(items.slice(0, 2).map((i) => i.status)).toEqual(["settled", "settled"]);
  });

  // F7 item 3: the refusal used to land "done", which is the feed's SUCCESS
  // status — a green check over "Not searched: …". A wall's verdict is neither.
  it("does not mark a refused query as a success", () => {
    const items = deriveLiveTrace(asEvents(REFUSED_TURN), "RUNNING");
    const refusals = items.filter((i) => i.label.startsWith("Not searched: "));
    const searches = items.filter((i) => i.label.startsWith("Searching: "));
    expect(refusals).toHaveLength(2);
    expect(refusals.every((i) => i.status === "settled")).toBe(true);
    // The searches beside them DID reach the world, so they stay successes.
    expect(searches.every((i) => i.status === "done")).toBe(true);
  });

  it("keeps the settled rows that DID record work as successes", () => {
    // `brief`, `hold_resumed` and `stop_requested` also settle on arrival, but
    // each records something that happened: only the refusal is a verdict.
    const items = deriveLiveTrace(
      asEvents([
        BRIEF_ACTION,
        { kind: "action", id: "hr", thought: "", tool_call: { tool_name: "hold_resumed", arguments: { waited_s: 240, engines_live: ["brave"] } } },
        { kind: "action", id: "sr", thought: "", tool_call: { tool_name: "stop_requested", arguments: { requested_at: "2026-09-02T13:00:00Z" } } },
      ]),
      "RUNNING",
    );
    expect(items.map((i) => i.status)).toEqual(["done", "done", "done"]);
  });

  it("a turn that lost all three proposals reads as three refusals and no search", () => {
    // Constructed: the real run never lost a whole turn, and this is the state
    // the surface used to render as "searching" and then silence.
    const items = deriveLiveTrace(
      asEvents([
        { kind: "action", id: "t", thought: "", tool_call: { tool_name: "turn", arguments: { n: 12, of: 16, phase: "planning" } } },
        ...REFUSED_TURN.filter((e) => e.tool_call?.tool_name === "query_refused"),
        refusal("r3", { query: "SMIC N+2 yield", reason: "exhausted", angle: "SMIC yield economics", why: "four distinct queries reached the world and admitted nothing" }),
      ]),
      "RUNNING",
    );
    expect(items).toHaveLength(3);
    expect(items.every((i) => i.label.startsWith("Not searched: "))).toBe(true);
    expect(items.some((i) => i.label.startsWith("Searching"))).toBe(false);
  });

  it("names the closed angle and the host's own sentence for an exhausted one", () => {
    const items = deriveLiveTrace(
      asEvents([
        refusal("r", {
          query: "SMIC N+3 yield teardown",
          reason: "exhausted",
          angle: "SMIC yield economics",
          why: "four distinct queries reached the world and admitted nothing",
        }),
      ]),
      "RUNNING",
    );
    expect(items[0]?.label).toBe(
      'Not searched: "SMIC N+3 yield teardown" — angle "SMIC yield economics" is exhausted: four distinct queries reached the world and admitted nothing',
    );
  });

  it("says the host is already re-running the queued one", () => {
    const items = deriveLiveTrace(
      asEvents([
        refusal("r", {
          query: "EUV light source LPP China 2025",
          reason: "queued",
          duplicates: "EUV lithography China light source LPP progress",
        }),
      ]),
      "RUNNING",
    );
    expect(items[0]?.label).toBe(
      'Not searched: "EUV light source LPP China 2025" — the host is already re-running "EUV lithography China light source LPP progress"',
    );
  });

  it("says a spent retry budget left the angle untested", () => {
    const items = deriveLiveTrace(
      asEvents([refusal("r", { query: "YMTC Xtacking 3.0 teardown", reason: "retry_budget_spent" })]),
      "RUNNING",
    );
    expect(items[0]?.label).toBe(
      'Not searched: "YMTC Xtacking 3.0 teardown" — the host stopped re-running it, so its angle is untested',
    );
  });

  it("names a blank proposal without quoting an empty string", () => {
    const items = deriveLiveTrace(asEvents([refusal("r", { query: "", reason: "empty" })]), "RUNNING");
    expect(items[0]?.label).toBe("Not searched: the proposal was blank");
  });

  it("never leaks the raw reason token or the raw action name", () => {
    const items = deriveLiveTrace(
      asEvents([
        ...REFUSED_TURN,
        refusal("r3", { query: "q", reason: "retry_budget_spent" }),
        refusal("r4", { query: "q2", reason: "exhausted", angle: "a", why: "w" }),
        refusal("r5", { query: "q3", reason: "queued", duplicates: "d" }),
        refusal("r6", { query: "q4", reason: "a_reason_the_ui_has_never_seen" }),
      ]),
      "RUNNING",
    );
    const rendered = items.map((i) => `${i.label} ${i.detail ?? ""}`).join("\n");
    for (const token of [
      "query_refused",
      "near_duplicate",
      "retry_budget_spent",
      "a_reason_the_ui_has_never_seen",
    ]) {
      expect(rendered).not.toContain(token);
    }
    // The unknown class still gets a row and still says what happened.
    expect(items.at(-1)?.label).toBe('Not searched: "q4" — the host refused it');
  });
});

// ---------------------------------------------------------------------------
// F7 item 4 — an empty admission says WHY, and a host re-run says who ran it.
//
// The fixture is a VERBATIM slice of lane T3's pass-3 live run
// (`lanes/T3-evidence/live-pass3/run-01/events.jsonl`, seq 24-30): two model
// searches, the observation that admitted 2, the observation that admitted 0 on
// an extraction outage, the turn marker, and then the LOOP's own re-issue of
// that same query and the 3 sources it finally admitted. Two payload fields the
// frontend never reads are elided to keep the fixture small and are named in
// the file: `tool_result.content` (13 KB of model-facing prose) and
// `retrieval_trace.queries` (10 KB of raw engine results).
// ---------------------------------------------------------------------------

describe("deriveLiveTrace — an empty admission says why", () => {
  it("names the extraction outage, with the page count the trace recorded", () => {
    const items = deriveLiveTrace(asEvents(ZERO_YIELD_AND_RERUN), "RUNNING");
    const empty = items.find((i) => i.detail?.startsWith("Added 0 sources"));
    expect(empty?.detail).toBe(
      "Added 0 sources (12 admitted so far) — 6 pages found, none could be read",
    );
  });

  it("leaves a productive admission exactly as it read before", () => {
    const items = deriveLiveTrace(asEvents(ZERO_YIELD_AND_RERUN), "RUNNING");
    expect(items[0]?.detail).toBe("Added 2 sources (12 admitted so far)");
  });

  it("says who issued a query the loop re-ran by itself", () => {
    const items = deriveLiveTrace(asEvents(ZERO_YIELD_AND_RERUN), "RUNNING");
    const rerun = items.find((i) => i.label.startsWith("Re-running: "));
    expect(rerun?.label).toBe(
      'Re-running: "EIA overnight capital cost gas combustion turbine peaker 2024 Cost and Performance Characteristics"',
    );
    // Same query text as the model's own row two above it: before this, the
    // trace showed the identical "Searching:" line twice with no way to tell
    // the second was the host's work.
    expect(items.some((i) => i.label === `Searching: ${rerun!.label.slice("Re-running: ".length)}`)).toBe(true);
    expect(rerun?.detail).toBe("Added 3 sources (15 admitted so far)");
  });

  it("renders each empty class in the reader's words and never the raw token", () => {
    const items = deriveLiveTrace(
      asEvents([
        ...observationFor("s1", "no_hits"),
        ...observationFor("s2", "duplicates_or_filtered"),
        ...observationFor("s3", "budget"),
        ...observationFor("s4", "provider_degraded"),
        ...observationFor("s5", "a_class_the_ui_has_never_seen"),
      ]),
      "RUNNING",
    );
    expect(items.map((i) => i.detail)).toEqual([
      "Added 0 sources (7 admitted so far) — no results",
      "Added 0 sources (7 admitted so far) — only sources already in the pool",
      "Added 0 sources (7 admitted so far) — the source budget is full",
      "Added 0 sources (7 admitted so far) — the search engines were degraded, so this query was never tested",
      // An unrecognised class adds no clause rather than a guessed one.
      "Added 0 sources (7 admitted so far)",
    ]);
    const rendered = items.map((i) => `${i.label} ${i.detail ?? ""}`).join("\n");
    for (const token of ["no_hits", "duplicates_or_filtered", "provider_degraded", "yield_reason"]) {
      expect(rendered).not.toContain(token);
    }
  });

  it("says pages were found without a number when the trace carried no rollup", () => {
    const items = deriveLiveTrace(
      asEvents(observationFor("s1", "extraction_failure", { retrieval_trace: {} })),
      "RUNNING",
    );
    expect(items[0]?.detail).toBe(
      "Added 0 sources (7 admitted so far) — pages found, none could be read",
    );
  });
});

// ---------------------------------------------------------------------------
// F7 item 2 — a query the freshness wall matched and issued anyway.
//
// `narrowed-search-action.json` is FROZEN from the shipping producer:
// lane T3's pass-3 run replayed query by query through the real
// `partition_fresh_queries`, whose second narrowing is a query the model really
// proposed — the same EIA query as an earlier turn's with "2023" added — and
// the payload built by `_search_turn._narrowing_fields`. Pins the TS mirror's
// two new fields against what Python actually emits.
// ---------------------------------------------------------------------------

describe("deriveLiveTrace — a narrowed query says what it narrowed", () => {
  const narrowedSearch = {
    kind: "action",
    id: "narrowed-1",
    thought: "",
    tool_call: NARROWED_SEARCH,
  };

  it("names the tested query it narrows and the scope it added", () => {
    const items = deriveLiveTrace(asEvents([narrowedSearch]), "RUNNING");
    expect(items[0]?.label).toBe(
      'Searching: "EIA Cost and Performance Characteristics 2023 gas combustion turbine overnight capital cost $/kW peaker" — narrows "EIA overnight capital cost gas combustion turbine peaker 2024 Cost and Performance Characteristics" (adds 2023)',
    );
  });

  it("lists every scoping term the wall recorded", () => {
    const items = deriveLiveTrace(
      asEvents([
        {
          ...narrowedSearch,
          tool_call: {
            ...NARROWED_SEARCH,
            arguments: { ...NARROWED_SEARCH.arguments, narrowing: ["2024", "site:eia.gov"] },
          },
        },
      ]),
      "RUNNING",
    );
    expect(items[0]?.label).toContain("(adds 2024, site:eia.gov)");
  });

  it("says only what it narrowed when the wall recorded no term", () => {
    const items = deriveLiveTrace(
      asEvents([
        {
          ...narrowedSearch,
          tool_call: {
            ...NARROWED_SEARCH,
            arguments: { ...NARROWED_SEARCH.arguments, narrowing: [] },
          },
        },
      ]),
      "RUNNING",
    );
    expect(items[0]?.label).toContain('— narrows "EIA overnight capital cost');
    expect(items[0]?.label).not.toContain("(adds");
  });

  it("adds nothing to an ordinary search", () => {
    const items = deriveLiveTrace(asEvents(ZERO_YIELD_AND_RERUN), "RUNNING");
    expect(items.every((i) => !i.label.includes("narrows"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// deriveStats — the strip's refusal count.
// ---------------------------------------------------------------------------

describe("deriveStats — refusals counted beside searches", () => {
  it("counts the real turn's two refusals and its one search", () => {
    const stats = deriveStats(asEvents(REFUSED_TURN));
    expect(stats.refusals).toBe(2);
    expect(stats.searches).toBe(1);
  });

  it("is zero on a run that never had one", () => {
    expect(deriveStats(asEvents([BRIEF_ACTION])).refusals).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// deriveSourceTiers — cited / reviewed / discovered split.
// ---------------------------------------------------------------------------

describe("deriveSourceTiers — three-tier corpus split", () => {
  it("empty tiers when there is no report", () => {
    expect(deriveSourceTiers(null)).toEqual({
      cited: [],
      reviewed: [],
      discovered: [],
      notKept: null,
    });
  });

  // F7 item 5: the panel counted the rows it received. On lane T2's exhaustive
  // proof the engine had trimmed 1,838 discovery rows down to 744 to fit the
  // 1 MiB event and said so on the event; the panel listed its rows and said
  // nothing about the 1,094 it never got.
  it("reads what the trimmed report says it did not keep", () => {
    const report = {
      ...REPORT_EVENT,
      // The real meta from T2's exhaustive run
      // (lanes/T2-evidence/live-exhaustive/run-01, report event).
      meta: {
        report_evidence_compacted: true,
        reviewed_passages_total: 79,
        all_hits_total: 1838,
      },
    } as unknown as ReportEvent;
    expect(deriveSourceTiers(report).notKept).toEqual({
      found: 1838,
      kept: REPORT_EVENT.all_hits.length,
    });
  });

  it("says nothing when the report carried its whole corpus", () => {
    // T3's pass-3 report IS compacted — excerpts were trimmed — but every
    // discovery row survived (620 of 620), so there is nothing to disclose.
    const report = {
      ...REPORT_EVENT,
      meta: {
        report_evidence_compacted: true,
        all_hits_total: REPORT_EVENT.all_hits.length,
      },
    } as unknown as ReportEvent;
    expect(deriveSourceTiers(report).notKept).toBeNull();
  });

  it("says nothing when the report carries no compaction meta at all", () => {
    expect(deriveSourceTiers(REPORT_EVENT as unknown as ReportEvent).notKept).toBeNull();
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
