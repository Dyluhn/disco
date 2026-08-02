/**
 * The Build-stream reducer: folds the WebSocket frame log into a `BuildStreamState`.
 * The agent loop's EVENTS are the source of truth (unlike Research's token stream):
 * each frame appends/updates an event; reconnect replays are de-duped by id.
 *
 * Extracted from hooks/useBuildStream.ts (module-size decomposition,
 * PKG-12-FE-BUILD/TS-0042) together with a genuine complexity reduction on the
 * reducer body itself (PKG-12-FE-BUILD/TS-0041): `reducerInner` used to be one
 * 72-branch function; it is now a thin dispatch over `action.type` → frame `type`
 * → event `kind`, with each kind's logic in its own small handler below (each
 * well under the 15-branch callable cap).
 *
 * `OptimisticIdFactory`, `StreamingFile` and `BuildStreamState` stay DECLARED on
 * useBuildStream.ts and are imported back here as types. That is deliberate: the
 * public-API authority keys a frontend target on the declaration's own module, so
 * moving a declaration out — even behind a byte-identical re-export — reads as
 * deleting a public target. Types are erased at compile time, so the import cycle
 * is types-only and never exists at runtime.
 */

import type {
  BuildStreamState,
  OptimisticIdFactory,
} from "../useBuildStream";
import type {
  AgentEvent,
  ConversationState,
  ConversationStatus,
  FileStreamFrame,
  MessageEvent,
  StatusEvent,
  WSServerFrame,
} from "@/types/agent";

/** Factory for the unique suffix of an optimistic message id. Injectable so a test
 *  can FREEZE it (the default uses Date.now()/Math.random(), which makes the DOM key
 *  non-reproducible even under a fixed event stream — codex P1). Production keeps the
 *  default, so behavior is unchanged. */

