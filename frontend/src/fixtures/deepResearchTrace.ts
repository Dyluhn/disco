/**
 * Deep Research fixture trace — drives the offline UI (no live backend
 * needed). Mirrors the live event stream the agent-server produces:
 *
 *   USER message → status RUNNING → PlanEvent → AWAITING_PLAN_APPROVAL
 *     [user approves]
 *   → status RUNNING (plan_approved) → ActionEvents (phase, search,
 *     synthesize_section, …) + ObservationEvents → ReportEvent →
 *     status FINISHED
 *
 * Includes a bounded_by run (3 of 6 sub-questions covered) to demonstrate
 * the honest-failure surface end-to-end in the screenshots.
 */

import type {
  ActionEvent,
  AgentEvent,
  ConversationState,
  MessageEvent,
  ObservationEvent,
  PlanEvent,
  ReportEvent,
  StatusEvent,
} from "@/types/agent";

export const FIXTURE_DEEP_CID = "conv_deep_fixture";
export const FIXTURE_DEEP_QUERY =
  "What is the current state of solid-state battery commercialization?";

const NOW = "2026-06-06T12:00:00Z";

const userMsg: MessageEvent = {
  id: "evt_user",
  kind: "message",
  source: "user",
  seq: 1,
  timestamp: NOW,
  message: { role: "user", content: FIXTURE_DEEP_QUERY },
};

const statusRunning: StatusEvent = {
  id: "evt_run_1",
  kind: "status",
  source: "system",
  seq: 2,
  timestamp: NOW,
  status: "RUNNING",
};

export const fixturePlan: PlanEvent = {
  id: "evt_plan",
  kind: "plan",
  source: "agent",
  seq: 3,
  timestamp: NOW,
  summary:
    "Multi-section research report on the current state of solid-state battery commercialization. Will gather sources across 6 sub-questions (tier: standard_deep; cap: 40 sources, 4 rounds/subq).",
  steps: [
    { title: "Which solid-state battery products are in mass or pilot production today?" },
    { title: "What are the primary technical and manufacturing bottlenecks?" },
    { title: "How do projected costs per kWh compare to lithium-ion?" },
    { title: "Which OEMs have confirmed near-term integration timelines?" },
    { title: "What is the competitive landscape (Toyota, QuantumScape, Solid Power, CATL)?" },
    { title: "What policy, supply chain, and regulatory headwinds exist?" },
  ],
  revision: 1,
  context:
    "## Approach\n\nDecomposed query into 6 sub-questions prioritized by relevance — products, bottlenecks, and economics first; competitive landscape and policy last (background context).",
};

const planGate: StatusEvent = {
  id: "evt_gate",
  kind: "status",
  source: "system",
  seq: 4,
  timestamp: NOW,
  status: "AWAITING_PLAN_APPROVAL",
  detail: "evt_plan",
};

/** Events emitted between plan-approval and the final report. */
const approved: StatusEvent = {
  id: "evt_approved",
  kind: "status",
  source: "system",
  seq: 5,
  timestamp: NOW,
  status: "RUNNING",
  detail: "plan_approved",
};

function action(
  id: string,
  seq: number,
  tool: string,
  args: Record<string, unknown>,
): ActionEvent {
  return {
    id,
    kind: "action",
    source: "agent",
    seq,
    timestamp: NOW,
    thought: `Deep Research: ${tool}`,
    tool_call: { tool_name: tool, arguments: args },
  };
}

function observation(
  id: string,
  seq: number,
  actionId: string,
  toolName: string,
  ok: boolean,
  structured: Record<string, unknown>,
): ObservationEvent {
  return {
    id,
    kind: "observation",
    source: "environment",
    seq,
    timestamp: NOW,
    action_id: actionId,
    tool_result: {
      call_id: `call_${id}`,
      tool_name: toolName,
      success: ok,
      content: JSON.stringify(structured),
      structured,
    },
  };
}

