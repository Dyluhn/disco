/**
 * Subscribe to a Build conversation's event stream and reduce it to a renderable
 * trace. The agent loop's EVENTS are the source of truth (unlike Research's token
 * stream): each frame appends/updates an event; reconnect replays are de-duped by id.
 * Exposes confirm/reject (the gate) and the raw send (the data layer owns the socket).
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { resumeConversation, subscribeConversation, type AgentHandle } from "@/api/agent";
import { derivePlan, derivePlanProgress, type PlanView, type StepState } from "@/lib/buildTrace";
import type {
  ActionEvent,
  AgentEvent,
  AlternativesEvent,
  ClarifyEvent,
  ConversationStatus,
  MessageEvent,
  WSServerFrame,
} from "@/types/agent";

/** Build a transient optimistic MessageEvent for the user's just-sent
 *  steer/revise text. The reducer drops it once the server echoes the
 *  canonical event back (matched by content). */
function localUserMessage(content: string): MessageEvent {
  return {
    id: `local-pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    kind: "message",
    source: "user",
    message: { role: "user", content },
  };
}

export interface BuildSession {
  cid: string;
  task: string;
  /** Whether subscribing should ALSO start the run (send the task). Only a fresh
   *  `submit()` sets this true — opening an existing build (resume / History) is a
   *  safe read (subscribe + replay only), never `send_message`. (Command–Query
   *  Separation: viewing must not start work.) */
  kick?: boolean;
}

/** The file the driver is writing RIGHT NOW, assembled from file_stream deltas.
 *  Cleared when the authoritative ActionEvent for that write lands (or the run
 *  leaves RUNNING). One at a time — the driver emits one tool call per step. */
export interface StreamingFile {
  path: string;
  content: string;
  tool: string;
}

export interface BuildStreamState {
  status: ConversationStatus;
  events: AgentEvent[];
  streamingFile: StreamingFile | null;
  pendingActionId: string | null;
  pendingPlanId: string | null;
  pendingAlternativesId: string | null;
  pendingQuestionId: string | null;
  pendingClarifyId: string | null;
  /** Runtime sandbox liveness from the state frame's extras overlay (bp-13):
   *  'suspended' = container torn down, workspace saved (badge);
   *  'active' = live; null = unknown / no sandbox context. */
  sandboxState: "active" | "suspended" | null;
  /** The state frame's last_seq at (re)connect. Events replayed from history
   *  have seq <= this; only GENUINELY NEW status events (seq beyond the frame)
   *  may clear the suspended badge — a fresh page load replays the whole run,
   *  and its historical RUNNING events must not erase what the frame just said. */
  frameSeq: number;
  error: string | null;
}

const initial: BuildStreamState = {
  status: "IDLE",
  events: [],
  streamingFile: null,
  pendingActionId: null,
  pendingPlanId: null,
  pendingAlternativesId: null,
  pendingQuestionId: null,
  pendingClarifyId: null,
  sandboxState: null,
  frameSeq: 0,
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
  if (action.type === "reset") return initial;
  if (action.type === "local_message") {
    // Optimistic echo: render the user's just-sent steer/revise message
    // immediately so the UI acknowledges input. The server echoes the same
    // event back with the SAME id; upsert dedupes — no duplicate row.
    return { ...state, events: upsert(state.events, action.event) };
  }
  const f = action.frame;
  if (f.type === "state") {
    return {
      ...state,
      status: f.state.execution_status,
      pendingActionId: f.state.pending_action_id,
      pendingPlanId: f.state.pending_plan_id,
      pendingAlternativesId: f.state.pending_alternatives_id ?? null,
      pendingQuestionId: f.state.pending_question_id ?? null,
      pendingClarifyId: f.state.pending_clarify_id ?? null,
      sandboxState: f.state.extras?.sandbox ?? null,
      frameSeq: f.state.last_seq ?? 0,
    };
  }
  if (f.type === "file_stream") {
    // Watch-it-write: append the delta to the active file buffer. A new path (or
    // the first frame) starts a fresh buffer. These are transient — the matching
    // ActionEvent will supersede this with the authoritative content below.
    const fs = f.file_stream;
    const prior =
      state.streamingFile && state.streamingFile.path === fs.path ? state.streamingFile.content : "";
    return {
      ...state,
      streamingFile: { path: fs.path, tool: fs.tool, content: prior + fs.delta },
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
            ? (events.find((e) => e.id === f.event.detail && e.kind === "clarify")?.id ?? null)
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
  plan: PlanView | null;
  planProgress: Map<number, StepState>;
  awaitingPlan: boolean;
  awaitingDecision: boolean;
  awaitingQuestion: boolean;
  confirm: () => void;
  reject: () => void;
  cancel: () => void;
  steer: (text: string) => void;
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

export function useBuildStream(session: BuildSession | null): BuildStream {
  const [state, dispatch] = useReducer(reducer, initial);
  const handle = useRef<AgentHandle | null>(null);

  useEffect(() => {
    if (!session) return;
    dispatch({ type: "reset" });
    const h = subscribeConversation(session.cid, (frame) => dispatch({ type: "frame", frame }));
    handle.current = h;
    // View ≠ start: only a fresh submit kicks the loop. Opening an existing build
    // (resume / History) is a safe read — subscribe + replay only.
    if (session.kick) h.send({ type: "send_message", content: session.task });
    return () => h.cancel();
  }, [session]);

  const confirm = useCallback(() => handle.current?.send({ type: "confirm" }), []);
  const reject = useCallback(() => handle.current?.send({ type: "reject" }), []);
  const cancel = useCallback(() => handle.current?.send({ type: "cancel" }), []);
  const steer = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    // Optimistic echo: render immediately so the user sees their input land in
    // the timeline. The server's canonical echo (same content) replaces this
    // placeholder on arrival.
    dispatch({ type: "local_message", event: localUserMessage(trimmed) });
    handle.current?.send({ type: "steer", steer_text: trimmed });
  }, []);
  const approvePlan = useCallback(() => handle.current?.send({ type: "approve_plan" }), []);
  const requestPlan = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    dispatch({ type: "local_message", event: localUserMessage(trimmed) });
    handle.current?.send({ type: "request_plan", content: trimmed });
  }, []);
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

  // True when the conversation is resumable: PAUSED always; IDLE only when the
  // event log contains an approved plan that was never finished (interrupted run).
  const canResume = useMemo(
    () =>
      state.status === "PAUSED" ||
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

  const plan = useMemo(() => derivePlan(state.events), [state.events]);
  const planProgress = useMemo(
    () => derivePlanProgress(state.events, state.status),
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
    plan,
    planProgress,
    awaitingPlan,
    awaitingDecision,
    awaitingQuestion,
    confirm,
    reject,
    cancel,
    steer,
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
