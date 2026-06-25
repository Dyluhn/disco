/**
 * Line protocol between the Pi SDK sidecar (this Node package) and the Python
 * process manager (Disco Pi Build Kernel Campaign, EPIC B / PR B2).
 *
 * Wire format: newline-delimited JSON. Each line is exactly one JSON object —
 * inbound a {@link KernelCommand}, outbound a {@link KernelOutbound}. No framing
 * beyond the trailing `\n`; both sides MUST ignore blank lines and SHOULD treat
 * an unparseable inbound line as a protocol error (never crash).
 *
 * This module is types + tiny pure helpers only. It imports nothing from Pi so
 * the Python side can mirror these shapes without pulling the SDK.
 */

/** Bumped when the wire shapes below change in a breaking way. */
export const PROTOCOL_VERSION = 1 as const;

// ---------------------------------------------------------------------------
// Inbound — process manager → sidecar
// ---------------------------------------------------------------------------

/**
 * The Disco inference gateway endpoint — the ONLY model/provider the kernel is
 * allowed to reach (Disco Pi Build Kernel Campaign §5.1 network invariant). The
 * spawner injects this; the kernel registers it as the sole usable provider and
 * pins the session's model to it, so no built-in provider (and no ambient
 * credential) can ever be selected. Omitted in B1, where no gateway is wired yet
 * — the session then has no model (a `prompt` fails loudly) rather than falling
 * back to any real provider.
 */
export interface KernelGatewayConfig {
  /**
   * Base URL of the loopback Disco inference gateway (e.g.
   * `http://127.0.0.1:<port>/v1`). All model traffic routes here and nowhere
   * else.
   */
  baseUrl: string;
  /** Model id exposed by the gateway and selected for this session. */
  model: string;
  /**
   * Gateway credential, injected by the spawner — NEVER read from the ambient
   * environment (those are scrubbed at startup). Required so the gateway model
   * is the only one with configured auth.
   */
  apiKey: string;
  /**
   * Wire dialect the gateway speaks. Defaults to `openai-completions` (the
   * OpenAI-compatible shape Disco's gateway exposes).
   */
  api?: string;
}

/**
 * Configuration for a kernel session. Intentionally minimal for B1: when no
 * `gateway` is supplied the model / provider is a PLACEHOLDER wired by the Disco
 * inference gateway in EPIC C, so nothing here selects a real model yet.
 * `conversationId` is an opaque tag the process manager uses to correlate this
 * kernel with a Disco conversation.
 */
export interface KernelInitConfig {
  /** Opaque correlation id for the owning Disco conversation, if any. */
  conversationId?: string;
  /**
   * Heartbeat cadence override (ms). Omitted → sidecar default. Primarily a
   * test seam; the process manager normally relies on the default.
   */
  heartbeatMs?: number;
  /**
   * The Disco inference gateway — the single reachable model/provider. When
   * present the kernel registers ONLY this provider and pins the session to it;
   * built-in providers remain registered but are never usable (their ambient
   * credentials are scrubbed at startup). When absent (B1 default), the session
   * has no model.
   */
  gateway?: KernelGatewayConfig;
}

/** Start the Pi session. Must be the first command; sending it twice errors. */
export interface InitCommand {
  type: "init";
  config?: KernelInitConfig;
}

/** Begin a new agent turn with the given user text. */
export interface PromptCommand {
  type: "prompt";
  text: string;
}

/** Queue a follow-up turn, delivered after the agent finishes the current one. */
export interface FollowupCommand {
  type: "followup";
  text: string;
}

/** Approve a pending plan / confirmation gate (reserved for later epics). */
export interface ApproveCommand {
  type: "approve";
}

/** Reject a pending plan / confirmation gate (reserved for later epics). */
export interface RejectCommand {
  type: "reject";
  reason?: string;
}

/** Abort any in-flight work and shut the sidecar down cleanly. */
export interface CancelCommand {
  type: "cancel";
}

export type KernelCommand =
  | InitCommand
  | PromptCommand
  | FollowupCommand
  | ApproveCommand
  | RejectCommand
  | CancelCommand;