const subq1 = fixturePlan.steps[0].title;
const subq2 = fixturePlan.steps[1].title;
const subq3 = fixturePlan.steps[2].title;

/** The events streamed during the live-progress phase. */
export const fixtureRunningEvents: AgentEvent[] = [
  userMsg,
  statusRunning,
  fixturePlan,
  planGate,
  approved,
  action("evt_phase_gather", 10, "phase", { phase: "gather", subquestions: 6 }),
  action("evt_search_1_1", 11, "search", {
    subquestion: subq1,
    query: "solid-state battery mass production 2026",
    round: 1,
    rounds_max: 4,
  }),
  observation("evt_obs_1_1", 12, "evt_search_1_1", "observation", true, {
    subquestion: subq1,
    round: 1,
    ok: true,
    added: 8,
    total_for_subq: 8,
    remaining_budget: 32,
  }),
  action("evt_gap_1", 13, "phase", {
    subquestion: subq1,
    sufficient: false,
    rationale: "missing OEM timeline specifics",
    follow_ups: ["SK On Lexus Mercedes solid-state timeline"],
  }),
  action("evt_search_1_2", 14, "search", {
    subquestion: subq1,
    query: "SK On Lexus Mercedes solid-state timeline",
    round: 2,
    rounds_max: 4,
  }),
  observation("evt_obs_1_2", 15, "evt_search_1_2", "observation", true, {
    subquestion: subq1,
    round: 2,
    ok: true,
    added: 6,
    total_for_subq: 14,
    remaining_budget: 26,
  }),
  action("evt_synth_1", 20, "synthesize_section", {
    section: subq1,
    passages_used: 6,
  }),
  action("evt_search_2_1", 30, "search", {
    subquestion: subq2,
    query: "solid-state battery interface stability dendrite suppression",
    round: 1,
    rounds_max: 4,
  }),
  observation("evt_obs_2_1", 31, "evt_search_2_1", "observation", true, {
    subquestion: subq2,
    round: 1,
    ok: true,
    added: 5,
    total_for_subq: 5,
    remaining_budget: 21,
  }),
  action("evt_synth_2", 35, "synthesize_section", {
    section: subq2,
    passages_used: 5,
  }),
  action("evt_search_3_1", 40, "search", {
    subquestion: subq3,
    query: "solid-state battery cost per kWh manufacturing scale economy",
    round: 1,
    rounds_max: 4,
  }),
  observation("evt_obs_3_1", 41, "evt_search_3_1", "observation", true, {
    subquestion: subq3,
    round: 1,
    ok: true,
    added: 5,
    total_for_subq: 5,
    remaining_budget: 16,
  }),
  action("evt_synth_3", 50, "synthesize_section", {
    section: subq3,
    passages_used: 5,
  }),
  action("evt_phase_synth", 55, "phase", { phase: "synthesize", sections: 3, passages_total: 30 }),
  action("evt_phase_coher", 60, "phase", { phase: "coherence" }),
];

