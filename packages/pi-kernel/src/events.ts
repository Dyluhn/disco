/**
 * Map Pi `AgentSessionEvent`s onto the protocol's {@link MappedAgentEvent}
 * envelope (Disco Pi Build Kernel Campaign, EPIC B / PR B1).
 *
 * Goals:
 * - Faithful: `kind` mirrors the Pi event `type` and every salient field is
 *   carried through, so EPIC I can map onto Disco MessageEvent / PlanEvent /
 *   StatusEvent / ActionEvent / ObservationEvent without re-reading Pi internals.
 * - JSON-safe + bounded: message/tool payloads can carry images (base64) and
 *   large tool output, so all extracted values pass through {@link toJsonSafe}
 *   which drops functions, breaks cycles, and truncates oversized strings.
 *
 * The mapper treats Pi message/content objects structurally (defensive runtime
 * guards) rather than importing their exact union, so it survives Pi point
 * releases that reshape content blocks.
 */
import type { AgentSessionEvent } from "@earendil-works/pi-coding-agent";
import type { MappedAgentEvent } from "./protocol.ts";

/** Strings longer than this are truncated in the JSON-safe projection. */
const MAX_STRING = 16_384;
/** Hard recursion guard for pathological nesting. */
const MAX_DEPTH = 12;

/**
 * Produce a structurally-cloned, JSON-safe value: functions/symbols dropped,
 * cycles replaced with `"[Circular]"`, over-long strings truncated with a
 * `…(+N)` suffix, and depth bounded. Used on every field extracted from Pi.
 */
export function toJsonSafe(value: unknown, depth = 0, seen = new WeakSet<object>()): unknown {
  if (value === null) return null;
  const t = typeof value;
  if (t === "string") {
    const s = value as string;
    return s.length > MAX_STRING ? `${s.slice(0, MAX_STRING)}…(+${s.length - MAX_STRING})` : s;
  }
  if (t === "number") return Number.isFinite(value as number) ? value : String(value);
  if (t === "boolean" || t === "bigint") return t === "bigint" ? String(value) : value;
  if (t === "function" || t === "symbol" || t === "undefined") return undefined;
  if (depth >= MAX_DEPTH) return "[MaxDepth]";
  const obj = value as object;
  if (seen.has(obj)) return "[Circular]";
  seen.add(obj);
  try {
    if (Array.isArray(value)) {
      return value.map((v) => toJsonSafe(v, depth + 1, seen));
    }
    // Honor a custom toJSON (e.g. Date) before generic enumeration.
    const maybe = value as { toJSON?: () => unknown };
    if (typeof maybe.toJSON === "function") {
      return toJsonSafe(maybe.toJSON(), depth + 1, seen);
    }
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      const mapped = toJsonSafe(v, depth + 1, seen);
      if (mapped !== undefined) out[k] = mapped;
    }
    return out;
  } finally {
    seen.delete(obj);
  }
}

/** Concatenated text + a list of non-text content-part kinds for a message. */
interface MessageSummary {
  role: unknown;
  text: string;
  /** Content-part `type`s in order (e.g. "text", "image", "tool_call"). */
  parts: string[];
  /** Full content, JSON-safe + bounded, for lossless downstream mapping. */
  content: unknown;
}

/** Summarize a Pi `AgentMessage` defensively (it may be any union member). */
function summarizeMessage(message: unknown): MessageSummary | undefined {
  if (typeof message !== "object" || message === null) return undefined;
  const m = message as { role?: unknown; content?: unknown };
  const parts: string[] = [];
  let text = "";
  const content = m.content;
  if (typeof content === "string") {
    text = content;
    parts.push("text");
  } else if (Array.isArray(content)) {
    for (const block of content) {
      if (typeof block !== "object" || block === null) continue;
      const b = block as { type?: unknown; text?: unknown };
      const type = typeof b.type === "string" ? b.type : "unknown";
      parts.push(type);
      if (type === "text" && typeof b.text === "string") {
        text += b.text;
      }
    }
  }
  return {
    role: m.role,
    text,
    parts,
    content: toJsonSafe(content),
  };
}

/**
 * Map one Pi `AgentSessionEvent` to a {@link MappedAgentEvent}. Unknown event
 * types still round-trip: `kind` is preserved and the whole event is carried
 * under `raw` so nothing is silently lost across Pi versions.
 */
export function mapAgentEvent(event: AgentSessionEvent): MappedAgentEvent {
  const e = event as { type: string } & Record<string, unknown>;
  const base: MappedAgentEvent = { kind: e.type };

  switch (e.type) {
    case "agent_start":
    case "turn_start":
      return base;

    case "agent_end":
      return {
        ...base,
        willRetry: Boolean(e.willRetry),
        messageCount: Array.isArray(e.messages) ? e.messages.length : 0,
        messages: Array.isArray(e.messages)
          ? e.messages.map((m) => summarizeMessage(m))
          : [],
      };

    case "turn_end":
      return {
        ...base,
        message: summarizeMessage(e.message),
        toolResults: Array.isArray(e.toolResults)
          ? e.toolResults.map((r) => summarizeMessage(r))
          : [],
      };

    case "message_start":
    case "message_end":
      return { ...base, message: summarizeMessage(e.message) };

    case "message_update":
      // Drop the high-frequency streaming delta payload; keep the running
      // message snapshot. Token frames are ephemeral — the snapshot is truth.
      return { ...base, message: summarizeMessage(e.message) };

    case "tool_execution_start":
      return {
        ...base,
        toolCallId: e.toolCallId,
        toolName: e.toolName,
        args: toJsonSafe(e.args),
      };

    case "tool_execution_update":
      return {
        ...base,
        toolCallId: e.toolCallId,
        toolName: e.toolName,
        args: toJsonSafe(e.args),
        partialResult: toJsonSafe(e.partialResult),
      };

    case "tool_execution_end":
      return {
        ...base,
        toolCallId: e.toolCallId,
        toolName: e.toolName,
        result: toJsonSafe(e.result),
        isError: Boolean(e.isError),
      };

    case "queue_update":
      return {
        ...base,
        steering: toJsonSafe(e.steering),
        followUp: toJsonSafe(e.followUp),
      };

    case "compaction_start":
      return { ...base, reason: e.reason };

    case "compaction_end":
      return {
        ...base,
        reason: e.reason,
        result: toJsonSafe(e.result),
        aborted: Boolean(e.aborted),
        willRetry: Boolean(e.willRetry),
        errorMessage: e.errorMessage,
      };

    case "session_info_changed":
      return { ...base, name: e.name };

    case "thinking_level_changed":
      return { ...base, level: e.level };

    case "auto_retry_start":
      return {
        ...base,
        attempt: e.attempt,
        maxAttempts: e.maxAttempts,
        delayMs: e.delayMs,
        errorMessage: e.errorMessage,
      };

    case "auto_retry_end":
      return {
        ...base,
        success: Boolean(e.success),
        attempt: e.attempt,
        finalError: e.finalError,
      };

    default:
      // Forward-compatible: preserve the whole event so EPIC I never loses a
      // newly-introduced Pi event type.
      return { ...base, raw: toJsonSafe(event) };
  }
}
