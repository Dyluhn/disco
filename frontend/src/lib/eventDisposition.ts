/**
 * Event-render disposition contract (the durable fix for the "backend emits an
 * event the UI silently drops" class — see the follow-up-answer bug).
 *
 * The backend (packages/core/src/disco/core/events.py :: EventKind) can append
 * any of these kinds to the conversation event log. The frontend renders the log
 * by projecting events to UI. The historical failure: a renderer typed to ONE
 * shape (e.g. DeepReportView → ReportEvent) silently dropped every other kind, so
 * a server-generated event (the RP-13 follow-up answer, a MessageEvent) never
 * appeared on screen.
 *
 * This module makes the seam EXPLICIT and ENFORCED: every backend EventKind MUST
 * have a conscious disposition here —
 *   "rendered"   → some surface has a render path for it (named in `where`)
 *   "suppressed" → deliberately NOT shown to the user (internal plumbing), with a reason
 * There is no third "silently dropped" state: a kind that isn't listed fails the
 * contract test (eventDisposition.test.ts) AND the cross-language drift test
 * (packages/core/tests/test_event_kind_frontend_contract.py), so a new backend
 * kind cannot ship without a deliberate UI decision.
 *
 * IMPORTANT: KNOWN_EVENT_KINDS must stay in lockstep with the backend EventKind
 * enum. The Python contract test asserts they are equal — if the backend adds or
 * removes a kind, that test goes red until this file is updated.
 */

export type EventDisposition = "rendered" | "suppressed";

/** The authoritative set of backend event kinds (mirror of EventKind in
 *  events.py). The Python contract test pins this against the enum. */
export const KNOWN_EVENT_KINDS = [
  "message",
  "action",
  "observation",
  "agent_error",
  "condensation",
  "status",
  "workspace_restored",
  "error",
  "plan",
  "report",
  "alternatives",
  "knowledge",
  "datasource",
  "deliverable",
  "verifier_started",
  "verifier_verdict",
  "verifier_shadow",
  "schedule",
  "schedule_run",
  "clarify",
  "questions_v2",
  "context_resolved",
  "context_summary",
] as const;

export type KnownEventKind = (typeof KNOWN_EVENT_KINDS)[number];

/** Each backend event kind's conscious UI disposition. `where` documents the
 *  render path (for "rendered") or the suppression reason (for "suppressed"). */
export const EVENT_DISPOSITION: Record<
  KnownEventKind,
  { disposition: EventDisposition; where: string }
> = {
  message: {
    disposition: "rendered",
    where:
      "Build/Agent ActivityFeed (agent narration) + Deep Research follow-up Q&A thread (DeepResearchSurface). User/assistant only; environment/system-reminder messages are suppressed at the projection.",
  },
  action: { disposition: "rendered", where: "Build/Agent ActivityFeed (tool calls). DR loop actions (phase/search/section_done) are suppressed on the research surface as plumbing." },
  observation: { disposition: "rendered", where: "Build/Agent ActivityFeed (tool results, screenshots)." },
  agent_error: { disposition: "rendered", where: "Build/Agent ActivityFeed error rows + status." },
  status: { disposition: "rendered", where: "AgentStatusBar / lifecycle state machine across all surfaces." },
  workspace_restored: {
    disposition: "suppressed",
    where: "Workspace rollback audit marker. Version history UI will surface it from the versions API, not as a chat turn.",
  },
  error: { disposition: "rendered", where: "Conversation-level error surface (states.tsx error card)." },
  plan: { disposition: "rendered", where: "PlanPanel (Build + Deep Research plan gate)." },
  report: { disposition: "rendered", where: "DeepReportView (Deep Research)." },
  alternatives: { disposition: "rendered", where: "AlternativesGate (Build/Agent, after repeated tool failure)." },
  deliverable: { disposition: "rendered", where: "DeliverablePanel (Build finished-artifact handoff)." },
  clarify: { disposition: "rendered", where: "ClarifyPanel (pre-plan clarification questions)." },
  questions_v2: {
    disposition: "rendered",
    where: "QuestionsV2Panel (structured pre-plan intake form).",
  },
  schedule: { disposition: "rendered", where: "Settings → Schedules section / schedule confirmation card." },
  schedule_run: {
    disposition: "rendered",
    where: "Activity view (a scheduled run firing appends to the conversation it points at).",
  },
  condensation: {
    disposition: "suppressed",
    where: "Internal context-compaction marker (View tombstone). Never user-facing — it would leak the harness's memory mechanics.",
  },
  knowledge: {
    disposition: "suppressed",
    where: "Internal pinned best-practice snippet injected into the model's context (Cluster 7). Not a user card. (Candidate for a future 'pinned facts' affordance.)",
  },
  datasource: {
    disposition: "suppressed",
    where: "Internal durable API/schema docs, condensation-immune (Cluster 7). Model-context only, not a user card.",
  },
  verifier_started: {
    disposition: "suppressed",
    where: "REL-1 host verifier audit marker. Not a user card and never model-facing.",
  },
  verifier_verdict: {
    disposition: "suppressed",
    where: "REL-1 host verifier verdict marker. Later projections may update artifact state; the event itself is internal.",
  },
  verifier_shadow: {
    disposition: "suppressed",
    where: "REL-1 shadow-mode inline-vs-host verifier comparison. Internal rollout evidence only.",
  },
  context_resolved: {
    disposition: "suppressed",
    where: "CXT-3 internal deferred-snip mark (agent resolves a context range). Never user-facing — harness memory mechanics.",
  },
  context_summary: {
    disposition: "suppressed",
    where: "CXT-3 internal record that a durable summary was written for a resolved range. Model-context/audit only, not a user card.",
  },
};