/** The final ReportEvent emitted after the synthesis phase completes. */
export const fixtureReport: ReportEvent & { claims: import("@/types/grounded").VerifiedClaim[] } = {
  id: "evt_report",
  kind: "report",
  source: "agent",
  seq: 70,
  timestamp: NOW,
  query: FIXTURE_DEEP_QUERY,
  summary:
    "As of early 2026, solid-state battery (SSB) commercialization remains in the pilot production phase, with no major manufacturer having achieved full-scale mass production for mainstream automotive applications. Despite industry narratives emphasizing a rapid transition from R&D, the sector has not yet moved beyond limited-scale manufacturing.\n\nThis delay is driven by a triad of interdependent technical barriers: interfacial instability, dendritic growth, and the absence of scalable manufacturing processes. Economically, SSBs are currently uncompetitive with conventional lithium-ion standards, with manufacturing costs estimated at three to five times higher.",
  claims: [
    {
      claim: { text: "SSB technology is currently confined to pilot production phases.", cited_passage_ids: ["2f1033_p0"] },
      verdict: "supported" as const,
      best_passage_id: "2f1033_p0",
      entailment_score: 0.92,
    },
    {
      claim: { text: "SK On has opened a 4,600-square-meter pilot plant in Daejeon.", cited_passage_ids: ["12afc7_p1"] },
      verdict: "supported" as const,
      best_passage_id: "12afc7_p1",
      entailment_score: 0.95,
    },
    {
      claim: { text: "Mass production is largely projected to occur around 2030.", cited_passage_ids: ["bbeab3_p1"] },
      verdict: "supported" as const,
      best_passage_id: "bbeab3_p1",
      entailment_score: 0.88,
    },
    {
      claim: { text: "Commercialization timelines have been clarified by recent mass-production schedules.", cited_passage_ids: ["12afc7_p0"] },
      verdict: "weak" as const,
      best_passage_id: "12afc7_p0",
      entailment_score: 0.51,
    },
    {
      claim: { text: "Dendrite suppression has been fully solved in mass-production environments.", cited_passage_ids: ["258fcf_p2"] },
      verdict: "unsupported" as const,
      best_passage_id: "258fcf_p2",
      entailment_score: 0.07,
    },
  ],
  sections: [
    {
      id: "s0",
      title: subq1,
      markdown:
        "As of early 2026, the solid-state battery (SSB) sector remains predominantly in the pilot production phase, with no major manufacturer having achieved full-scale mass production for mainstream automotive applications [[2f1033_p0]]. SK On has opened a 4,600-square-meter pilot plant in Daejeon, South Korea, which serves as a proving ground for its all-solid-state battery technology rather than a mass-production line [[12afc7_p1]].\n\nCommercialization timelines announced by manufacturers cluster around the late 2020s. SK On has announced a target of 2029 for market-ready batteries, accelerated a full year ahead of its previous projections [[12afc7_p0]]. However, broader industry analysis suggests that while pilots and initial commercialization efforts are expected between 2027 and 2028, mass production is largely projected to occur around 2030 [[bbeab3_p1]].\n\nTaken together, the evidence suggests that while pilot-scale manufacturing is active and timelines are being accelerated by key players like SK On, the industry has not yet reached the threshold of mass production for mainstream vehicles [[12afc7_p0]] [[bbeab3_p1]].",
      cited_passage_ids: ["2f1033_p0", "12afc7_p0", "12afc7_p1", "bbeab3_p1"],
      confidence: "mixed",
      disputed_notes: [
        "Sources disagree on near-term integration: some observers cite recent mass-production schedules as clarifying, while others urge close scrutiny of 'production-ready' claims.",
      ],
      unsupported_count: 1,
    },
    {
      id: "s1",
      title: subq2,
      markdown:
        "The commercialization of solid-state batteries is constrained by a triad of interdependent technical barriers: interfacial instability, dendritic growth, and the lack of scalable manufacturing processes [[10cd08_p2]]. Interface stability is critical for preventing degradation, yet specific solutions remain an active area of investigation rather than a solved engineering problem [[7b9306_p3]].\n\nDendrite formation remains a persistent safety and longevity risk [[27f6e7_p0]]. While some experimental approaches such as silver-ion mediation claim to guide more uniform lithium deposition, these results have not yet been validated in mass-production environments [[258fcf_p2]].\n\nTaken together, the evidence suggests that while individual components of solid-state battery technology are advancing, the integration of these components into a commercially viable product remains elusive.",
      cited_passage_ids: ["10cd08_p2", "7b9306_p3", "27f6e7_p0", "258fcf_p2"],
      confidence: "mixed",
      disputed_notes: [],
      unsupported_count: 0,
    },
    {
      id: "s2",
      title: subq3,
      markdown:
        "As of early 2026, solid-state batteries remain significantly more expensive than conventional lithium-ion counterparts, with manufacturing costs estimated at three to five times higher [[2f1033_p0]]. This cost disparity persists despite SSBs offering nearly double the energy density of advanced lithium-ion cells, reaching over 500 Wh/kg [[2f1033_p0]].\n\nThe path to price parity hinges on overcoming substantial manufacturing hurdles. Unlike liquid-electrolyte lithium-ion batteries, SSBs require novel fabrication techniques for solid ceramic or polymer conductors [[2f1033_p1]]. Industry reports emphasize that until battery pack costs are reduced through breakthroughs in manufacturing efficiency, the shift to battery electric vehicles will continue to face headwinds [[791a48_p0]].",
      cited_passage_ids: ["2f1033_p0", "2f1033_p1", "791a48_p0"],
      confidence: "high",
      disputed_notes: [],
      unsupported_count: 0,
    },
  ],
  passages: [
    {
      id: "2f1033_p0",
      source_url: "https://energy-solutions.co/articles/sub/solid-state-batteries-vs-lithium-ion-2026",
      source_title: "Solid-State Batteries vs Lithium-Ion 2026",
      text: "Manufacturing costs for solid-state batteries are estimated at three to five times higher than current lithium-ion standards. SSB technology is currently confined to pilot production phases.",
    },
    {
      id: "2f1033_p1",
      source_url: "https://energy-solutions.co/articles/sub/solid-state-batteries-vs-lithium-ion-2026",
      source_title: "Solid-State Batteries vs Lithium-Ion 2026",
      text: "SSBs require novel fabrication techniques for solid ceramic or polymer conductors. Achieving cost competitiveness requires not just volume, but specific economies of scale.",
    },
    {
      id: "12afc7_p0",
      source_url:
        "https://www.batterytechonline.com/automotive-mobility/sk-on-accelerates-solid-state-ev-battery-timeline-targets-2029-commercialization",
      source_title: "SK On Speeds Up Solid-State Battery Timeline",
      text: "SK On has announced a target of 2029 for market-ready solid-state batteries, a full year ahead of its previous projections.",
    },
    {
      id: "12afc7_p1",
      source_url:
        "https://www.batterytechonline.com/automotive-mobility/sk-on-accelerates-solid-state-ev-battery-timeline-targets-2029-commercialization",
      source_title: "SK On Speeds Up Solid-State Battery Timeline",
      text: "SK On has opened a 4,600-square-meter pilot plant in Daejeon, South Korea, serving as a proving ground for all-solid-state battery technology.",
    },
    {
      id: "bbeab3_p1",
      source_url:
        "https://www.batterytechonline.com/market-analysis/production-timelines-for-14-upcoming-solid-state-batteries",
      source_title: "Production Timelines for 14 Solid-State Batteries",
      text: "Mass production for mainstream automotive applications is largely projected to occur around 2030.",
    },
    {
      id: "10cd08_p2",
      source_url: "https://www.idtechex.com/en/research-report/solid-state-batteries/1130",
      source_title: "Solid-State Batteries 2026-2036: IDTechEx",
      text: "High costs and manufacturing challenges have led some analysts to view the technology as overhyped relative to its current readiness.",
    },
    {
      id: "7b9306_p3",
      source_url:
        "https://www.patsnap.com/resources/blog/articles/solid-state-sodium-batteries-overcoming-barriers/",
      source_title: "Solid-State Sodium Battery Challenges 2026",
      text: "Maintaining stable interfaces is critical for preventing degradation, yet specific solutions remain an active area of investigation.",
    },
    {
      id: "27f6e7_p0",
      source_url:
        "https://www.facebook.com/unboxingenergy/posts/dendrite-challenge",
      source_title: "Battery researchers reducing dendrite challenges",
      text: "Dendrite formation remains a persistent safety and longevity risk, with microscopic structures growing during charging and increasing the likelihood of internal short circuits.",
    },
    {
      id: "258fcf_p2",
      source_url: "https://www.mdpi.com/2313-0105/11/8/304",
      source_title: "Dendrite Suppression Strategies for Solid-State Batteries",
      text: "While some experimental approaches such as silver-ion mediation claim to guide more uniform lithium deposition, these results have not yet been validated in mass-production environments.",
    },
    {
      id: "791a48_p0",
      source_url:
        "https://www.mckinsey.com/features/mckinsey-center-for-future-mobility/our-insights/the-future-of-affordable-evs-breakthroughs-in-battery-pack-costs",
      source_title: "McKinsey: Future EVs and battery pack costs",
      text: "Until battery pack costs are reduced through breakthroughs in manufacturing efficiency, the shift to battery electric vehicles will continue to face headwinds.",
    },
  ],
  all_hits: [
    // Cited URLs
    {
      url: "https://energy-solutions.co/articles/sub/solid-state-batteries-vs-lithium-ion-2026",
      title: "Solid-State Batteries vs Lithium-Ion 2026",
      snippet: "...",
      status: "ok",
      source_engine: "searxng",
      rank: 1,
    },
    {
      url: "https://www.batterytechonline.com/automotive-mobility/sk-on-accelerates-solid-state-ev-battery-timeline-targets-2029-commercialization",
      title: "SK On Speeds Up Solid-State Battery Timeline",
      snippet: "...",
      status: "ok",
      source_engine: "searxng",
      rank: 2,
    },
    // Reviewed-but-uncited
    {
      url: "https://www.naccon.com/Comprehensive-Guide-to-Solid-State-Batteries",
      title: "Comprehensive Guide to Solid-State Batteries",
      snippet: "...",
      status: "ok",
      source_engine: "searxng",
      rank: 3,
    },
    {
      url: "https://www.energytrend.com/news/20240716-47888.html",
      title: "20 companies' solid-state battery mass production timetable",
      snippet: "...",
      status: "ok",
      source_engine: "searxng",
      rank: 4,
    },
    {
      url: "https://www.neware.net/news/solid-state-battery/230/63.html",
      title: "Solid State Battery: Comprehensive Introduction",
      snippet: "...",
      status: "ok",
      source_engine: "searxng",
      rank: 5,
    },
    // Discovered (failed)
    {
      url: "https://patents.google.com/some-paywalled-paper",
      title: "Paywalled IP: solid-state architecture",
      snippet: "...",
      status: "paywalled",
      source_engine: "searxng",
      rank: 6,
    },
    {
      url: "https://blocked.example.com/article",
      title: "Blocked source",
      snippet: "",
      status: "blocked",
      source_engine: "searxng",
      rank: 7,
    },
    {
      url: "https://404.example.com/missing",
      title: "Missing source",
      snippet: "",
      status: "not_found",
      source_engine: "searxng",
      rank: 8,
    },
  ],
  unsupported_count: 1,
  bounded_by: "rounds",
  depth_tier: "standard_deep",
};

const statusFinished: StatusEvent = {
  id: "evt_finished",
  kind: "status",
  source: "system",
  seq: 80,
  timestamp: NOW,
  status: "FINISHED",
};

/** The full canned trace from start to FINISHED — for tests that need the
 * whole conversation as one array. */
export const fixtureFullTrace: AgentEvent[] = [
  ...fixtureRunningEvents,
  fixtureReport,
  statusFinished,
];

/** Initial state snapshot the WS sends on connect. */
export const fixtureInitialState: ConversationState = {
  conversation_id: FIXTURE_DEEP_CID,
  execution_status: "IDLE",
  iteration: 0,
  max_iterations: 500,
  last_seq: 0,
  pending_action_id: null,
  pending_plan_id: null,
};

/** State after FINISHED — used by tests that want to drop straight into the
 * finished view without replaying the live trace. */
export const fixtureFinishedState: ConversationState = {
  conversation_id: FIXTURE_DEEP_CID,
  execution_status: "FINISHED",
  iteration: 6,
  max_iterations: 500,
  last_seq: 80,
  pending_action_id: null,
  pending_plan_id: null,
};
