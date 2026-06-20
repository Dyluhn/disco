/**
 * The Build (agent) surface data-access layer — the ONE place that talks to the
 * agent-server's conversation WebSocket + REST. Components never import this; only the
 * Build hooks do (the data-flow discipline). Live when VITE_AGENT_BASE is set; otherwise
 * it replays the in-repo agent-trace fixture (offline/tests/screenshots), including the
 * confirmation-gate pause that confirm/reject resume.
 */

import {
  askGateState,
  askQuestionEvent,
  clarifyEvent,
  clarifyGateState,
  finishedState,
  gateState,
  planEvent,
  planGateState,
  replanEvent,
  replanGateState,
  traceAfterConfirm,
  traceAfterReject,
  traceBeforeGate,
  FIXTURE_CID,
} from "@/fixtures/agentTrace";
import {
  FIXTURE_DEEP_CID,
  fixtureFinishedState,
  fixtureInitialState,
  fixturePlan,
  fixtureReport,
  fixtureRunningEvents,
} from "@/fixtures/deepResearchTrace";
import type {
  ConversationState,
  DriverModels,
  PreviewInfo,
  WSClientFrame,
  WSServerFrame,
} from "@/types/agent";
import type { JsonPatchOp, LoweredDeck } from "@/components/build/editor/types";
import { agentGet, agentHttpBase, agentLive, agentSend, agentWsUrl, fixtureDelay } from "./client";

export interface AgentHandle {
  /** Send a client frame (send_message / confirm / reject / cancel). */
  send: (frame: WSClientFrame) => void;
  /** Tear down the subscription. */
  cancel: () => void;
}

/** Create a build-like conversation (the agent loop + the ConfirmRisky gate),
 * optionally pinning the driver model for it (the chat model picker). The surface
 * is "build" (software framing) or "agent" (general-task framing) — identical
 * machinery, so the same create path serves both. */
export async function createBuildConversation(
  modelOverride?: string | null,
  surface: "build" | "agent" = "build",
  autonomous = false,
): Promise<string> {
  if (!agentLive()) return FIXTURE_CID;
  const res = await agentSend<{ conversation_id: string }>("POST", "/conversations", {
    owner_id: import.meta.env.VITE_OWNER_ID ?? "local",
    surface,
    model_override: modelOverride ?? null,
    autonomous,
  });
  return res.conversation_id;
}

/** runthru-v2 ROOT-1: apply the user's model pick (and autonomous choice) to a
 * PRE-CREATED conversation right before the kick. The build surface pre-creates a
 * cid on mount with defaults, so without this the picker's value was dropped and the
 * run used the default model (local Qwen) instead of what the user chose. The server
 * 409s if the loop already started (the model is fixed once a run begins). */
export async function patchConversationSettings(
  conversationId: string,
  settings: { modelOverride?: string | null; autonomous?: boolean },
): Promise<void> {
  if (!agentLive()) return;
  await agentSend("PATCH", `/conversations/${conversationId}/settings`, {
    model_override: settings.modelOverride ?? null,
    ...(settings.autonomous !== undefined ? { autonomous: settings.autonomous } : {}),
  });
}

/** The driver-eligible models for the Build chat picker (+ the default). Offline → a
 * small fixture so the picker renders in tests/screenshots. */
export async function listDriverModels(): Promise<DriverModels> {
  if (!agentLive())
    return {
      models: [
        { id: "driver-local", label: "Qwen3.6-27B", provider: "local", free: true, context_window: 131072 },
        { id: "driver-overflow", label: "claude-3.5-sonnet", provider: "openrouter", free: false, context_window: 200000 },
      ],
      default: "driver-local",
    };
  return agentGet<DriverModels>("/models");
}

/** P3 — the globally-persisted last-picked driver model from the agent-server.
 * Returns null when no pick has ever been made. Fixture: null (no stored pick
 * offline so the pill falls back to the settings default as before). */
export async function getLastSelectedModel(): Promise<string | null> {
  if (!agentLive()) return null;
  const r = await agentGet<{ model: string | null }>("/models/last-selected");
  return r.model ?? null;
}

/** The backend-aware live preview URL for a conversation's sandbox (or why not). */
export async function getPreview(cid: string): Promise<PreviewInfo> {
  if (!agentLive())
    return { available: false, reason: "Live preview runs against the agent-server (offline here)." };
  return agentGet<PreviewInfo>(`/conversations/${cid}/preview`);
}

