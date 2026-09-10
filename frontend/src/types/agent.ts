/**
 * Agent (Build) surface types — the TS mirror of the core event/state contract
 * (event-state-contract §2/§3) as it crosses the conversation WebSocket. The Build
 * surface consumes the AGENT LOOP's event log (/ws/conversations/{id}), distinct from
 * Research's ephemeral token stream: here the EVENTS are the source of truth.
 */

import type { SelectionRef } from "@/lib/selectionBridge";
import type { VerifiedClaim } from "@/types/grounded";

export type ConversationStatus =
  | "IDLE" | "RUNNING" | "PAUSED" | "STUCK"
  | "WAITING_FOR_CONFIRMATION" | "AWAITING_PLAN_APPROVAL"
  | "AWAITING_USER_DECISION" | "AWAITING_USER_QUESTION"
  | "FINISHED" | "ERROR";

export type SecurityRisk = "UNKNOWN" | "LOW" | "MEDIUM" | "HIGH";
export type EventSource = "user" | "agent" | "environment" | "system";

/** The analyzer's verdict, carried on a proposed action's `meta.risk_assessment`. */
export interface RiskAssessment {
  risk: SecurityRisk;
  rationale: string;
  analyzer: string;
  self_assessed?: SecurityRisk;
}

export interface ToolCall {
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResult {
  /** Correlates the result back to its ToolCall (contract §2.3, VOLATILE).
   *  The backend always emits it; the UI treats it as optional metadata. */
  call_id?: string;
  tool_name: string;
  success: boolean;
  content: string;
  structured?: Record<string, unknown> | null;
  error?: string | null;
}

interface EventBase {
  id: string;
  seq?: number | null;
  source?: EventSource;
  agent_view_id?: string | null;
  /** ISO-8601 instant the event was produced (contract §2.1 BaseEvent.timestamp,
   *  VOLATILE). Sent on every event; the UI doesn't rely on it for ordering
   *  (seq is authoritative) so it's optional here. */
  timestamp?: string;
  /** Free-form, non-semantic metadata (contract §2.1 BaseEvent.meta, VOLATILE —
   *  tracing ids, UI hints such as blocked-landing / driver-outage labels).
   *  Every wire event carries it; never used for reconstruction. */
  meta?: Record<string, unknown>;
}

export interface MessageEvent extends EventBase {
  kind: "message";
  message: { role: string; content: string };
}
export interface ActionEvent extends EventBase {
  kind: "action";
  thought: string;
  tool_call: ToolCall | null;
  self_assessed_risk?: SecurityRisk;
  meta?: { risk_assessment?: RiskAssessment } & Record<string, unknown>;
}
export interface ObservationEvent extends EventBase {
  kind: "observation";
  tool_result: ToolResult;
  action_id: string;
}
export interface AgentErrorEvent extends EventBase {
  kind: "agent_error";
  error: string;
  detail?: string | null; // [REL-RC-E] tool recovery guidance (capped); `error` stays the code
  action_id?: string | null;
}
export interface CondensationEvent extends EventBase {
  kind: "condensation";
  summary: string;
  forgotten_start_seq: number;
  forgotten_end_seq: number;
}
export interface StatusEvent extends EventBase {
  kind: "status";
  status: ConversationStatus;
  detail?: string | null;
  host_mutation_id?: string | null;
  run_intent_id?: string | null;
}
export interface WorkspaceVersionEvent extends EventBase {
  kind: "workspace_version";
  version_seq: number;
  tree_digest: string;
  trigger: string;
  final_seal?: {
    schema_version: 1;
    scope: { namespace: string; identifier: string };
    terminal_seq: number;
    latest_effect_seq: number | null;
    version_seq: number;
    tree_digest: string;
    file_count: number;
    total_bytes: number;
  } | null;
}
export interface WorkspaceRestoredEvent extends EventBase {
  kind: "workspace_restored";
  version_seq: number;
  tree_digest: string;
  label: string;
}
export interface WorkspaceMutationEvent extends EventBase {
  kind: "workspace_mutation";
  operation: string;
  paths: string[];
  run_intent_id?: string | null;
  run_protocol_version?: 1 | null;
}
export interface BuildPlatformAdmissionEvent extends EventBase {
  kind: "build_platform_admission";
  route: "legacy" | "platform";
  profile_id: string;
  run_intent_id: string;
  composition_authority: "legacy" | "build_platform_core";
  execution_bridge: "legacy_host";
  composition_digest?: string | null;
  run_identity?: string | null;
  transition?: "initial" | "appkit_ejection";
  supersedes_admission_id?: string | null;
}
export interface AppKitEjectionEvent extends EventBase {
  kind: "appkit_ejection";
  action_id: string;
  tool_call_id: string;
  source_profile_id: "disco.appkit_web@1";
  target_profile_id: "disco.freeform_web@1";
  source_version_seq: number;
  source_tree_digest: string;
  ejected_version_seq: number;
  ejected_tree_digest: string;
  lost_guarantees: string[];
  appkit_verified: false;
}
export interface PlanStep {
  title: string;
  detail?: string | null;
}
export interface PlanEvent extends EventBase {
  kind: "plan";
  summary: string;
  steps: PlanStep[];
  revision: number;
  /** Optional markdown rationale + exploration findings (the WHY behind the WHAT). */
  context?: string;
}
/** Which boundary failed. Closed set, mirrored from `core.RunFailureClass`;
 *  every member is produced by a real code path that raised — never inferred
 *  from an error string. */
export type RunFailureClass =
  | "search_infrastructure"
  | "extraction_infrastructure"
  | "no_usable_evidence"
  | "host_circuit_breaker"
  | "model_protocol"
  | "model_provider"
  | "preflight"
  | "internal_error";

/** A terminal run failure as fields rather than prose: why it happened, what
 *  state the run is in now, the exact next action, and what remains available.
 *  `ErrorEvent.detail` still carries the identical sentence. */
export interface RunFailure {
  failure_class: RunFailureClass;
  why: string;
  state: string;
  next: string;
  allowed: string;
}

export interface ErrorEvent extends EventBase {
  kind: "error";
  code?: string;
  detail?: string | null;
  /** Present when the producing code path could name its own failure; absent
   *  on every event written before this field existed. */
  failure?: RunFailure | null;
}

/** One section of a Deep Research report — body is markdown with [[passage_id]]
 * inline citations the existing CitedText parser already resolves. The
 * confidence rollup + disputed_notes are the engine's honesty-at-scale surface. */
export interface ReportSection {
  id: string;
  title: string;
  markdown: string;
  cited_passage_ids: string[];
  confidence: "high" | "mixed" | "low";
  disputed_notes: string[];
  unsupported_count: number;
}

/** The finished Deep Research report — multi-section, grounded synthesis. A
 * report is never a placeholder: the backend requires a summary and sections. */
export interface ReportEvent extends EventBase {
  kind: "report";
  query: string;
  summary: string;
  sections: ReportSection[];
  passages: Array<Record<string, unknown>>;
  reviewed_passages?: Array<Record<string, unknown>>;
  all_hits: Array<Record<string, unknown>>;
  unsupported_count: number;
  /** Which hard budget closed research: "turns" (the work budget), "sources",
   *  "stopped", or the depth-only "rounds". Conversations persisted before the
   *  budget became work-denominated carry the legacy "wall_clock"; it is still
   *  accepted and rendered, but no new run emits it. Left open (`string`)
   *  because the backend owns the vocabulary and stored events outlive it. */
  bounded_by: string | null;
  depth_tier: string | null;
  /** Optional per-claim verification verdicts. Absent on base-engine reports
   *  (a section carries only `unsupported_count`, not a per-claim breakdown);
   *  present when a verification overlay / the demo fixture supplies them, and
   *  rendered by the report's claim-verdicts surface when set. */
  claims?: VerifiedClaim[];
  /** `meta.report_evidence_compacted` / `.reviewed_passages_total` /
   *  `.all_hits_total` ride the inherited free-form `meta`, read with a runtime
   *  check (`deepResearchTrace.discoveryNotKept`) as `meta.turn_accounting` is.
   *  Undeclared: this module is at its 500-line cap (F7). */
}

/** Resumable evidence state emitted when research pauses before writing a
 * report. This is deliberately a separate event kind so the UI cannot render
 * an empty report as if it were a finished artifact. */
export interface ResearchCheckpointEvent extends EventBase {
  kind: "research_checkpoint";
  query: string;
  passages: Array<Record<string, unknown>>;
  all_hits: Array<Record<string, unknown>>;
  trail: Array<Record<string, unknown>>;
  completed_queries: string[];
  depth_tier: string | null;
  recency_window: "week" | "month" | null;
}

/* ---- Deep Research progress payloads (L23/L24 event contract) --------------
 *
 * These are NOT event kinds. Every one arrives as an ordinary ActionEvent
 * (`kind: "action"`) whose `tool_call.tool_name` is the action name below and
 * whose `tool_call.arguments` is the payload. The wire is untyped
 * (`Record<string, unknown>`), so these interfaces are the SHAPE CONTRACT the
 * parsers in `lib/deepResearchTraceParts/activity.ts` validate against — a
 * payload that doesn't match is dropped, never half-rendered.
 *
 * The rule this contract exists to enforce: the run UI reports what the engine
 * said and when it said it. It never infers activity from `status`.
 */

/** Which part of a research turn the loop is in (`turn` action). */
export type ResearchTurnPhase = "planning" | "searching" | "extracting" | "thinking";

/** `turn` — emitted at each research-turn phase change. `n`/`of` is the same
 *  work-denominated turn budget the model itself sees. */
export interface ResearchTurnPayload {
  n: number;
  of: number;
  phase: ResearchTurnPhase;
  /** The subquestion this turn is working, when the loop named one. */
  subquestion?: string | null;
}

/** Which model call a `model_activity` heartbeat belongs to. Mirrors
 *  `retrieval.deep_research._progress_events.ModelActivityStage`. `follow_up`
 *  is a follow-up ANSWER call on a finished report — not part of the run, so
 *  only the follow-up indicator reads it (see `MODEL_STAGES` in
 *  `lib/deepResearchTraceParts/activity.ts`). */
export type ModelActivityStage =
  | "brief" | "research_turn" | "draft" | "review" | "rework"
  | "continuation" | "follow_up";

/** `model_activity` — emitted at most every ~5 s while a provider stream is
 *  open, plus once on close. This is the ONLY signal that distinguishes "the
 *  model is streaming slowly" from "nothing is happening". May be absent
 *  (a driver that streams without chunk visibility emits none); the UI renders
 *  it when present and never fabricates it. */
export interface ModelActivityPayload {
  stage: ModelActivityStage;
  tokens_streamed: number;
  /** Seconds since the CALL WAS MADE, measured by the backend at emit —
   *  prefill included (`core.llm.stream_progress.StreamProgress.seconds`). Not
   *  since the first chunk: on a slow local model that would report a
   *  four-minute call as a two-second one. */
  seconds: number;
  call_ordinal: number;
  /** The reasoning-channel share of `tokens_streamed`, when the provider
   *  exposed a separate reasoning channel. Absent otherwise (the backend omits
   *  the key at 0). Equal to `tokens_streamed` means the model is producing and
   *  none of it is visible output yet. */
  reasoning_tokens?: number;
  /** False on the ONE heartbeat a BUFFERED transport emits, at call start with
   *  nothing delivered (`core.llm.stream_progress.buffered_start`). The whole
   *  answer arrives in a single response, so no further heartbeat is coming and
   *  the silence after this one belongs to the transport, not the model. Absent
   *  on a streamed call (the backend omits the key when true). */
  streams?: boolean;
}

/** Why the loop is holding instead of searching. Both are infrastructure
 *  states, never failures. */
export type ResearchHoldReason = "search_pool_cooling" | "search_rate_starved";

export interface ResearchHoldEngine {
  name: string;
  /** ISO-8601 instant this engine's cooldown expires. */
  resume_at: string;
}

/** `hold` — the loop stopped issuing searches because every usable engine is
 *  cooling. It resumes itself; the user's choice is only whether to keep
 *  waiting or stop with what has been gathered. Re-emitted as the estimate
 *  is re-synced, so the newest one wins. */
export interface ResearchHoldPayload {
  reason: ResearchHoldReason;
  engines: ResearchHoldEngine[];
  /** ISO-8601 instant the loop will next probe the pool. */
  resume_at: string;
  sources_retained: number;
  turn: { n: number; of: number };
  /** The queries the loop is holding ON — the work it will issue the moment the
   *  pool comes back, verbatim, in the order it queued them. The producer
   *  (`_progress_events.emit_hold`) has always sent the query STRINGS; this
   *  mirror declared `number` and the parser coerced them to 0, which silently
   *  removed the whole "N queries waiting to run" clause from the hold panel. */
  queued_queries: string[];
}

/** `hold_resumed` — the pool came back and the loop is working again. */
export interface ResearchHoldResumedPayload {
  waited_s: number;
  engines_live: string[];
}

/** `review` / `rework` / `continuation` — round k of a bounded writer loop. */
export interface ResearchRoundPayload {
  k: number;
  of: number;
}

/** `stop_requested` — the user pressed Stop and the server set the run's cancel
 *  flag. Deliberately NON-terminal: the run is still working, and it writes its
 *  own ending (a `research_checkpoint` + PAUSED) when it reaches the next
 *  boundary that can honour the flag. The only fields are the two the server
 *  truthfully has; everything the wall says about the run's state comes from
 *  the run's own events, so the two can never disagree. */
export interface ResearchStopRequestedPayload {
  /** ISO-8601 instant the server recorded the request. */
  requested_at: string;
}

/** `follow_up_grounding` — what grounding did to ONE follow-up answer, appended
 *  immediately before the assistant message it describes. It exists so a reader
 *  can tell an answer from a refusal without matching refusal prose: `refused`
 *  is the server's own verdict, and `statements === supported + weak + removed`
 *  by construction. */
export interface ResearchFollowUpGroundingPayload {
  statements: number;
  supported: number;
  weak: number;
  /** Statements retention took out: unsupported by the report's own sources. */
  removed: number;
  /** True when nothing survived and the message that follows is a wall. */
  refused: boolean;
}

/** `phase` — the pre-existing writer/loop phase marker. Writer phases may
 *  carry the running word count of the stream that is producing the report. */
export interface ResearchPhasePayload {
  phase: string;
  words_streamed?: number;
}

/** `search` has no interface here — payload `{query, subquestion, round,
 *  origin}` plus, on a query the freshness wall matched and issued anyway,
 *  `narrowed_from` + `narrowing`. Undeclared for the same cap reason; read in
 *  `deepResearchTraceParts/liveTrace.ts::narrowsClause` and pinned to the real
 *  producer by `lib/__fixtures__/narrowed-search-action.json`. */

/** Why the host did not send one proposed query to the world. The audit row's
 *  own word, because the event is built FROM that row — the trail and the
 *  event can never disagree. `empty` carries no detail; the two duplicate
 *  classes name what they matched; `exhausted` is the only one that names an
 *  angle. */
export type QueryRefusedReason =
  | "repeat" | "near_duplicate" | "queued" | "retry_budget_spent"
  | "exhausted" | "empty";

/** `query_refused` — one query the model proposed that never reached a search
 *  engine, emitted in proposal order BEFORE that turn's `search` events. A turn
 *  can lose all three of its proposals, which is exactly the state the run view
 *  used to render as "searching" and then silence. Optional keys are absent,
 *  never null, when the reason's class does not have them. */
export interface QueryRefusedPayload {
  /** The proposed query, verbatim. Empty string when the proposal was blank. */
  query: string;
  reason: QueryRefusedReason;
  /** The query this one matched — a duplicate wall or the re-issue queue. */
  duplicates?: string;
  /** The turn the matched query ran on (duplicate wall only). */
  duplicates_turn?: number;
  /** The angle the host closed (`exhausted` only). */
  angle?: string;
  /** The host's own sentence for closing it (`exhausted` only). */
  why?: string;
}

/** One concrete next-step option proposed by the agent after repeated failures.
 *  The user clicks an option's card → the loop executes its tool_call as the
 *  next action. Mirrors the backend `AlternativeOption`. */
export interface AlternativeOption {
  id: string;
  title: string;
  description: string;
  tool_name: string;
  arguments: Record<string, unknown>;
}

/** The structured-recovery handoff: emitted after 4+ consecutive tool failures.
 *  The loop halts at `AWAITING_USER_DECISION` until the user picks one option
 *  (or steers explicitly). */
export interface AlternativesEvent extends EventBase {
  kind: "alternatives";
  failed_action_id: string;
  summary: string;
  options: AlternativeOption[];
}

/** The agent's finished-artifact HANDOFF (Build). Mirrors the backend
 *  DeliverableEvent: it names WHAT was produced and WHERE, so the UI can offer a
 *  real handoff (open the live app / download the files) instead of leaving the
 *  user to guess what the run made. `artifact_kind` drives the affordance:
 *  "app" → open in the live preview; "files" → download `path`. */
export interface DeliverableEvent extends EventBase {
  kind: "deliverable";
  title: string;
  path: string;
  artifact_kind: "app" | "files";
  /** Canonical URL the deliverable is reachable at (deploy target / tunnel / preview). */
  deployment_url?: string;
}

/** REL-1b host verifier audit marker. Suppressed in the UI event log. */
export interface VerifierStartedEvent extends EventBase {
  kind: "verifier_started";
  artifact_path: string;
  artifact_kind: string;
  verifier: string;
  requested_by_event_id?: string | null;
}

/** REL-1b host verifier verdict marker. Suppressed in the UI event log. */
export interface VerifierVerdictEvent extends EventBase {
  kind: "verifier_verdict";
  artifact_path: string;
  artifact_kind: string;
  verified: boolean;
  verdict?: string | null;
  detail?: string | null;
  failures?: Array<Record<string, unknown>>;
}

/** REL-1b shadow comparison between inline and host verifier results. */
export interface VerifierShadowEvent extends EventBase {
  kind: "verifier_shadow";
  artifact_path: string;
  artifact_kind: string;
  inline_verdict?: string | null;
  host_verdict?: string | null;
  agreement?: boolean | null;
  detail?: string | null;
}

/** CXT-3 deferred compaction marker. Suppressed in the UI event log. */
export interface ContextResolvedEvent extends EventBase {
  kind: "context_resolved";
  range_id: string;
  forgotten_start_seq: number;
  forgotten_end_seq: number;
  reason: string;
  summary_ref_path?: string | null;
}

/** CXT-3 durable summary marker. Suppressed in the UI event log. */
export interface ContextSummaryEvent extends EventBase {
  kind: "context_summary";
  range_id: string;
  rel_path: string;
  summary: string;
  artifact_kind: string;
}

export interface ClarifyQuestionItem {
  id: string;
  question: string;
  type: "short_text" | "long_text" | "choice";
  options: string[];
  answer: string;
}

/** Pre-plan typed clarification questions (RP-13). The planner calls `clarify`
 *  with multiple structured questions; the loop emits this event and halts at
 *  AWAITING_USER_QUESTION. The user answers each question and planning proceeds
 *  with the clarified context. */
export interface ClarifyEvent extends EventBase {
  kind: "clarify";
  question: string;
  items: ClarifyQuestionItem[];
}

export interface QuestionsV2Item {
  id: string;
  question: string;
  options: string[];
  allow_free_text?: boolean;
  answer: string;
}

/** Structured pre-plan intake (§K). The planner may call `questions_v2` once
 *  before submit_plan; the UI renders a form with options and free text. */
export interface QuestionsV2Event extends EventBase {
  kind: "questions_v2";
  question: string;
  items: QuestionsV2Item[];
}

/** A schedule was created or deleted for this conversation (RP-08). The UI
 *  surfaces a `created` notification in the activity panel + writes the new
 *  row to the schedule list; `deleted` removes it. NOT rendered as a
 *  conversation turn (it's a system lifecycle event). */
export interface ScheduleEvent extends EventBase {
  kind: "schedule";
  action: "created" | "deleted";
  schedule_id: string;
  rrule: string; // cron expression
  description: string;
}

/** A scheduled run fired and was appended to this conversation (RP-08). The
 *  UI shows it as a small system badge in the chat timeline ("Scheduled run
 *  fired at …") so the user can distinguish a run they triggered from one
 *  the scheduler kicked. `coalesced` is True when the server was down across
 *  N missed fires and this single run stands in for all of them. */
export interface ScheduleRunEvent extends EventBase {
  kind: "schedule_run";
  schedule_id: string;
  coalesced?: boolean; // default false on the wire
}

/** A scoped best-practice snippet injected into the agent's context (Cluster 7).
 *  Mirrors the backend `KnowledgeEvent`. `scope` is an optional applicability
 *  hint (task keyword or path glob); `snippet` is the guidance itself. Pinned
 *  against condensation so standing guidance survives a long run. */
export interface KnowledgeEvent extends EventBase {
  kind: "knowledge";
  scope: string;
  snippet: string;
}

/** Durable data-API / schema documentation the agent learned or was given
 *  (Cluster 7). Mirrors the backend `DatasourceEvent`. Condensation-IMMUNE: the
 *  exact contract stays verbatim across an arbitrarily long build, so an API
 *  shape learned mid-run can't be compressed into lossy prose and hallucinated
 *  back. `docs` carries endpoint shape, auth, params and an example response. */
export interface DatasourceEvent extends EventBase {
  kind: "datasource";
  name: string;
  docs: string;
}

/** A host-authored typed runtime constraint, with a usable alternative. Mirrors
 *  the backend `RuntimeConstraintEvent`. Host authority only — the backend fixes
 *  `source` to SYSTEM, so a model-authored lookalike in ordinary content carries
 *  no authority. `constraint_key` is stable identity: only the newest event for
 *  a key stays live, so repeated observations never grow context. `active: false`
 *  explicitly lifts the constraint for its key. */
export interface RuntimeConstraintEvent extends EventBase {
  kind: "runtime_constraint";
  constraint_key: string;
  scope: string;
  guidance: string;
  alternative: string;
  capability_generation: string;
  active: boolean;
}

export type AgentEvent =
  | MessageEvent
  | ActionEvent
  | ObservationEvent
  | AgentErrorEvent
  | CondensationEvent
  | StatusEvent
  | WorkspaceVersionEvent
  | WorkspaceRestoredEvent
  | WorkspaceMutationEvent
  | BuildPlatformAdmissionEvent
  | AppKitEjectionEvent
  | PlanEvent
  | ReportEvent
  | ResearchCheckpointEvent
  | AlternativesEvent
  | ClarifyEvent
  | QuestionsV2Event
  | ScheduleEvent
  | ScheduleRunEvent
  | DeliverableEvent
  | VerifierStartedEvent
  | VerifierVerdictEvent
  | VerifierShadowEvent
  | ContextResolvedEvent
  | ContextSummaryEvent
  | KnowledgeEvent
  | DatasourceEvent
  | RuntimeConstraintEvent
  | ErrorEvent;

export interface ConversationState {
  conversation_id: string;
  execution_status: ConversationStatus;
  iteration: number;
  max_iterations: number;
  last_seq: number;
  active_agent_view_id?: string | null;
  active_agent_view_seq?: number | null;
  agent_view_pending?: boolean;
  pending_action_id: string | null;
  pending_plan_id: string | null;
  pending_alternatives_id?: string | null;
  // The agent's free-form question message id, if status is AWAITING_USER_QUESTION.
  pending_question_id?: string | null;
  // The ClarifyEvent id, if status is AWAITING_USER_QUESTION and the gate is a clarify card.
  pending_clarify_id?: string | null;
  // The QuestionsV2Event id, if status is AWAITING_USER_QUESTION and the gate is structured intake.
  pending_questions_v2_id?: string | null;
  // Runtime-overlaid sandbox liveness; absent for non-build surfaces.
  // `autonomous` is set when the run is headless (no ask_user, auto-approved plan).
  // `quiet` is set when pre-plan assistant prose is suppressed.
  // `assist` is the server-derived execution tier (true = weak/assist; false/absent = standard).
  extras?: { sandbox?: "active" | "suspended"; autonomous?: boolean; quiet?: boolean; assist?: boolean };
  // BP-15: the active sandbox backend name ('gvisor'|'podman'|'local'|'process').
  // Absent until the first state frame; wire value only — never guessed client-side.
  sandbox_backend?: string;
}

// ---- WS frames (event-state §7) ---------------------------------------------

/** The payload of a `mcp_approval_required` frame (RP-05 D3). Emitted by the
 *  server when an MCP server's tool description fingerprint has changed
 *  since the user last approved it. The UI surfaces a re-approval card that
 *  cites the old + new hash so the user can audit the change. */
export interface McpApprovalPayload {
  server: string;
  description_hash: string;
  old_description_hash: string;
}

/** Frames the BACKEND actually sends. This is the mirror of `core/wire.py`'s
 *  `WSServerFrame.type` literal and is held to it byte-for-byte by
 *  `development/tests/architecture/test_frontend_contract_parity.py` — adding a variant
 *  here that the backend does not declare, or omitting one it does, fails CI.
 *
 *  `token` is declared because the backend declares it. No model emits token
 *  frames on this socket today ("present for contract completeness" — wire.py),
 *  so no UI path consumes it; the type exists so the mirror stays honest and so
 *  a future token stream finds the contract already correct. */
export type WSWireServerFrame =
  | { type: "state"; state: ConversationState }
  | { type: "event"; event: AgentEvent }
  | { type: "token"; token: string; token_for_event_id?: string | null }
  | { type: "file_stream"; file_stream: FileStreamFrame }
  | { type: "error"; error: { detail?: string } }
  | { type: "pong" }
  | { type: "mcp_approval_required"; mcp_approval: McpApprovalPayload };

/** Frames the WS CLIENT synthesizes locally — they never cross the wire and the
 *  backend has no equivalent. `subscribeConversation` emits `connection` when
 *  the socket recovers (`api/agent.ts`, on open after a degraded spell) and when
 *  it has failed past the degraded threshold (on close); `useBuildStream`'s
 *  reducer folds it into `connectionState`.
 *
 *  Kept in a separate union, and asserted DISJOINT from the wire union by the
 *  parity gate, so this cannot become a hiding place for a frame that really is
 *  backend surface. */
export type WSClientSynthesizedFrame = { type: "connection"; state: "connected" | "degraded" };

/** What an `onFrame` consumer receives: real wire frames plus the locally
 *  synthesized transport frames. */
export type WSServerFrame = WSWireServerFrame | WSClientSynthesizedFrame;

/** A watch-it-write delta: the driver is assembling a file body in a tool call.
 *  `delta` appends to the per-path buffer. NOT persisted — superseded by the
 *  final ActionEvent (which carries the authoritative full content). */
export interface FileStreamFrame {
  tool: string;
  path: string;
  index: number;
  delta: string;
  field?: "content" | "new";
  agent_view_id?: string | null;
}

// R3: optional `context` carries large hidden context (e.g. a full DR report)
// that the model receives as an ENVIRONMENT message but the user doesn't see;
// the visible `content` stays a short one-liner.
// D3: inject_source adds plaintext to an active Deep Research run's corpus.
// CONTRACT-ACTIVATE (2026-07-10): `build_brief` is an advisory PRESENCE flag —
// the server never trusts client fields; it re-classifies from `content` and
// uses the result to declare the build contract (starter recommendation,
// finalizer alias). Omitting it leaves the run undeclared (CUSTOM).
export type WSClientFrame =
  | { type: "send_message"; content: string; context?: string; build_brief?: Record<string, never> }
  | { type: "steer"; steer_text: string } // redirect a running agent / DR mid-run steer (routes by context)
  | { type: "confirm"; action_id?: string }
  | { type: "reject"; action_id?: string }
  | { type: "approve_plan" } // approve the pending plan → start building
  | { type: "request_plan"; content: string } // (re-)enter plan mode with an instruction
  // F-3 follow-up: the EXPLICIT "accept the finished build as-is" action (the
  // Mark-done control). The UI states stop-intent; the server treats it as
  // authoritative — the conversation stays FINISHED, no replan, no kick. The
  // server-side free-text heuristic remains only for plain typed messages.
  // Optional `content` is echoed to the log as a user note, never intent-matched.
  | { type: "accept_finished"; content?: string }
  | { type: "pick_alternative"; option_id: string } // structured recovery: pick a proposed alternative
  | { type: "pause" } // cooperative Build/Agent stop at the next step boundary; resumable
  | { type: "cancel" }
  | { type: "resume" } // continue a stopped/incomplete run (explicit, never on open)
  | { type: "ping" }
  | { type: "inject_source"; inject_source_text: string }
  // P8: the user clicked one preview element and described a change. The host
  // builds the scoped-edit directive (core/selection_edit.py) and steers the loop.
  | {
      type: "selection_edit";
      selection_ref: SelectionRef;
      edit_instruction: string;
      human_label?: string;
    };

/** Which isolation tier backs the sandbox — surfaced so the lower-isolation tier is
 * legible at the point of use (the cost-legible picker, applied to isolation). */
export interface IsolationInfo {
  tier: string; // "gvisor" | "container-remote" | "container" | …
  label: string;
  adversarialSafe: boolean;
}

/** A driver-eligible model for the Build chat picker (from the agent-server /models). */
export interface DriverModel {
  id: string; // catalogue key — the model_override
  label: string;
  provider: "local" | "openrouter";
  free: boolean;
  // W-05-fu: how the user pays — distinguishes a flat-rate SUBSCRIPTION model
  // (price 0/token but NOT free) from a genuinely free one. The server always
  // sends an effective mode (derives "free"/"metered" when unset).
  pricing_mode?: "metered" | "subscription" | "free";
  context_window: number;
  capabilities: Array<"vision" | "long_context" | "tool_calling" | "json_mode">;
}
export interface DriverModels {
  models: DriverModel[];
  default: string | null;
}

/** The backend-aware live preview: a URL to iframe the agent's running dev server, or a
 * reason it isn't available (no server yet / Podman stub). */
export interface PreviewInfo {
  available: boolean;
  proxy?: boolean; // served via the agent-server proxy (the browser builds the URL)
  reason?: string;
  stub?: boolean;
  /** Exact platform-managed process generation. Rotates on restart/recovery. */
  generation?: string;
  /** Current PreviewManager lifecycle state. */
  status?: "starting" | "running" | "unavailable" | "restarting" | "crashed" | "stopped";
  /** Authoritative launcher class, not inferred from a process name in the UI. */
  launch_kind?: "static" | "framework" | "custom" | string;
  /** Whether workspace changes are applied by the runtime or need a frame reload. */
  reload_strategy?: "hmr" | "reload";
  /** Latest host-owned refresh failure while the prior healthy frame remains visible. */
  update_error?: string | null;
  /** Canonical platform-selected runtime port; clients never choose it. */
  port?: number | null;
  owner?: {
    pid: number;
    cmdline: string;
    session: string | null;
  } | null;
  ports?: Array<{
    port: number;
    owner: { pid: number; cmdline: string; session: string | null } | null;
  }>;
}
