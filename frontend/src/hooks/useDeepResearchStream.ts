/**
 * Subscribe to a Deep Research conversation's event stream and reduce it to
 * the surface's renderable views. Same architecture as `useBuildStream` —
 * upsert-dedup-by-id, status state machine, history-then-live via the
 * shared subscribeConversation. The Deep Research specifics live in the
 * derived state: plan / progress / stats / assembling sections / report.
 */

import { useEffect, useMemo, useReducer, useRef } from "react";
import { subscribeConversation, type AgentHandle } from "@/api/agent";
import {
  deriveAssemblingSections,
  deriveLiveTrace,
  derivePlan,
  derivePlanProgress,
  deriveReport,
  deriveSourceTiers,
  deriveStats,
  type AssemblingSection,
  type DeepPlanView,
  type DeepStats,
  type SourceTiers,
  type StepState,
} from "@/lib/deepResearchTrace";
import type { ActivityItem } from "@/lib/buildTrace";
import type {
  AgentEvent,
  ConversationStatus,
  ReportEvent,
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
   *  null = no follow-up in flight (plan-run or idle). */
  followUpStatus: "follow_up" | "follow_up_complete" | null;
  events: AgentEvent[];
  pendingPlanId: string | null;
  error: string | null;
}

export const initial: RawState = {
  status: "IDLE",
  followUpStatus: null,
  events: [],
  pendingPlanId: null,
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
    return {
      ...state,
      status: f.state.execution_status,
      pendingPlanId: f.state.pending_plan_id,
    };
  }
  if (f.type === "event") {
    const events = upsert(state.events, f.event);
    if (f.event.kind === "status") {
      const detail = f.event.detail ?? null;
      // Follow-up status events MUST NOT overwrite the main conversation status.
      // If they did, re-entering RUNNING for a follow-up re-spins DeepProgressStrip
      // (the original-plan loader flashes again). Track them in a separate field
      // so the plan-progress UI can stay calm while the follow-up answer generates.
      if (detail === "follow_up" || detail === "follow_up_complete") {
        return { ...state, events, followUpStatus: detail };
      }
      const status = f.event.status;
      return {
        ...state,
        events,
        status,
        followUpStatus: null,
        pendingPlanId:
          status === "AWAITING_PLAN_APPROVAL"
            ? (detail ?? state.pendingPlanId)
            : null,
      };
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
  /** The proposed plan (sub-questions). Null until decompose completes. */
  plan: DeepPlanView | null;
  /** Per-sub-question progress (1-based index → state). */
  progress: Map<number, StepState>;
  /** True at AWAITING_PLAN_APPROVAL (drives the plan-edit gate). */
  awaitingPlan: boolean;
  /** Live counts for the mono stats strip. */
  stats: DeepStats;
  /** The activity feed items (reuses ActivityFeed verbatim). */
  trace: ActivityItem[];
  /** Section placeholders + finalized sections (the assembling report view). */
  assembling: AssemblingSection[];
  /** The final ReportEvent — null until the engine emits it. */
  report: ReportEvent | null;
  /** Three-tier source split (cited / reviewed / discovered). */
  sources: SourceTiers;
  /** Send WS frames to the conversation. */
  approvePlan: () => void;
  requestPlan: (text: string) => void;
  cancel: () => void;
  /** Continue a stopped/incomplete run (explicit — never on open). */
  resume: () => void;
  /** Ask a follow-up question on this conversation — sends a steer message
   *  that re-kicks the loop with the report's corpus as grounding. */
  followUp: (question: string) => void;
}

export function useDeepResearchStream(
  session: DeepResearchSession | null,
): DeepResearchStream {
  const [state, dispatch] = useReducer(reducer, initial);
  const handle = useRef<AgentHandle | null>(null);

  useEffect(() => {
    if (!session) {
      // Dispatch reset so derived state (followUps, plan, report …) clears
      // immediately when the session goes away (e.g. navigate away → back).
      // Without this the reducer held stale events from the previous run.
      dispatch({ type: "reset" });
      return;
    }
    dispatch({ type: "reset" });
    const h = subscribeConversation(session.cid, (frame) =>
      dispatch({ type: "frame", frame }),
    );
    handle.current = h;
    // Command–Query Separation: only a FRESH submit kicks the loop. Opening an
    // existing run (resume / History) is a safe read — subscribe + replay only.
    // The engine's _propose_deep_research_plan path decomposes the query into a
    // PlanEvent + AWAITING_PLAN_APPROVAL; we then wait at the plan gate.
    if (session.kick) {
      h.send({ type: "send_message", content: session.query });
    }
    return () => h.cancel();
  }, [session]);

  const approvePlan = () => handle.current?.send({ type: "approve_plan" });
  const requestPlan = (text: string) =>
    text.trim() &&
    handle.current?.send({ type: "request_plan", content: text.trim() });
  const cancel = () => handle.current?.send({ type: "cancel" });
  // Resume a stopped/incomplete run (explicit; never automatic on open).
  const resume = () => handle.current?.send({ type: "resume" });
  // Ask a follow-up question on the finished report — sends a user message
  // that resumes the conversation loop with the existing context.
  const followUp = (question: string) => {
    handle.current?.send({ type: "send_message", content: question });
  };

  const plan = useMemo(() => derivePlan(state.events), [state.events]);
  const progress = useMemo(
    () => derivePlanProgress(state.events, plan),
    [state.events, plan],
  );
  const stats = useMemo(() => deriveStats(state.events, plan), [state.events, plan]);
  const trace = useMemo(
    () => deriveLiveTrace(state.events, state.status),
    [state.events, state.status],
  );
  const assembling = useMemo(
    () => deriveAssemblingSections(state.events, plan),
    [state.events, plan],
  );
  const report = useMemo(() => deriveReport(state.events), [state.events]);
  const sources = useMemo(() => deriveSourceTiers(report), [report]);
  const awaitingPlan = state.status === "AWAITING_PLAN_APPROVAL";

  return {
    status: state.status,
    followUpStatus: state.followUpStatus,
    events: state.events,
    error: state.error,
    plan,
    progress,
    awaitingPlan,
    stats,
    trace,
    assembling,
    report,
    sources,
    approvePlan,
    requestPlan,
    cancel,
    resume,
    followUp,
  };
}