/** Bring a down preview back (§E7): bounded, idempotent restart of the static
 *  serve on the conversation's sandbox. Returns whether a server is now up. */
export async function restartPreview(cid: string): Promise<boolean> {
  if (!agentLive()) return false;
  const r = await agentSend<{ ok: boolean }>("POST", `/conversations/${cid}/preview/restart`);
  return Boolean(r?.ok);
}

export interface UploadResult {
  saved: { name: string; bytes: number }[];
  rejected: { name: string; reason: string }[];
}

/** Upload files into the conversation's sandbox under uploads/ (BP-11). */
export async function uploadFiles(cid: string, files: File[]): Promise<UploadResult> {
  if (!agentLive()) return { saved: [], rejected: [] };
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const res = await fetch(`${agentHttpBase()}/conversations/${cid}/files`, {
    method: "POST",
    body: fd,
  });
  const body = (await res.json()) as UploadResult & { detail?: unknown };
  if (!res.ok && res.status !== 413) {
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : `Upload failed (${res.status})`,
    );
  }
  return body as UploadResult;
}

// ---- A2: in-app deck editor (GET lowered deck / PUT patch) -----------------

/** A small fixture LoweredDeck so the editor pane renders offline (tests/screenshots). */
const _FIXTURE_LOWERED_DECK: LoweredDeck = {
  title: "Sample Deck",
  theme_name: "disco",
  theme_mode: "light",
  slides: [
    {
      slide_id: "slide-0",
      slide_idx: 0,
      layout: "title",
      bg_color: "#ffffff",
      elements: [
        {
          element_id: "slide-0:title",
          slide_id: "slide-0",
          kind: "title",
          content: "Sample Title",
          geometry: { x: 4, y: 7, w: 92, h: 13 },
          font_size_vw: 2.5,
          font_weight: "bold",
          font_style: "normal",
          json_pointer: "/slides/0/title",
        },
      ],
    },
  ],
};

/** A2.1: fetch the LoweredDeck for an editable deck (its `{base}.authored.json`).
 * Offline → the fixture deck so the editor renders in tests/screenshots. */
export async function getDeckForEditor(cid: string, base: string): Promise<LoweredDeck> {
  if (!agentLive()) return _FIXTURE_LOWERED_DECK;
  return agentGet<LoweredDeck>(
    `/conversations/${encodeURIComponent(cid)}/deck/editor?path=${encodeURIComponent(base)}`,
  );
}

export interface DeckPatchResult {
  ok: boolean;
  lowered: LoweredDeck;
  html_file: string;
  pptx_file: string;
  pdf_stale?: boolean;
}

/** A2.3: apply an RFC-6902 patch to the deck, re-render, and return the new
 * server-authoritative LoweredDeck. Throws ApiError on failure — notably 409
 * (no live sandbox: the build's workspace is suspended) and 422 (rejected patch);
 * the caller surfaces those distinctly. Offline → echo the fixture deck. */
export async function patchDeck(
  cid: string,
  base: string,
  patch: JsonPatchOp[],
): Promise<DeckPatchResult> {
  if (!agentLive())
    return {
      ok: true,
      lowered: _FIXTURE_LOWERED_DECK,
      html_file: `${base}.html`,
      pptx_file: `${base}.pptx`,
    };
  return agentSend<DeckPatchResult>(
    "PUT",
    `/conversations/${encodeURIComponent(cid)}/deck/editor?path=${encodeURIComponent(base)}`,
    { patch },
  );
}

/** The kill switch (BoD §13.6): halt, tear down the sandbox, revoke capabilities. */
export async function killConversation(cid: string): Promise<void> {
  if (!agentLive()) return; // offline: the hook marks the run stopped
  await agentSend("POST", `/conversations/${cid}/kill`);
}

export interface SessionInfo {
  name: string;
  busy: boolean;
  last_line: string;
}

export interface SessionView {
  name: string;
  busy: boolean;
  content: string;
}

/** List live tmux sessions for a conversation's sandbox (BP-14). */
export async function getSessions(cid: string): Promise<{ sessions: SessionInfo[] }> {
  if (!agentLive()) return { sessions: [] };
  return agentGet<{ sessions: SessionInfo[] }>(`/conversations/${cid}/sessions`);
}

