/**
 * Subscribe to a Build conversation's event stream and reduce it to a renderable
 * trace. The agent loop's EVENTS are the source of truth (unlike Research's token
 * stream): each frame appends/updates an event; reconnect replays are de-duped by id.
 * Exposes confirm/reject (the gate) and the raw send (the data layer owns the socket).
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { subscribeConversation, type AgentHandle } from "@/api/agent";
import { derivePlan, derivePlanProgress, type PlanView, type StepState } from "@/lib/buildTrace";
import type {
  ActionEvent,
  AgentEvent,
  AlternativesEvent,
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
}

export interface BuildStreamState {
  status: ConversationStatus;
  events: AgentEvent[];
  pendingActionId: string | null;
  pendingPlanId: string | null;
  pendingAlternativesId: string | null;
  error: string | null;
}

const initial: BuildStreamState = {
  status: "IDLE",
  events: [],
  pendingActionId: null,
  pendingPlanId: null,
  pendingAlternativesId: null,
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
      };
    }
    if (f.event.kind === "error") {
      return { ...state, events, status: "ERROR", error: f.event.detail ?? "conversation error" };
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
  plan: PlanView | null;
  planProgress: Map<number, StepState>;
  awaitingPlan: boolean;
  awaitingDecision: boolean;
  confirm: () => void;
  reject: () => void;
  cancel: () => void;
  steer: (text: string) => void;
  approvePlan: () => void;
  requestPlan: (text: string) => void;
  pickAlternative: (optionId: string) => void;
}

export function useBuildStream(session: BuildSession | null): BuildStream {
  const [state, dispatch] = useReducer(reducer, initial);
  const handle = useRef<AgentHandle | null>(null);

  useEffect(() => {
    if (!session) return;
    dispatch({ type: "reset" });
    const h = subscribeConversation(session.cid, (frame) => dispatch({ type: "frame", frame }));
    handle.current = h;
    h.send({ type: "send_message", content: session.task }); // kick the loop with the task
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

  const plan = useMemo(() => derivePlan(state.events), [state.events]);
  const planProgress = useMemo(
    () => derivePlanProgress(state.events, state.status),
    [state.events, state.status],
  );
  const awaitingPlan = state.status === "AWAITING_PLAN_APPROVAL";
  const awaitingDecision = state.status === "AWAITING_USER_DECISION";

  return {
    ...state,
    pendingAction,
    pendingAlternatives,
    plan,
    planProgress,
    awaitingPlan,
    awaitingDecision,
    confirm,
    reject,
    cancel,
    steer,
    approvePlan,
    requestPlan,
    pickAlternative,
  };
}
