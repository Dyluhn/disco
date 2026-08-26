/**
 * Subscribe to a Deep Research conversation's event stream and reduce it to
 * the surface's renderable views. Same architecture as `useBuildStream` —
 * upsert-dedup-by-id, status state machine, history-then-live via the
 * shared subscribeConversation. The Deep Research specifics live in the
 * derived state: brief / stats / assembling sections / report.
 *
 * v2 (gateless deep research): there is NO plan-approval gate on this surface.
 * Submitting sends the question and research starts immediately; the model's
 * brief is its first visible output. The approve/revise frames the build lane
 * still uses are deliberately absent here — the build surface keeps its own
 * plan machinery in `useBuildStream`.
 */

import { useEffect, useMemo, useReducer, useRef } from "react";
import { subscribeConversation, type AgentHandle } from "@/api/agent";
import {
  deriveAssemblingSections,
  deriveBrief,
  deriveLiveTrace,
  deriveReport,
  deriveResearchCheckpoint,
  deriveSourceTiers,
  deriveStats,
  type AssemblingSection,
  type DeepStats,
  type SourceTiers,
} from "@/lib/deepResearchTrace";
import type { ActivityItem } from "@/lib/buildTrace";
import type {
  AgentEvent,
  ConversationStatus,
  ReportEvent,
  ResearchCheckpointEvent,
  WSServerFrame,
} from "@/types/agent";

export interface DeepResearchSession {
  cid: string;
  query: string;
  depthTier: "quick" | "standard_deep" | "exhaustive";
  /** Whether subscribing should ALSO start the run (send the query). Command–Query
   *  Separation: only a fresh `submit()` sets this true. Opening an existing run
   *  (from History / a /deep/:cid route) is a SAFE read — subscribe + replay only,
   *  never `send_message`. This is what stops "viewing spins it back up". */
  kick?: boolean;
}

interface RawState {
  status: ConversationStatus;
  /** Tracks the current follow-up phase without overwriting the main status.
   *  "follow_up" = a follow-up answer is being generated (loop re-entered);
   *  "follow_up_complete" = the last follow-up finished;
   *  null = no follow-up in flight (main run or idle). */
  followUpStatus: "follow_up" | "follow_up_complete" | null;
  events: AgentEvent[];
  error: string | null;
}

export const initial: RawState = {
  status: "IDLE",
  followUpStatus: null,
  events: [],
  error: null,
};

function upsert(events: AgentEvent[], event: AgentEvent): AgentEvent[] {
  const i = events.findIndex((e) => e.id === event.id);
  if (i === -1) return [...events, event];
  const next = events.slice();
  next[i] = event;
  return next;
}

export type Action = { type: "reset" } | { type: "frame"; frame: WSServerFrame };

/** Pure reducer — exported for unit tests. */
export function reducer(state: RawState, action: Action): RawState {
  if (action.type === "reset") return initial;
  const f = action.frame;
  if (f.type === "state") {
    return { ...state, status: f.state.execution_status };
  }
  if (f.type === "event") {
    const events = upsert(state.events, f.event);
    if (f.event.kind === "status") {
      const detail = f.event.detail ?? null;
      // Follow-up status events MUST NOT overwrite the main conversation status.
      // If they did, re-entering RUNNING for a follow-up re-spins
      // DeepProgressStrip (the run loader flashes again). Track them in a
      // separate field so the progress UI stays calm while the follow-up
      // answer generates.
      if (detail === "follow_up" || detail === "follow_up_complete") {
        return { ...state, events, followUpStatus: detail };
      }
      return { ...state, events, status: f.event.status, followUpStatus: null };
    }
    if (f.event.kind === "error") {
      return {
        ...state,
        events,
        status: "ERROR",
        error: f.event.detail ?? "conversation error",
      };
    }
    return { ...state, events };
  }
  if (f.type === "error") {
    return { ...state, status: "ERROR", error: f.error.detail ?? "stream error" };
  }
  return state;
}

