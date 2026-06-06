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
}

interface RawState {
  status: ConversationStatus;
  events: AgentEvent[];
  pendingPlanId: string | null;
  error: string | null;
}

const initial: RawState = {
  status: "IDLE",
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

type Action = { type: "reset" } | { type: "frame"; frame: WSServerFrame };

function reducer(state: RawState, action: Action): RawState {
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
      const status = f.event.status;
      return {
        ...state,
        events,
        status,
        pendingPlanId:
          status === "AWAITING_PLAN_APPROVAL"
            ? (f.event.detail ?? state.pendingPlanId)
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
}

export function useDeepResearchStream(
  session: DeepResearchSession | null,
): DeepResearchStream {
  const [state, dispatch] = useReducer(reducer, initial);
  const handle = useRef<AgentHandle | null>(null);

  useEffect(() => {
    if (!session) return;
    dispatch({ type: "reset" });
    const h = subscribeConversation(session.cid, (frame) =>
      dispatch({ type: "frame", frame }),
    );
    handle.current = h;
    // Kick the loop: send the query as the first user message. The engine's
    // _propose_deep_research_plan path decomposes it into a PlanEvent +
    // AWAITING_PLAN_APPROVAL, and we wait for the user to approve via the
    // plan gate.
    h.send({ type: "send_message", content: session.query });
    return () => h.cancel();
  }, [session]);

  const approvePlan = () => handle.current?.send({ type: "approve_plan" });
  const requestPlan = (text: string) =>
    text.trim() &&
    handle.current?.send({ type: "request_plan", content: text.trim() });
  const cancel = () => handle.current?.send({ type: "cancel" });

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
  };
}