export type KernelCommandType = KernelCommand["type"];

// ---------------------------------------------------------------------------
// Outbound — sidecar → process manager
// ---------------------------------------------------------------------------

/** Summary of the session's tool surface, proving the no-tools posture. */
export interface ToolSurface {
  /** Names of tools active on the agent. For a no-tools kernel this is `[]`. */
  activeToolNames: string[];
  /** Count of SDK custom tools registered. For B1 this is `0`. */
  customToolCount: number;
  /** The `noTools` mode the session was created with (always `"all"` in B1). */
  noTools: "all" | "builtin" | null;
}

/** Emitted once after `init` succeeds; the session is live and idle. */
export interface ReadyEvent {
  type: "ready";
  protocolVersion: typeof PROTOCOL_VERSION;
  /** Version of the embedded `@earendil-works/pi-coding-agent` package. */
  piVersion: string;
  /** Selected model id, or `null` while the gateway (EPIC C) is unwired. */
  model: string | null;
  tools: ToolSurface;
}

/** Periodic liveness signal while the sidecar is running. */
export interface HeartbeatEvent {
  type: "heartbeat";
  /** Epoch milliseconds at emit time. */
  ts: number;
}

/** A mapped Pi `AgentSessionEvent` (see events.ts for the envelope shape). */
export interface AgentEventMessage {
  type: "agent_event";
  event: MappedAgentEvent;
}

/** Final frame before the process exits. */
export interface ExitEvent {
  type: "exit";
  code: number;
}

/** A recoverable or fatal error surfaced to the process manager. */
export interface ErrorEvent {
  type: "error";
  message: string;
  /** When true the sidecar is shutting down; an `exit` frame follows. */
  fatal?: boolean;
}

export type KernelOutbound =
  | ReadyEvent
  | HeartbeatEvent
  | AgentEventMessage
  | ExitEvent
  | ErrorEvent;

export type KernelOutboundType = KernelOutbound["type"];

// ---------------------------------------------------------------------------
// Mapped agent event envelope (produced by events.ts)
// ---------------------------------------------------------------------------

/**
 * Faithful, JSON-safe projection of a Pi `AgentSessionEvent`. `kind` mirrors the
 * Pi event `type`; the remaining fields are extracted per-kind (see events.ts).
 * Kept lossless-enough for EPIC I to map onto Disco MessageEvent/PlanEvent/etc.
 */
export interface MappedAgentEvent {
  /** The original Pi event `type` (e.g. "agent_start", "tool_execution_end"). */
  kind: string;
  /** Per-kind extracted, JSON-safe payload. */
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

const COMMAND_TYPES: ReadonlySet<string> = new Set([
  "init",
  "prompt",
  "followup",
  "approve",
  "reject",
  "cancel",
]);

/**
 * Narrow an arbitrary parsed JSON value to a {@link KernelCommand}. Returns
 * `null` for anything that is not a well-formed command (the caller surfaces a
 * protocol `error` rather than throwing).
 */
export function parseCommand(value: unknown): KernelCommand | null {
  if (typeof value !== "object" || value === null) return null;
  const type = (value as { type?: unknown }).type;
  if (typeof type !== "string" || !COMMAND_TYPES.has(type)) return null;
  switch (type) {
    case "prompt":
    case "followup": {
      const text = (value as { text?: unknown }).text;
      if (typeof text !== "string") return null;
      return { type, text };
    }
    case "init": {
      const config = (value as { config?: unknown }).config;
      if (config !== undefined && (typeof config !== "object" || config === null)) {
        return null;
      }
      return { type, config: config as KernelInitConfig | undefined };
    }
    case "reject": {
      const reason = (value as { reason?: unknown }).reason;
      if (reason !== undefined && typeof reason !== "string") return null;
      return { type, reason };
    }
    case "approve":
    case "cancel":
      return { type };
    default:
      return null;
  }
}

/** Serialize an outbound frame to a single protocol line (newline included). */
export function encodeOutbound(event: KernelOutbound): string {
  return JSON.stringify(event) + "\n";
}
