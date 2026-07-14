/**
 * Subscribe to a Build conversation's event stream and reduce it to a renderable
 * trace. The agent loop's EVENTS are the source of truth (unlike Research's token
 * stream): each frame appends/updates an event; reconnect replays are de-duped by id.
 * Exposes confirm/reject (the gate) and the raw send (the data layer owns the socket).
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { resumeConversation, subscribeConversation, type AgentHandle } from "@/api/agent";
import type { SelectionRef } from "@/lib/selectionBridge";
import {
  derivePlan,
  derivePlanProgress,
  deriveBuildProgress,
  type PlanView,
  type StepState,
} from "@/lib/buildTrace";
import type {
  ActionEvent,
  AgentEvent,
  AlternativesEvent,
  ClarifyEvent,
  ConversationStatus,
  MessageEvent,
  QuestionsV2Event,
  WSServerFrame,
} from "@/types/agent";

/** Factory for the unique suffix of an optimistic message id. Injectable so a test
 *  can FREEZE it (the default uses Date.now()/Math.random(), which makes the DOM key
 *  non-reproducible even under a fixed event stream — codex P1). Production keeps the
 *  default, so behavior is unchanged. */
export type OptimisticIdFactory = () => string;

const defaultOptimisticIdFactory: OptimisticIdFactory = () =>
  `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

/** Build a transient optimistic MessageEvent for the user's just-sent
 *  steer/revise text. The reducer drops it once the server echoes the
 *  canonical event back (matched by content). */
function localUserMessage(
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

export interface BuildSession {
  cid: string;
  task: string;
  /** R3: optional large context (the full DR report) sent as the send_message
   *  frame's hidden `context` — stored as an ENVIRONMENT message the model reads
   *  but the user doesn't see, so the visible `task` stays a short one-liner. */
  context?: string | null;
  /** Whether subscribing should ALSO start the run (send the task). Only a fresh
   *  `submit()` sets this true — opening an existing build (resume / History) is a
   *  safe read (subscribe + replay only), never `send_message`. (Command–Query
   *  Separation: viewing must not start work.) */
  kick?: boolean;
}

/** The file the driver is writing/editing RIGHT NOW, assembled from file_stream deltas.
 *  Cleared when the authoritative ActionEvent for that tool call lands (or the run
 *  leaves RUNNING). One at a time — the driver emits one tool call per step. */
export interface StreamingFile {
  path: string;
  content: string;
  tool: string;
  field?: "content" | "new";
}

export interface BuildStreamState {
  status: ConversationStatus;
  connectionState: "connected" | "degraded";
  events: AgentEvent[];
  streamingFile: StreamingFile | null;
  pendingActionId: string | null;
  pendingPlanId: string | null;
  pendingAlternativesId: string | null;
  pendingQuestionId: string | null;
  pendingClarifyId: string | null;
  pendingQuestionsV2Id: string | null;
  /** Runtime sandbox liveness from the state frame's extras overlay (bp-13):
   *  'suspended' = container torn down, workspace saved (badge);
   *  'active' = live; null = unknown / no sandbox context. */
  sandboxState: "active" | "suspended" | null;
  /** Headless run flag from the state frame's extras overlay (issue A). */
  autonomous: boolean;
  /** Server-derived execution tier from the state frame's extras (Order D).
   *  true = weak/assist tier (small-model compensations on); false = standard. */
  assist: boolean;
  /** The state frame's last_seq at (re)connect. Events replayed from history
   *  have seq <= this; only GENUINELY NEW status events (seq beyond the frame)
   *  may clear the suspended badge — a fresh page load replays the whole run,
   *  and its historical RUNNING events must not erase what the frame just said. */
  frameSeq: number;
  /** Highest event seq observed from ANY source (state frames or events).
   *  Monotonic per conversation and survives reconnects/reloads (the state
   *  frame re-reports last_seq) — a state-VERSION the live gauntlet reads via
   *  data-seq to judge "did this FINISHED happen after my edit?" without
   *  trusting an eternally-healthy stream to show it the transitions. */
  maxSeq: number;
  error: string | null;
}

const initial: BuildStreamState = {
  status: "IDLE",
  connectionState: "connected",
  events: [],
  streamingFile: null,
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

type Action =
  | { type: "reset" }
  | { type: "frame"; frame: WSServerFrame }
  | { type: "local_message"; event: AgentEvent };

function reducer(state: BuildStreamState, action: Action): BuildStreamState {
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
  const f = action.frame;
  if (f.type === "connection") {
    return f.state === "degraded" ? { ...state, connectionState: "degraded" } : state;
  }
  if (f.type === "state") {
    return {
      ...state,
      status: f.state.execution_status,
      pendingActionId: f.state.pending_action_id,
      pendingPlanId: f.state.pending_plan_id,
      pendingAlternativesId: f.state.pending_alternatives_id ?? null,
      pendingQuestionId: f.state.pending_question_id ?? null,
      pendingClarifyId: f.state.pending_clarify_id ?? null,
      pendingQuestionsV2Id: f.state.pending_questions_v2_id ?? null,
      sandboxState: f.state.extras?.sandbox ?? null,
      autonomous: f.state.extras?.autonomous ?? state.autonomous,
      assist: f.state.extras?.assist ?? state.assist,
      frameSeq: f.state.last_seq ?? 0,
    };
  }
  if (f.type === "file_stream") {
    // Watch-it-write: append the delta to the active file buffer. A new path (or
    // the first frame) starts a fresh buffer. These are transient — the matching
    // ActionEvent will supersede this with the authoritative content below.
    const fs = f.file_stream;
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
  if (f.type === "event") {
    // When the server echoes a USER MessageEvent, drop EXACTLY ONE matching
    // optimistic placeholder (id prefix "local-pending-", same content). Removing
    // one-to-one (not all matches) means two identical steers sent in quick
    // succession don't collapse into one row — each server echo retires one
    // placeholder.
    let working = state.events;
    if (
      f.event.kind === "message" &&
      f.event.source === "user" &&
      f.event.message?.content
    ) {
      const echo = f.event.message.content;
      const idx = working.findIndex(
        (e) =>
          e.id.startsWith("local-pending-") &&
          e.kind === "message" &&
          (e as MessageEvent).message?.content === echo,
      );
      if (idx !== -1) {
        working = [...working.slice(0, idx), ...working.slice(idx + 1)];
      }
    }
    const events = upsert(working, f.event);
    if (f.event.kind === "status") {
      const status = f.event.status;
      // Hoist out of the closures below: TS only preserves the `f.event` status
      // narrowing in the immediate scope, not inside the `.find` callback.
      const statusDetail = f.event.detail;
      return {
        ...state,
        events,
        status,
        // A LIVE RUNNING transition means the sandbox is live (or being
        // created) — never let a stale "suspended" badge sit over a working
        // build. But a fresh connect REPLAYS history (seq <= frameSeq): those
        // RUNNING events are the past, and must not erase the state frame's
        // overlay (the badge vanished on every page reload until run 3 of the
        // live spec caught it). Events without a seq are live by definition.
        sandboxState:
          status === "RUNNING" && (f.event.seq == null || f.event.seq > state.frameSeq)
            ? null
            : state.sandboxState,
        pendingActionId:
          status === "WAITING_FOR_CONFIRMATION"
            ? (f.event.detail ?? state.pendingActionId)
            : null,
        pendingPlanId:
          status === "AWAITING_PLAN_APPROVAL"
            ? (f.event.detail ?? state.pendingPlanId)
            : null,
        pendingAlternativesId:
          status === "AWAITING_USER_DECISION"
            ? (f.event.detail ?? state.pendingAlternativesId)
            : null,
        pendingQuestionId:
          status === "AWAITING_USER_QUESTION"
            ? (f.event.detail ?? state.pendingQuestionId)
            : null,
        pendingClarifyId:
          status === "AWAITING_USER_QUESTION"
            ? (events.find((e) => e.id === statusDetail && e.kind === "clarify")?.id ?? null)
            : null,
        pendingQuestionsV2Id:
          status === "AWAITING_USER_QUESTION"
            ? (events.find((e) => e.id === statusDetail && e.kind === "questions_v2")?.id ?? null)
            : null,
      };
    }
    if (f.event.kind === "error") {
      return { ...state, events, status: "ERROR", error: f.event.detail ?? "conversation error" };
    }
    // The driver's step concluded → the authoritative ActionEvent now carries the
    // full file. Retire the transient streaming buffer so the event renders as the
    // source of truth (no double display, no stale half-file lingering).
    if (f.event.kind === "action") {
      return { ...state, events, streamingFile: null };
    }
    return { ...state, events };
  }
  if (f.type === "error") {
    return { ...state, status: "ERROR", error: f.error.detail ?? "stream error" };
  }
  return state;
}

export interface BuildStream extends BuildStreamState {
  pendingAction: ActionEvent | null;
  pendingAlternatives: AlternativesEvent | null;
  /** The agent's free-form question, when status is AWAITING_USER_QUESTION. */
  pendingQuestion: MessageEvent | null;
  /** The clarify card's event, when status is AWAITING_USER_QUESTION via clarify. */
  pendingClarify: ClarifyEvent | null;
  /** The structured intake event, when status is AWAITING_USER_QUESTION via questions_v2. */
  pendingQuestionsV2: QuestionsV2Event | null;
  plan: PlanView | null;
  planProgress: Map<number, StepState>;
  buildProgress: Map<number, StepState>;
  awaitingPlan: boolean;
  awaitingDecision: boolean;
  awaitingQuestion: boolean;
  confirm: () => void;
  reject: () => void;
  cancel: () => void;
  steer: (text: string) => void;
  /** P8 click-to-edit: apply a scoped change to exactly the selected element. */
  selectionEdit: (ref: SelectionRef, instruction: string, humanLabel?: string) => void;
  /** Answer the agent's free-form question — resumes the loop (alias of steer,
   *  named for the Ask-gate so the AskPanel reads clearly). */
  answer: (text: string) => void;
  approvePlan: () => void;
  requestPlan: (text: string) => void;
  pickAlternative: (optionId: string) => void;
  resume: () => void;
  /** True when the conversation is in a resumable state (PAUSED, or IDLE with an
   *  unfinished approved plan). Used by the parent to decide whether to show the
   *  Resume button for the interrupted-with-unfinished-plan case (BP-12). */
  canResume: boolean;
}

export function useBuildStream(
  session: BuildSession | null,
  options?: {
    /** Inject a deterministic id factory for optimistic message ids (tests).
     *  Omitted in production → the default Date.now()/Math.random() suffix. */
    idFactory?: OptimisticIdFactory;
  },
): BuildStream {
  const [state, dispatch] = useReducer(reducer, initial);
  const handle = useRef<AgentHandle | null>(null);
  const makeId = options?.idFactory ?? defaultOptimisticIdFactory;

  useEffect(() => {
    if (!session) return;
    dispatch({ type: "reset" });
    const h = subscribeConversation(session.cid, (frame) => dispatch({ type: "frame", frame }));
    handle.current = h;
    // View ≠ start: only a fresh submit kicks the loop. Opening an existing build
    // (resume / History) is a safe read — subscribe + replay only.
    if (session.kick)
      h.send({
        type: "send_message",
        content: session.task,
        // CONTRACT-ACTIVATE: presence of build_brief asks the server to classify
        // the request and declare the build contract for this run (codex found
        // the shipped UI never sent it, so activation only fired for API callers).
        build_brief: {},
        ...(session.context ? { context: session.context } : {}),
      });
    return () => h.cancel();
  }, [session]);

  const confirm = useCallback(() => handle.current?.send({ type: "confirm" }), []);
  const reject = useCallback(() => handle.current?.send({ type: "reject" }), []);
  // The Build/Agent Stop control promises a resumable, non-destructive pause.
  // `cancel` takes the loop lock and can lose a race to a long in-flight model
  // turn; the server's `pause` control is deliberately lock-free and is observed
  // at the next step boundary. Deep Research keeps its separate `cancel` path.
  const cancel = useCallback(() => handle.current?.send({ type: "pause" }), []);
  const steer = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    // Optimistic echo: render immediately so the user sees their input land in
    // the timeline. The server's canonical echo (same content) replaces this
    // placeholder on arrival.
    dispatch({ type: "local_message", event: localUserMessage(trimmed, makeId) });
    handle.current?.send({ type: "steer", steer_text: trimmed });
  }, [makeId]);
  // P8: click-to-edit. Send the typed selection ref + the user's change; the host
  // builds the precisely-anchored scoped-edit directive (core/selection_edit.py) and
  // steers the loop. Optimistic echo so the change lands in the timeline immediately.
  const selectionEdit = useCallback(
    (ref: SelectionRef, instruction: string, humanLabel?: string) => {
      const trimmed = instruction.trim();
      if (!trimmed) return;
      dispatch({ type: "local_message", event: localUserMessage(trimmed, makeId) });
      handle.current?.send({
        type: "selection_edit",
        selection_ref: ref,
        edit_instruction: trimmed,
        ...(humanLabel ? { human_label: humanLabel } : {}),
      });
    },
    [makeId],
  );
  const approvePlan = useCallback(() => handle.current?.send({ type: "approve_plan" }), []);
  const requestPlan = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    dispatch({ type: "local_message", event: localUserMessage(trimmed, makeId) });
    handle.current?.send({ type: "request_plan", content: trimmed });
  }, [makeId]);
  const pickAlternative = useCallback(
    (optionId: string) =>
      handle.current?.send({ type: "pick_alternative", option_id: optionId }),
    [],
  );
  // Resume a PAUSED or IDLE-with-unfinished-plan build (BP-12). Calls the HTTP
  // POST /resume endpoint (mode-agnostic, legality-checked) — continuation, not
  // a fresh run. The WS subscription replays history-then-live on subscribe, so
  // events after the resume appear without a page reload.
  const resume = useCallback(() => {
    if (!session?.cid) return;
    void resumeConversation(session.cid);
  }, [session?.cid]);

  // True when the conversation is resumable: PAUSED always; a terminal ERROR or
  // STUCK (the recovery path — re-kick from history, no lost progress); IDLE only
  // when the event log contains an approved plan that was never finished
  // (interrupted run). The backend's resume_conversation enforces the same set.
  const canResume = useMemo(
    () =>
      state.status === "PAUSED" ||
      state.status === "ERROR" ||
      state.status === "STUCK" ||
      (state.status === "IDLE" && state.events.some((e) => e.kind === "plan")),
    [state.status, state.events],
  );

  const pendingAction =
    (state.pendingActionId &&
      (state.events.find(
        (e) => e.id === state.pendingActionId && e.kind === "action",
      ) as ActionEvent | undefined)) ||
    null;
  const pendingAlternatives =
    (state.pendingAlternativesId &&
      (state.events.find(
        (e) => e.id === state.pendingAlternativesId && e.kind === "alternatives",
      ) as AlternativesEvent | undefined)) ||
    null;
  const pendingQuestion =
    (state.pendingQuestionId &&
      (state.events.find(
        (e) => e.id === state.pendingQuestionId && e.kind === "message",
      ) as MessageEvent | undefined)) ||
    null;
  const pendingClarify =
    (state.pendingClarifyId &&
      (state.events.find(
        (e) => e.id === state.pendingClarifyId && e.kind === "clarify",
      ) as ClarifyEvent | undefined)) ||
    null;
  const pendingQuestionsV2 =
    (state.pendingQuestionsV2Id &&
      (state.events.find(
        (e) => e.id === state.pendingQuestionsV2Id && e.kind === "questions_v2",
      ) as QuestionsV2Event | undefined)) ||
    null;

  const plan = useMemo(() => derivePlan(state.events), [state.events]);
  const planProgress = useMemo(
    () => derivePlanProgress(state.events, state.status),
    [state.events, state.status],
  );
  // runthru-v2 (#3): capable-model live checklist, derived from the latest declarative
  // update_plan_progress snapshot. Empty (small models / none yet) → UI shows the chip.
  const buildProgress = useMemo(
    () => deriveBuildProgress(state.events, state.status),
    [state.events, state.status],
  );
  const awaitingPlan = state.status === "AWAITING_PLAN_APPROVAL";
  const awaitingDecision = state.status === "AWAITING_USER_DECISION";
  const awaitingQuestion = state.status === "AWAITING_USER_QUESTION";

  return {
    ...state,
    pendingAction,
    pendingAlternatives,
    pendingQuestion,
    pendingClarify,
    pendingQuestionsV2,
    plan,
    planProgress,
    buildProgress,
    awaitingPlan,
    awaitingDecision,
    awaitingQuestion,
    confirm,
    reject,
    cancel,
    steer,
    selectionEdit,
    // Answering a free-form question is just a user message that re-kicks the
    // loop — same wire path as steer, exposed under an Ask-gate-friendly name.
    answer: steer,
    approvePlan,
    requestPlan,
    pickAlternative,
    resume,
    canResume,
  };
}
