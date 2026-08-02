/**
 * Subscribe to a Build conversation's event stream and reduce it to a renderable
 * trace. The agent loop's EVENTS are the source of truth (unlike Research's token
 * stream): each frame appends/updates an event; reconnect replays are de-duped by id.
 * Exposes confirm/reject (the gate) and the raw send (the data layer owns the socket).
 *
 * Module-size decomposition (PKG-12-FE-BUILD/TS-0042/TS-0041): the reducer itself
 * (state shape, `reducer`/`reducerInner`, and every per-frame/per-kind handler) now
 * lives in `./buildStream/reducer.ts` — see that file for the complexity-reduction
 * rationale. Everything it exports that's part of THIS file's public surface is
 * re-exported below so the import path (`@/hooks/useBuildStream`) is unchanged.
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
} from "@/types/agent";
import {
  defaultOptimisticIdFactory,
  initial,
  localUserMessage,
  reducer,
} from "./buildStream/reducer";

export type OptimisticIdFactory = () => string;

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
  activeAgentViewId: string | null;
  activeAgentViewSeq: number | null;
  agentViewPending: boolean;
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

/** An honest provider-outage label, derived from the latest blocked-landing
 *  StatusEvent's non-semantic meta (the backend's driver_error_* keys). Present
 *  ONLY when the run parked because the model provider stayed unavailable AND
 *  the provider answered with an HTTP status (429 usage limit, 5xx outage) —
 *  timeouts/network failures carry no status and keep the generic copy. */
export interface DriverOutage {
  /** The HTTP status the provider returned (e.g. 429). */
  httpStatus: number;
  /** The adapter's sanitized provider line, verbatim (never the raw body). */
  message: string | null;
  provider: string | null;
  kind: string | null;
}

/** Selector over the event log (NOT reducer state) so it is replay/reconnect
 *  proof: a fresh WS subscribe replays history, and the landing meta rides on
 *  the replayed StatusEvents. Reads the LATEST status event only — a newer
 *  RUNNING (resume/answer) naturally supersedes the outage. */
export function deriveDriverOutage(events: AgentEvent[]): DriverOutage | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind !== "status") continue;
    const meta = e.meta;
    if (!meta || meta.blocked_landing !== true) return null;
    const httpStatus = meta.driver_error_http_status;
    if (typeof httpStatus !== "number") return null;
    return {
      httpStatus,
      message: typeof meta.driver_error === "string" ? meta.driver_error : null,
      provider:
        typeof meta.driver_error_provider === "string" ? meta.driver_error_provider : null,
      kind: typeof meta.driver_error_kind === "string" ? meta.driver_error_kind : null,
    };
  }
  return null;
}

/** The latest StatusEvent(ERROR)'s detail — the run-level failure the backend
 *  actually reported (e.g. a run-start provider 429 preflight). Selector over
 *  events (replay-proof): the reducer's `error` only captures live error
 *  frames, so on reload of an errored conversation it stayed null and the UI
 *  fell back to a generic "the run failed". Null unless the latest status IS
 *  an ERROR (a later RUNNING supersedes stale error details). */
export function deriveRunErrorDetail(events: AgentEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind !== "status") continue;
    return e.status === "ERROR" ? (e.detail ?? null) : null;
  }
  return null;
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
  /** Honest provider-outage label for the current parked run, or null. */
  driverOutage: DriverOutage | null;
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
  const driverOutage = useMemo(() => deriveDriverOutage(state.events), [state.events]);
  // ERROR-detail honesty: surface the backend's real StatusEvent(ERROR) detail
  // when no live error frame set state.error (replay/reconnect path included).
  const runErrorDetail = useMemo(() => deriveRunErrorDetail(state.events), [state.events]);
  const awaitingPlan = state.status === "AWAITING_PLAN_APPROVAL";
  const awaitingDecision = state.status === "AWAITING_USER_DECISION";
  const awaitingQuestion = state.status === "AWAITING_USER_QUESTION";

  return {
    ...state,
    // Transport/event error frames win; otherwise the latest StatusEvent(ERROR)
    // detail (verbatim from the backend) replaces the silent null that made the
    // UI show a generic "the run failed" fallback.
    error: state.error ?? runErrorDetail,
    pendingAction,
    pendingAlternatives,
    pendingQuestion,
    pendingClarify,
    pendingQuestionsV2,
    plan,
    planProgress,
    buildProgress,
    driverOutage,
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
