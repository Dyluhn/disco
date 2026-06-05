/**
 * Subscribe to a Build conversation's event stream and reduce it to a renderable
 * trace. The agent loop's EVENTS are the source of truth (unlike Research's token
 * stream): each frame appends/updates an event; reconnect replays are de-duped by id.
 * Exposes confirm/reject (the gate) and the raw send (the data layer owns the socket).
 */

import { useCallback, useEffect, useReducer, useRef } from "react";
import { subscribeConversation, type AgentHandle } from "@/api/agent";
import type {
  ActionEvent,
  AgentEvent,
  ConversationStatus,
  WSServerFrame,
} from "@/types/agent";

export interface BuildSession {
  cid: string;
  task: string;
}

export interface BuildStreamState {
  status: ConversationStatus;
  events: AgentEvent[];
  pendingActionId: string | null;
  error: string | null;
}

const initial: BuildStreamState = {
  status: "IDLE",
  events: [],
  pendingActionId: null,
  error: null,
};

function upsert(events: AgentEvent[], event: AgentEvent): AgentEvent[] {
  const i = events.findIndex((e) => e.id === event.id);
  if (i === -1) return [...events, event];
  const next = events.slice();
  next[i] = event;
  return next;
}

type Action = { type: "reset" } | { type: "frame"; frame: WSServerFrame };

function reducer(state: BuildStreamState, action: Action): BuildStreamState {
  if (action.type === "reset") return initial;
  const f = action.frame;
  if (f.type === "state") {
    return {
      ...state,
      status: f.state.execution_status,
      pendingActionId: f.state.pending_action_id,
    };
  }
  if (f.type === "event") {
    const events = upsert(state.events, f.event);
    if (f.event.kind === "status") {
      return {
        ...state,
        events,
        status: f.event.status,
        pendingActionId:
          f.event.status === "WAITING_FOR_CONFIRMATION"
            ? (f.event.detail ?? state.pendingActionId)
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
  confirm: () => void;
  reject: () => void;
  cancel: () => void;
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

  const pendingAction =
    (state.pendingActionId &&
      (state.events.find(
        (e) => e.id === state.pendingActionId && e.kind === "action",
      ) as ActionEvent | undefined)) ||
    null;

  return { ...state, pendingAction, confirm, reject, cancel };
}
