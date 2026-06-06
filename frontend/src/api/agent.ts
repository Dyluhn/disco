/**
 * The Build (agent) surface data-access layer — the ONE place that talks to the
 * agent-server's conversation WebSocket + REST. Components never import this; only the
 * Build hooks do (the data-flow discipline). Live when VITE_AGENT_BASE is set; otherwise
 * it replays the in-repo agent-trace fixture (offline/tests/screenshots), including the
 * confirmation-gate pause that confirm/reject resume.
 */

import {
  finishedState,
  gateState,
  traceAfterConfirm,
  traceAfterReject,
  traceBeforeGate,
  FIXTURE_CID,
} from "@/fixtures/agentTrace";
import type {
  ConversationState,
  DriverModels,
  PreviewInfo,
  WSClientFrame,
  WSServerFrame,
} from "@/types/agent";
import { agentGet, agentLive, agentSend, agentWsUrl, fixtureDelay } from "./client";

export interface AgentHandle {
  /** Send a client frame (send_message / confirm / reject / cancel). */
  send: (frame: WSClientFrame) => void;
  /** Tear down the subscription. */
  cancel: () => void;
}

/** Create a Build-surface conversation (the agent loop + the ConfirmRisky gate),
 * optionally pinning the driver model for it (the chat model picker). */
export async function createBuildConversation(modelOverride?: string | null): Promise<string> {
  if (!agentLive()) return FIXTURE_CID;
  const res = await agentSend<{ conversation_id: string }>("POST", "/conversations", {
    owner_id: import.meta.env.VITE_OWNER_ID ?? "local",
    surface: "build",
    model_override: modelOverride ?? null,
  });
  return res.conversation_id;
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

/** The backend-aware live preview URL for a conversation's sandbox (or why not). */
export async function getPreview(cid: string): Promise<PreviewInfo> {
  if (!agentLive())
    return { available: false, reason: "Live preview runs against the agent-server (offline here)." };
  return agentGet<PreviewInfo>(`/conversations/${cid}/preview`);
}

/** The kill switch (BoD §13.6): halt, tear down the sandbox, revoke capabilities. */
export async function killConversation(cid: string): Promise<void> {
  if (!agentLive()) return; // offline: the hook marks the run stopped
  await agentSend("POST", `/conversations/${cid}/kill`);
}

// ---- live: the conversation WebSocket (history-then-live) -------------------

function subscribeLive(cid: string, onFrame: (f: WSServerFrame) => void): AgentHandle {
  let closed = false;
  const queue: WSClientFrame[] = [];
  const ws = new WebSocket(agentWsUrl(`/ws/conversations/${cid}`)!);
  ws.onopen = () => {
    for (const f of queue) ws.send(JSON.stringify(f));
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
  ws.onerror = () => {
    if (!closed) onFrame({ type: "error", error: { detail: "connection to the agent server failed" } });
  };
  return {
    send: (f) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(f));
      else queue.push(f);
    },
    cancel: () => {
      closed = true;
      ws.close();
    },
  };
}

// ---- offline: replay the fixture trace, pausing at the gate -----------------

function runningState(): ConversationState {
  return { ...gateState, execution_status: "RUNNING", pending_action_id: null };
}

function subscribeFixture(onFrame: (f: WSServerFrame) => void): AgentHandle {
  let cancelled = false;
  let atGate = false;

  async function emit(events: typeof traceBeforeGate, final: ConversationState | null) {
    for (const event of events) {
      if (cancelled) return;
      await fixtureDelay(120);
      onFrame({ type: "event", event });
    }
    if (final && !cancelled) onFrame({ type: "state", state: final });
  }

  async function start() {
    onFrame({ type: "state", state: runningState() });
    await emit(traceBeforeGate, null);
    if (cancelled) return;
    atGate = true;
    onFrame({ type: "state", state: gateState }); // pause for confirmation
  }

  return {
    send: (f) => {
      if (f.type === "send_message") void start();
      else if (f.type === "confirm" && atGate) {
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

export function subscribeConversation(
  cid: string,
  onFrame: (f: WSServerFrame) => void,
): AgentHandle {
  return agentLive() ? subscribeLive(cid, onFrame) : subscribeFixture(onFrame);
}
