/**
 * The live conversation WebSocket transport: reconnect-with-backoff, a stale-frame
 * watchdog, and an explicit resume cursor. The production ASGI stack does not
 * reliably apply the WebSocket route's Query(default=0) when the parameter is
 * omitted — in that shape it sends the state snapshot but never drains durable
 * history, leaving reopened runs on "Resuming…" forever — so this always sends an
 * explicit cursor, advanced only after an event was actually delivered.
 *
 * Extracted from api/agent.ts (module-size decomposition, PKG-12-FE-BUILD/
 * TS-0002). `subscribeLive` + `ConversationWsUrlFactory` remain part of the
 * public `api/agent` surface via re-export — the import path for consumers
 * (including the reconnect test) is unchanged.
 */

import type { WSClientFrame, WSServerFrame } from "@/types/agent";
import { agentWsUrl, ensureAgentSession } from "../client";
import type { AgentHandle, ConversationWsUrlFactory } from "../agent";

const _RECONNECT_DEGRADED_AFTER_ATTEMPTS = 6;
const _RECONNECT_BACKOFF_CAP_MS = 15000;
const _STALE_FRAME_TIMEOUT_MS = 45000;


export const defaultConversationWsUrl: ConversationWsUrlFactory = (
  conversationId,
  lastSeq,
) =>
  agentWsUrl(
    `/ws/conversations/${encodeURIComponent(conversationId)}?last_seq=${lastSeq}`,
  )!;

export function subscribeLive(
  cid: string,
  onFrame: (f: WSServerFrame) => void,
  wsUrlForCursor: ConversationWsUrlFactory = defaultConversationWsUrl,
): AgentHandle {
  let closed = false;
  let ws: WebSocket | null = null;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let watchdogTimer: ReturnType<typeof setTimeout> | null = null;
  let degraded = false;
  // The production ASGI stack does not reliably apply the WebSocket route's
  // Query(default=0) when the parameter is omitted. In that shape it sends the
  // state snapshot but never drains durable history, leaving reopened runs on
  // "Resuming…" forever. Always send an explicit cursor. Advance it only after
  // an event was actually delivered: the state frame's `last_seq` is a server
  // watermark, not proof this client received every event up to that point.
  let lastSeq = 0;
  const queue: WSClientFrame[] = [];

  const clearWatchdog = () => {
    if (watchdogTimer) clearTimeout(watchdogTimer);
    watchdogTimer = null;
  };

  const armWatchdog = (socket: WebSocket) => {
    clearWatchdog();
    if (closed) return;
    watchdogTimer = setTimeout(() => {
      if (closed || ws !== socket || socket.readyState !== WebSocket.OPEN) return;
      socket.close();
    }, _STALE_FRAME_TIMEOUT_MS);
  };

  const resetAttemptOnVisible = () => {
    if (typeof document !== "undefined" && document.visibilityState === "visible") {
      attempt = 0;
    }
  };

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", resetAttemptOnVisible);
  }

  async function connect() {
    timer = null;
    await ensureAgentSession();
    if (closed) return;
    const socket = new WebSocket(wsUrlForCursor(cid, lastSeq));
    ws = socket;
    socket.onopen = () => {
      if (closed || ws !== socket) return;
      attempt = 0; // a successful connection resets the backoff
      armWatchdog(socket);
      if (degraded) {
        degraded = false;
        onFrame({ type: "connection", state: "connected" });
      }
      for (const f of queue) socket.send(JSON.stringify(f));
      queue.length = 0;
    };
    socket.onmessage = (ev) => {
      if (closed || ws !== socket) return;
      armWatchdog(socket);
      try {
        const frame = JSON.parse(ev.data) as WSServerFrame;
        if (frame.type === "event" && typeof frame.event.seq === "number") {
          lastSeq = Math.max(lastSeq, frame.event.seq);
        }
        onFrame(frame);
      } catch {
        onFrame({ type: "error", error: { detail: "malformed frame from server" } });
      }
    };
    // onclose is the definitive "connection ended" signal (onerror fires first but
    // a close always follows); reconnect from here.
    socket.onclose = () => {
      if (closed || ws !== socket) return;
      clearWatchdog();
      ws = null;
      attempt += 1;
      if (attempt > _RECONNECT_DEGRADED_AFTER_ATTEMPTS && !degraded) {
        degraded = true;
        onFrame({ type: "connection", state: "degraded" });
      }
      const delay = Math.min(1000 * 2 ** (attempt - 1), _RECONNECT_BACKOFF_CAP_MS);
      timer = setTimeout(() => void connect(), delay);
    };
    socket.onerror = () => {
      /* onclose handles teardown + reconnect */
    };
  }

  void connect();

  return {
    send: (f) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(f));
      else queue.push(f);
    },
    cancel: () => {
      closed = true;
      if (timer) clearTimeout(timer);
      clearWatchdog();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", resetAttemptOnVisible);
      }
      ws?.close();
    },
  };
}