export const defaultOptimisticIdFactory: OptimisticIdFactory = () =>
  `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

/** Build a transient optimistic MessageEvent for the user's just-sent
 *  steer/revise text. The reducer drops it once the server echoes the
 *  canonical event back (matched by content). */
export function localUserMessage(
  content: string,
  makeId: OptimisticIdFactory = defaultOptimisticIdFactory,
): MessageEvent {
  return {
    id: `local-pending-${makeId()}`,
    kind: "message",
    source: "user",
    message: { role: "user", content },
  };
}

/** The file the driver is writing/editing RIGHT NOW, assembled from file_stream deltas.
 *  Cleared when the authoritative ActionEvent for that tool call lands (or the run
 *  leaves RUNNING). One at a time — the driver emits one tool call per step. */


export const initial: BuildStreamState = {
  status: "IDLE",
  connectionState: "connected",
  events: [],
  streamingFile: null,
  activeAgentViewId: null,
  activeAgentViewSeq: null,
  agentViewPending: false,
  pendingActionId: null,
  pendingPlanId: null,
  pendingAlternativesId: null,
  pendingQuestionId: null,
  pendingClarifyId: null,
  pendingQuestionsV2Id: null,
  sandboxState: null,
  autonomous: false,
  assist: false,
  frameSeq: 0,
  maxSeq: 0,
  error: null,
};

function upsert(events: AgentEvent[], event: AgentEvent): AgentEvent[] {
  const i = events.findIndex((e) => e.id === event.id);
  if (i === -1) return [...events, event];
  const next = events.slice();
  next[i] = event;
  return next;
}

export type Action =
  | { type: "reset" }
  | { type: "frame"; frame: WSServerFrame }
  | { type: "local_message"; event: AgentEvent };

export function reducer(state: BuildStreamState, action: Action): BuildStreamState {
  let next = reducerInner(state, action);
  // Fold the highest seen seq in ONE place rather than at every return site:
  // state frames report last_seq; events carry their own seq (absent on
  // optimistic/live-only frames). Reset passes through untouched (back to 0).
  if (action.type !== "frame") return next;
  const f = action.frame;
  if (f.type !== "connection" && next.connectionState !== "connected") {
    next = { ...next, connectionState: "connected" };
  }
  const seen =
    f.type === "state" ? (f.state.last_seq ?? 0) : f.type === "event" ? (f.event.seq ?? 0) : 0;
  return seen > next.maxSeq ? { ...next, maxSeq: seen } : next;
}

function reducerInner(state: BuildStreamState, action: Action): BuildStreamState {
  if (action.type === "reset") return initial;
  if (action.type === "local_message") {
    // Optimistic echo: render the user's just-sent steer/revise message
    // immediately so the UI acknowledges input. The server echoes the same
    // event back with the SAME id; upsert dedupes — no duplicate row.
    return { ...state, events: upsert(state.events, action.event) };
  }
  return handleFrame(state, action.frame);
}

function handleFrame(state: BuildStreamState, f: WSServerFrame): BuildStreamState {
  if (f.type === "connection") {
    return f.state === "degraded" ? { ...state, connectionState: "degraded" } : state;
  }
  if (f.type === "state") return handleStateFrame(state, f.state);
  if (f.type === "file_stream") return handleFileStreamFrame(state, f.file_stream);
  if (f.type === "event") return handleEventFrame(state, f.event);
  if (f.type === "error") {
    // A transport/protocol failure is not a durable conversation transition.
    // Preserve the server-derived status and surface the connection error only.
    return { ...state, error: f.error.detail ?? "stream error" };
  }
  return state;
}

function handleStateFrame(state: BuildStreamState, s: ConversationState): BuildStreamState {
  const activeAgentViewId = s.active_agent_view_id ?? null;
  const activeAgentViewSeq = s.active_agent_view_seq ?? null;
  return {
    ...state,
    status: s.execution_status,
    pendingActionId: s.pending_action_id,
    pendingPlanId: s.pending_plan_id,
    pendingAlternativesId: s.pending_alternatives_id ?? null,
    pendingQuestionId: s.pending_question_id ?? null,
    pendingClarifyId: s.pending_clarify_id ?? null,
    pendingQuestionsV2Id: s.pending_questions_v2_id ?? null,
    sandboxState: s.extras?.sandbox ?? null,
    autonomous: s.extras?.autonomous ?? state.autonomous,
    assist: s.extras?.assist ?? state.assist,
    frameSeq: s.last_seq ?? 0,
    activeAgentViewId,
    activeAgentViewSeq,
    agentViewPending: s.agent_view_pending ?? false,
    // Ephemeral deltas are not replayable. A state snapshot always starts a
    // fresh live buffer even when the durable view generation is unchanged.
    streamingFile: null,
  };
}

function handleFileStreamFrame(state: BuildStreamState, fs: FileStreamFrame): BuildStreamState {
  // Watch-it-write: append the delta to the active file buffer. A new path (or
  // the first frame) starts a fresh buffer. These are transient — the matching
  // ActionEvent will supersede this with the authoritative content below.
  if (fs.agent_view_id !== state.activeAgentViewId) return state;
  const prior =
    state.streamingFile &&
    state.streamingFile.path === fs.path &&
    state.streamingFile.tool === fs.tool &&
    state.streamingFile.field === fs.field
      ? state.streamingFile.content
      : "";
  return {
    ...state,
    streamingFile: {
      path: fs.path,
      tool: fs.tool,
      field: fs.field,
      content: prior + fs.delta,
    },
  };
}

/** When the server echoes a USER MessageEvent, drop EXACTLY ONE matching
 *  optimistic placeholder (id prefix "local-pending-", same content). Removing
 *  one-to-one (not all matches) means two identical steers sent in quick
 *  succession don't collapse into one row — each server echo retires one
 *  placeholder. */
function dedupeOptimisticEcho(events: AgentEvent[], event: AgentEvent): AgentEvent[] {
  if (event.kind !== "message" || event.source !== "user" || !event.message?.content) {
    return events;
  }
  const echo = event.message.content;
  const idx = events.findIndex(
    (e) =>
      e.id.startsWith("local-pending-") &&
      e.kind === "message" &&
      (e as MessageEvent).message?.content === echo,
  );
  if (idx === -1) return events;
  return [...events.slice(0, idx), ...events.slice(idx + 1)];
}

/** A new agent view starting (run-intent). Returns null when `event` isn't a
 *  matching run-intent workspace_mutation, so the caller falls through. */
function handleRunIntentMutation(
  state: BuildStreamState,
  event: AgentEvent,
  events: AgentEvent[],
): BuildStreamState | null {
  if (
    event.kind !== "workspace_mutation" ||
    !event.operation.startsWith("agent.run-intent.") ||
    event.run_protocol_version !== 1
  ) {
    return null;
  }
  if (
    state.activeAgentViewSeq != null &&
    event.seq != null &&
    event.seq <= state.activeAgentViewSeq
  ) {
    return { ...state, events };
  }
  return {
    ...state,
    events,
    activeAgentViewId: null,
    activeAgentViewSeq: null,
    agentViewPending: true,
    streamingFile: null,
    status: "RUNNING",
    pendingActionId: null,
    pendingPlanId: null,
    pendingAlternativesId: null,
    pendingQuestionId: null,
    pendingClarifyId: null,
    pendingQuestionsV2Id: null,
    error: null,
  };
}

/** The agent view the run-intent opened is now admitted. Returns null when
 *  `event` isn't a matching view-admitted workspace_mutation. */
function handleViewAdmittedMutation(
  state: BuildStreamState,
  event: AgentEvent,
  events: AgentEvent[],
): BuildStreamState | null {
  if (
    event.kind !== "workspace_mutation" ||
    event.operation !== "agent.view-admitted" ||
    !event.agent_view_id
  ) {
    return null;
  }
  if (
    state.activeAgentViewSeq != null &&
    event.seq != null &&
    event.seq < state.activeAgentViewSeq
  ) {
    return { ...state, events };
  }
  return {
    ...state,
    events,
    activeAgentViewId: event.agent_view_id,
    activeAgentViewSeq: event.seq ?? null,
    agentViewPending: false,
    streamingFile: null,
  };
}

function isStaleViewEvent(state: BuildStreamState, event: AgentEvent): boolean {
  if (event.agent_view_id == null) return false;
  if (state.agentViewPending) return true;
  return (
    state.activeAgentViewId != null &&
    event.agent_view_id !== state.activeAgentViewId &&
    (event.seq == null || state.activeAgentViewSeq == null || event.seq > state.activeAgentViewSeq)
  );
}

/** Shared shape of the four `status === X ? (detail ?? priorId) : null` pending-id
 *  projections below `handleStatusEvent` — one status owns one pending-gate id, and
 *  every other status clears it. Factoring the identical pattern out (rather than
 *  writing it four times) is what keeps `handleStatusEvent` under the complexity cap. */
function pendingIdForStatus(
  status: ConversationStatus,
  targetStatus: ConversationStatus,
  detail: string | null | undefined,
  priorId: string | null,
): string | null {
  return status === targetStatus ? (detail ?? priorId) : null;
}

/** The clarify/questions_v2 pending-gate id is the ask-gate event matching the
 *  status detail's id — both follow the identical lookup, keyed only by kind. */
function pendingGateEventId(
  status: ConversationStatus,
  events: AgentEvent[],
  statusDetail: string | null | undefined,
  kind: AgentEvent["kind"],
): string | null {
  if (status !== "AWAITING_USER_QUESTION") return null;
  return events.find((e) => e.id === statusDetail && e.kind === kind)?.id ?? null;
}

function handleStatusEvent(
  state: BuildStreamState,
  event: StatusEvent,
  events: AgentEvent[],
): BuildStreamState {
  const status = event.status;
  const statusDetail = event.detail;
  return {
    ...state,
    events,
    status,
    // A LIVE RUNNING transition means the sandbox is live (or being created) —
    // never let a stale "suspended" badge sit over a working build. But a fresh
    // connect REPLAYS history (seq <= frameSeq): those RUNNING events are the
    // past, and must not erase the state frame's overlay. Events without a seq
    // are live by definition.
    sandboxState:
      status === "RUNNING" && (event.seq == null || event.seq > state.frameSeq)
        ? null
        : state.sandboxState,
    pendingActionId: pendingIdForStatus(
      status,
      "WAITING_FOR_CONFIRMATION",
      statusDetail,
      state.pendingActionId,
    ),
    pendingPlanId: pendingIdForStatus(
      status,
      "AWAITING_PLAN_APPROVAL",
      statusDetail,
      state.pendingPlanId,
    ),
    pendingAlternativesId: pendingIdForStatus(
      status,
      "AWAITING_USER_DECISION",
      statusDetail,
      state.pendingAlternativesId,
    ),
    pendingQuestionId: pendingIdForStatus(
      status,
      "AWAITING_USER_QUESTION",
      statusDetail,
      state.pendingQuestionId,
    ),
    pendingClarifyId: pendingGateEventId(status, events, statusDetail, "clarify"),
    pendingQuestionsV2Id: pendingGateEventId(status, events, statusDetail, "questions_v2"),
    streamingFile: status === "RUNNING" ? state.streamingFile : null,
  };
}

function handleEventFrame(state: BuildStreamState, event: AgentEvent): BuildStreamState {
  const events = upsert(dedupeOptimisticEcho(state.events, event), event);
  // The state frame is the authoritative semantic projection at frameSeq.
  // Replayed history populates the activity trace but must never roll status,
  // gates, or view ownership back from that snapshot.
  if (event.seq != null && event.seq <= state.frameSeq) {
    return { ...state, events };
  }
  const runIntent = handleRunIntentMutation(state, event, events);
  if (runIntent) return runIntent;
  const viewAdmitted = handleViewAdmittedMutation(state, event, events);
  if (viewAdmitted) return viewAdmitted;
  if (isStaleViewEvent(state, event)) return state;
  if (event.kind === "status") return handleStatusEvent(state, event, events);
  if (event.kind === "error") {
    return { ...state, events, status: "ERROR", error: event.detail ?? "conversation error" };
  }
  // The driver's step concluded → the authoritative ActionEvent now carries the
  // full file. Retire the transient streaming buffer so the event renders as the
  // source of truth (no double display, no stale half-file lingering).
  if (event.kind === "action") {
    return { ...state, events, streamingFile: null };
  }
  return { ...state, events };
}