export interface DeepResearchStream {
  status: ConversationStatus;
  /** Separate from `status` so the plan-progress UI can stay calm while a
   *  follow-up answer is generating. null when idle; "follow_up" during
   *  generation; "follow_up_complete" after the answer lands. */
  followUpStatus: "follow_up" | "follow_up_complete" | null;
  events: AgentEvent[];
  error: string | null;
  /** The model's opening brief — how it read the question and the angles it
   *  will chase. The FIRST visible output of a gateless run (v2); null until
   *  it lands. */
  brief: string | null;
  /** Live counts for the mono stats strip. */
  stats: DeepStats;
  /** The activity feed items (reuses ActivityFeed verbatim). */
  trace: ActivityItem[];
  /** Section placeholders + finalized sections (the assembling report view). */
  assembling: AssemblingSection[];
  /** The final ReportEvent — null until the engine emits it. */
  report: ReportEvent | null;
  /** Paused evidence state, kept separate from the finished report. */
  checkpoint: ResearchCheckpointEvent | null;
  /** Three-tier source split (cited / reviewed / discovered). */
  sources: SourceTiers;
  /** Send WS frames to the conversation. */
  cancel: () => void;
  /** Continue a stopped/incomplete run (explicit — never on open). */
  resume: () => void;
  /** Ask a follow-up question on this conversation — sends a steer message
   *  that re-kicks the loop with the report's corpus as grounding. */
  followUp: (question: string) => void;
  /**
   * Mid-run steer: send a steer string to the running DR engine. The server
   * routes the `steer` WS frame into the DR queue (not the agent loop) when
   * a DR run is active; the research agent drains it at its next turn
   * boundary, where it appears as a priority USER STEER line the model acts
   * on before returning to its own plan.
   *
   * Only call while `status === "RUNNING"` and the WS is open — the control
   * is gated in the surface to enforce this.
   */
  steer: (text: string) => void;
  /**
   * D3 inject-source: fold a plaintext snippet into the DR run's corpus.
   * The server converts the text to a Passage and makes it available to
   * subsequent section synthesis. URL extraction is a follow-up (v1: text only).
   *
   * Only call while `status === "RUNNING"`.
   */
  injectSource: (text: string) => void;
}

export function useDeepResearchStream(
  session: DeepResearchSession | null,
): DeepResearchStream {
  const [state, dispatch] = useReducer(reducer, initial);
  const handle = useRef<AgentHandle | null>(null);
  const sessionCid = session?.cid ?? null;
  const sessionRef = useRef(session);
  sessionRef.current = session;

  useEffect(() => {
    if (!sessionCid) {
      // Dispatch reset so derived state (followUps, plan, report …) clears
      // immediately when the session goes away (e.g. navigate away → back).
      // Without this the reducer held stale events from the previous run.
      dispatch({ type: "reset" });
      return;
    }
    const current = sessionRef.current;
    if (current === null) return;
    dispatch({ type: "reset" });
    const h = subscribeConversation(sessionCid, (frame) =>
      dispatch({ type: "frame", frame }),
    );
    handle.current = h;
    // Command–Query Separation: only a FRESH submit kicks the loop. Opening an
    // existing run (resume / History) is a safe read — subscribe + replay only.
    // v2: sending the question IS starting the research. There is no plan
    // proposal and nothing to approve — the engine's first visible output is
    // the model's brief.
    if (current.kick) {
      h.send({ type: "send_message", content: current.query });
    }
    return () => {
      h.cancel();
      if (handle.current === h) handle.current = null;
    };
  }, [sessionCid]);

  const cancel = () => handle.current?.send({ type: "cancel" });
  // Resume a stopped/incomplete run (explicit; never automatic on open).
  const resume = () => handle.current?.send({ type: "resume" });
  // Ask a follow-up question on the finished report — sends a user message
  // that resumes the conversation loop with the existing context.
  const followUp = (question: string) => {
    handle.current?.send({ type: "send_message", content: question });
  };
  // D3: mid-run steer — sends a `steer` frame while a DR run is active.
  // The server routes it into the DR queue (not the agent loop) and the engine
  // starts a new gather leg for the steer topic at the next section boundary.
  // Only meaningful while status === "RUNNING".
  // The user-facing control is `DeepSteerInput`, rendered by
  // DeepResearchRunView while the run is live.
  const steer = (text: string) => {
    const trimmed = text.trim();
    if (trimmed) handle.current?.send({ type: "steer", steer_text: trimmed });
  };
  // D3: inject-source — sends a plaintext snippet into the DR corpus.
  // v1: text only; URL extraction is a follow-up.
  const injectSource = (text: string) => {
    const trimmed = text.trim();
    if (trimmed)
      handle.current?.send({ type: "inject_source", inject_source_text: trimmed });
  };

  const brief = useMemo(() => deriveBrief(state.events), [state.events]);
  const stats = useMemo(() => deriveStats(state.events), [state.events]);
  const trace = useMemo(
    () => deriveLiveTrace(state.events, state.status),
    [state.events, state.status],
  );
  const assembling = useMemo(
    () => deriveAssemblingSections(state.events),
    [state.events],
  );
  const report = useMemo(() => deriveReport(state.events), [state.events]);
  const checkpoint = useMemo(
    () => deriveResearchCheckpoint(state.events),
    [state.events],
  );
  const sources = useMemo(() => deriveSourceTiers(report), [report]);

  return {
    status: state.status,
    followUpStatus: state.followUpStatus,
    events: state.events,
    error: state.error,
    brief,
    stats,
    trace,
    assembling,
    report,
    checkpoint,
    sources,
    cancel,
    resume,
    followUp,
    steer,
    injectSource,
  };
}