/** Capture-pane tail for one named session (BP-14). */
export async function getSessionView(
  cid: string,
  name: string,
  tailChars = 10_000,
): Promise<SessionView> {
  return agentGet<SessionView>(
    `/conversations/${cid}/sessions/${encodeURIComponent(name)}/view?tail_chars=${tailChars}`,
  );
}

/** Resume a PAUSED or interrupted-with-unfinished-plan conversation (BP-12). */
export async function resumeConversation(
  cid: string,
): Promise<{ ok: boolean; status?: string; reason?: string }> {
  if (!agentLive()) return { ok: true, status: "RUNNING" };
  return agentSend<{ ok: boolean; status?: string; reason?: string }>(
    "POST",
    `/conversations/${cid}/resume`,
  );
}

// ---- live: the conversation WebSocket (history-then-live) -------------------

// Reconnect with exponential backoff. A dropped socket (network blip, server
// restart) used to send a FATAL error frame and die — a transient drop became a
// permanent failure. Now we reconnect silently: on reopen the server replays
// history-then-live and the reducers dedup events by id, so no work is lost. Only
// after exhausting retries do we surface the error.
const _RECONNECT_MAX_ATTEMPTS = 6;

// exported for the reconnect test; not part of the public api (use subscribeConversation)
export function subscribeLive(cid: string, onFrame: (f: WSServerFrame) => void): AgentHandle {
  let closed = false;
  let ws: WebSocket | null = null;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const queue: WSClientFrame[] = [];

  function connect() {
    ws = new WebSocket(agentWsUrl(`/ws/conversations/${cid}`)!);
    ws.onopen = () => {
      attempt = 0; // a successful connection resets the backoff
      for (const f of queue) ws?.send(JSON.stringify(f));
      queue.length = 0;
    };
    ws.onmessage = (ev) => {
      if (closed) return;
      try {
        onFrame(JSON.parse(ev.data) as WSServerFrame);
      } catch {
        onFrame({ type: "error", error: { detail: "malformed frame from server" } });
      }
    };
    // onclose is the definitive "connection ended" signal (onerror fires first but
    // a close always follows); reconnect from here.
    ws.onclose = () => {
      if (closed) return;
      attempt += 1;
      if (attempt > _RECONNECT_MAX_ATTEMPTS) {
        onFrame({
          type: "error",
          error: { detail: "lost connection to the agent server (couldn't reconnect)" },
        });
        return;
      }
      const delay = Math.min(1000 * 2 ** (attempt - 1), 15000); // 1s,2s,…capped 15s
      timer = setTimeout(connect, delay);
    };
    ws.onerror = () => {
      /* onclose handles teardown + reconnect */
    };
  }

  connect();

  return {
    send: (f) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(f));
      else queue.push(f);
    },
    cancel: () => {
      closed = true;
      if (timer) clearTimeout(timer);
      ws?.close();
    },
  };
}

// ---- offline: replay the fixture trace, pausing at the gate -----------------

function runningState(): ConversationState {
  return { ...gateState, execution_status: "RUNNING", pending_action_id: null, pending_plan_id: null };
}

