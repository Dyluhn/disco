/**
 * wsRecorder — attach WebSocket event recording to a Playwright page.
 *
 * Records open / framesent / framereceived / close events into
 * websockets.jsonl via the provided Recorder.  Conversation IDs found in WS
 * URLs or JSON payloads are forwarded to recorder.noteConversation so the
 * per-cid evidence sub-folder is created immediately on discovery.
 *
 * evidence-harness-campaign.md W11
 */

import type { Page } from "@playwright/test";
import type { Recorder } from "./recorder";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Parse {@link data} as JSON; return the original string on parse failure.
 * Never throws.
 */
function safeJson(data: string): unknown {
  try {
    return JSON.parse(data) as unknown;
  } catch {
    return data;
  }
}

/**
 * Convert a WebSocket frame payload (string or Buffer) to a string, then
 * attempt JSON parsing.
 */
function parsePayload(payload: string | Buffer): unknown {
  const raw = typeof payload === "string" ? payload : payload.toString("utf-8");
  return safeJson(raw);
}

/**
 * Extract a conversation_id from a WS URL or parsed payload.
 *
 * URL pattern:   .../conversations/{cid}[/...]
 * Payload keys:  conversation_id | cid
 */
function extractCid(wsUrl: string, payload: unknown): string | null {
  // Try URL first — /ws/conversations/{cid} or /conversations/{cid}
  const m = wsUrl.match(/\/conversations\/([a-zA-Z0-9_-]+)/);
  if (m?.[1]) return m[1];

  // Try payload object
  if (payload !== null && typeof payload === "object") {
    const p = payload as Record<string, unknown>;
    const v = p["conversation_id"] ?? p["cid"];
    if (typeof v === "string" && v.length > 0) return v;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Attach WebSocket recording to {@link page}.
 *
 * Must be called before navigation so that the listener is in place before
 * any WS connections are opened.
 */
export function attachWsRecorder(page: Page, recorder: Recorder): void {
  page.on("websocket", (ws) => {
    const wsUrl = ws.url();

    recorder.write("websockets.jsonl", {
      event: "open",
      url: wsUrl,
      ts: new Date().toISOString(),
    });

    // Attempt cid extraction from URL immediately on open.
    const cidFromUrl = extractCid(wsUrl, null);
    if (cidFromUrl !== null) recorder.noteConversation(cidFromUrl);

    ws.on("framesent", ({ payload }) => {
      const parsed = parsePayload(payload);
      const cid = extractCid(wsUrl, parsed);
      if (cid !== null) recorder.noteConversation(cid);
      recorder.write("websockets.jsonl", {
        event: "framesent",
        url: wsUrl,
        payload: parsed,
        ts: new Date().toISOString(),
      });
    });

    ws.on("framereceived", ({ payload }) => {
      const parsed = parsePayload(payload);
      const cid = extractCid(wsUrl, parsed);
      if (cid !== null) recorder.noteConversation(cid);
      recorder.write("websockets.jsonl", {
        event: "framereceived",
        url: wsUrl,
        payload: parsed,
        ts: new Date().toISOString(),
      });
    });

    ws.on("close", () => {
      recorder.write("websockets.jsonl", {
        event: "close",
        url: wsUrl,
        ts: new Date().toISOString(),
      });
    });
  });
}