function subscribeFixture(onFrame: (f: WSServerFrame) => void): AgentHandle {
  let cancelled = false;
  let atGate = false; // paused at the per-action confirmation gate
  let atPlan = false; // paused at the plan-approval gate
  let atAsk = false; // paused at the free-form Ask-gate (AWAITING_USER_QUESTION)

  async function emit(events: typeof traceBeforeGate, final: ConversationState | null) {
    for (const event of events) {
      if (cancelled) return;
      await fixtureDelay(120);
      onFrame({ type: "event", event });
    }
    if (final && !cancelled) onFrame({ type: "state", state: final });
  }

  // Plan-first: a new task (or a re-plan request) proposes a plan and pauses for approval.
  async function propose(event: typeof planEvent, gate: ConversationState) {
    onFrame({ type: "state", state: runningState() });
    if (cancelled) return;
    await fixtureDelay(120);
    onFrame({ type: "event", event });
    if (cancelled) return;
    atPlan = true;
    onFrame({ type: "state", state: gate });
  }

  // After plan approval: run the build trace up to the per-action confirmation gate.
  async function build() {
    onFrame({ type: "state", state: runningState() });
    await emit(traceBeforeGate, null);
    if (cancelled) return;
    atGate = true;
    onFrame({ type: "state", state: gateState });
  }

  // The two-way Ask-gate demo: the agent poses a free-form question and parks at
  // AWAITING_USER_QUESTION so the AskPanel is reachable offline. The user's typed
  // answer (a steer frame) resumes the run to finished.
  async function askDemo() {
    onFrame({ type: "state", state: runningState() });
    if (cancelled) return;
    await fixtureDelay(120);
    onFrame({ type: "event", event: askQuestionEvent });
    if (cancelled) return;
    atAsk = true;
    onFrame({ type: "state", state: askGateState });
  }

  // The pre-plan Clarify gate demo (RP-13): the planner asks several TYPED
  // questions and parks at AWAITING_USER_QUESTION with a clarify card.
  async function clarifyDemo() {
    onFrame({ type: "state", state: runningState() });
    if (cancelled) return;
    await fixtureDelay(120);
    onFrame({ type: "event", event: clarifyEvent });
    if (cancelled) return;
    atAsk = true;
    onFrame({ type: "state", state: clarifyGateState });
  }

  return {
    send: (f) => {
      if (f.type === "send_message")
        // A task mentioning "clarify" routes to the pre-plan Clarify gate demo;
        // "ask" routes to the free-form Ask-gate; everything else follows the
        // normal plan-first flow.
        void (
          /\bclarif/i.test(f.content ?? "")
            ? clarifyDemo()
            : /\bask\b/i.test(f.content ?? "")
              ? askDemo()
              : propose(planEvent, planGateState)
        );
      else if (f.type === "steer" && atAsk) {
        // the answer to the agent's question → resume + finish
        atAsk = false;
        onFrame({ type: "state", state: runningState() });
        void emit(traceAfterConfirm, finishedState);
      } else if (f.type === "approve_plan" && atPlan) {
        atPlan = false;
        void build();
      } else if (f.type === "request_plan") {
        // (Re-)enter plan mode — a focused revision after the build (or a re-propose).
        atGate = false;
        void propose(replanEvent, replanGateState);
      } else if (f.type === "confirm" && atGate) {
        atGate = false;
        onFrame({ type: "state", state: runningState() });
        void emit(traceAfterConfirm, finishedState);
      } else if (f.type === "reject" && atGate) {
        atGate = false;
        onFrame({ type: "state", state: runningState() });
        void emit(traceAfterReject, finishedState);
      } else if (f.type === "cancel") {
        cancelled = true;
        onFrame({ type: "state", state: { ...finishedState, execution_status: "IDLE" } });
      }
    },
    cancel: () => {
      cancelled = true;
    },
  };
}

// ---- offline: Deep Research fixture replay ---------------------------------

function subscribeDeepFixture(onFrame: (f: WSServerFrame) => void): AgentHandle {
  let cancelled = false;
  let atPlan = false;

  async function streamUntilGate() {
    onFrame({ type: "state", state: fixtureInitialState });
    // Send events up to the AWAITING_PLAN_APPROVAL gate (the first 4 events:
    // user msg, RUNNING, plan, gate-status).
    for (let i = 0; i < 4; i++) {
      if (cancelled) return;
      await fixtureDelay(80);
      onFrame({ type: "event", event: fixtureRunningEvents[i] });
    }
    atPlan = true;
  }

  async function streamRunAndReport() {
    // Stream the post-approval events (plan_approved → phases → actions → obs)
    // with small delays so the live progress feel is preserved.
    for (let i = 4; i < fixtureRunningEvents.length; i++) {
      if (cancelled) return;
      await fixtureDelay(90);
      onFrame({ type: "event", event: fixtureRunningEvents[i] });
    }
    if (cancelled) return;
    await fixtureDelay(150);
    onFrame({ type: "event", event: fixtureReport });
    if (cancelled) return;
    onFrame({
      type: "event",
      event: {
        id: "evt_finished",
        kind: "status",
        source: "system",
        seq: 80,
        timestamp: "2026-06-06T12:00:00Z",
        status: "FINISHED",
      },
    });
    onFrame({ type: "state", state: fixtureFinishedState });
  }

  return {
    send: (f) => {
      if (f.type === "send_message") void streamUntilGate();
      else if (f.type === "approve_plan" && atPlan) {
        atPlan = false;
        void streamRunAndReport();
      } else if (f.type === "request_plan") {
        // re-plan: re-emit a (slightly modified) plan and the gate. For
        // fixture simplicity we just bump revision and re-gate; tests can
        // observe the new revision on the next event.
        atPlan = true;
        void (async () => {
          await fixtureDelay(80);
          onFrame({
            type: "event",
            event: {
              ...fixturePlan,
              id: "evt_replan",
              seq: 5,
              revision: 2,
              summary: `Revised plan: ${f.content ?? ""}`,
            },
          });
        })();
      } else if (f.type === "cancel") {
        cancelled = true;
        onFrame({
          type: "state",
          state: { ...fixtureFinishedState, execution_status: "IDLE" },
        });
      }
    },
    cancel: () => {
      cancelled = true;
    },
  };
}

export function subscribeConversation(
  cid: string,
  onFrame: (f: WSServerFrame) => void,
): AgentHandle {
  if (agentLive()) return subscribeLive(cid, onFrame);
  // Dispatch by cid: the Deep Research fixture lives in its own module so the
  // Build fixture stays unmodified.
  if (cid === FIXTURE_DEEP_CID) return subscribeDeepFixture(onFrame);
  return subscribeFixture(onFrame);
}

// ---- share (RP-06) ---------------------------------------------------------

export interface ShareLink {
  ok: boolean;
  url?: string;
  token: string;
  conversation_id: string;
  owner_id?: string;
  bundle_seq?: number;
  /** Present when create_share fails (e.g. conversation not found). */
  reason?: string;
}

/** Create a revocable share token for a conversation. Returns the token + the
 *  URL slug (`/share/<token>`) the frontend can render as a link.
 *  Double-issuing the same conversation is idempotent. */
export async function createShare(conversationId: string): Promise<ShareLink> {
  if (!agentLive()) {
    return {
      ok: false,
      reason: "Share links require the agent server (offline here).",
      token: "",
      conversation_id: conversationId,
    };
  }
  return agentSend<ShareLink>("POST", `/conversations/${conversationId}/share`);
}

/** The redacted, versioned JSON bundle for a shared conversation. The static
 *  viewer (ShareView) consumes this — zero WebSocket dependency. */
export interface ShareBundle {
  bundle_version: number;
  conversation_id: string;
  owner_id: string;
  surface: string;
  title: string;
  exported_at: string;
  last_seq: number;
  state: Record<string, unknown>;
  events: Array<Record<string, unknown>>;
  share?: {
    created_at?: string;
    bundle_seq?: number;
    revoked?: boolean;
  };
}

/** Fetch the scrubbed bundle for a share token. The endpoint resolves the token,
 *  re-exports the event log, and returns the versioned bundle. Returns null when
 *  the token is invalid or revoked (the two are intentionally conflated — a probe
 *  can't distinguish a revoked link from one that never existed). */
export async function fetchShareBundle(token: string): Promise<ShareBundle | null> {
  if (!agentLive()) return null;
  try {
    const bundle = await agentGet<ShareBundle>(`/api/share/${encodeURIComponent(token)}/bundle`);
    return bundle;
  } catch {
    // HTTP 404 or 403 — token invalid or revoked
    return null;
  }
}

/** Import an exported bundle as a READ-ONLY local conversation. The server
 *  validates + re-scrubs + mints a fresh cid + marks it imported; returns the new
 *  cid. Throws ApiError (422) on an invalid/unsupported bundle. */
export async function importShareBundle(bundle: unknown): Promise<{ conversation_id: string }> {
  return agentSend<{ conversation_id: string }>("POST", "/api/share/import", bundle);
}

/** Fetch a finished conversation's event log + final status for a static (read-only)
 *  render — used by ImportedRunView. No WebSocket: the log is already complete. */
export async function fetchConversationRun(
  cid: string,
): Promise<{ events: Array<Record<string, unknown>>; status: string }> {
  const ev = await agentGet<{ events: Array<Record<string, unknown>> }>(
    `/conversations/${encodeURIComponent(cid)}/events`,
  );
  const state = await agentGet<{ execution_status?: string }>(
    `/conversations/${encodeURIComponent(cid)}/state`,
  );
  return { events: ev.events ?? [], status: state.execution_status ?? "FINISHED" };
}
