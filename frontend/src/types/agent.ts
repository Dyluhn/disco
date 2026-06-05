/**
 * Agent (Build) surface types — the TS mirror of the core event/state contract
 * (event-state-contract §2/§3) as it crosses the conversation WebSocket. The Build
 * surface consumes the AGENT LOOP's event log (/ws/conversations/{id}), distinct from
 * Research's ephemeral token stream: here the EVENTS are the source of truth.
 */

export type ConversationStatus =
  | "IDLE"
  | "RUNNING"
  | "PAUSED"
  | "STUCK"
  | "WAITING_FOR_CONFIRMATION"
  | "FINISHED"
  | "ERROR";

export type SecurityRisk = "UNKNOWN" | "LOW" | "MEDIUM" | "HIGH";
export type EventSource = "user" | "agent" | "environment" | "system";

/** The analyzer's verdict, carried on a proposed action's `meta.risk_assessment`. */
export interface RiskAssessment {
  risk: SecurityRisk;
  rationale: string;
  analyzer: string;
  self_assessed?: SecurityRisk;
}

export interface ToolCall {
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResult {
  tool_name: string;
  success: boolean;
  content: string;
  structured?: Record<string, unknown> | null;
  error?: string | null;
}

interface EventBase {
  id: string;
  seq?: number | null;
  source?: EventSource;
}

export interface MessageEvent extends EventBase {
  kind: "message";
  message: { role: string; content: string };
}
export interface ActionEvent extends EventBase {
  kind: "action";
  thought: string;
  tool_call: ToolCall | null;
  self_assessed_risk?: SecurityRisk;
  meta?: { risk_assessment?: RiskAssessment } & Record<string, unknown>;
}
export interface ObservationEvent extends EventBase {
  kind: "observation";
  tool_result: ToolResult;
  action_id: string;
}
export interface AgentErrorEvent extends EventBase {
  kind: "agent_error";
  error: string;
  action_id?: string | null;
}
export interface CondensationEvent extends EventBase {
  kind: "condensation";
  summary: string;
  forgotten_start_seq: number;
  forgotten_end_seq: number;
}
export interface StatusEvent extends EventBase {
  kind: "status";
  status: ConversationStatus;
  detail?: string | null;
}
export interface ErrorEvent extends EventBase {
  kind: "error";
  code?: string;
  detail?: string | null;
}

export type AgentEvent =
  | MessageEvent
  | ActionEvent
  | ObservationEvent
  | AgentErrorEvent
  | CondensationEvent
  | StatusEvent
  | ErrorEvent;

export interface ConversationState {
  conversation_id: string;
  execution_status: ConversationStatus;
  iteration: number;
  max_iterations: number;
  last_seq: number;
  pending_action_id: string | null;
}

// ---- WS frames (event-state §7) ---------------------------------------------

export type WSServerFrame =
  | { type: "state"; state: ConversationState }
  | { type: "event"; event: AgentEvent }
  | { type: "error"; error: { detail?: string } }
  | { type: "pong" };

export type WSClientFrame =
  | { type: "send_message"; content: string }
  | { type: "confirm"; action_id?: string }
  | { type: "reject"; action_id?: string }
  | { type: "cancel" }
  | { type: "ping" };

/** Which isolation tier backs the sandbox — surfaced so the lower-isolation tier is
 * legible at the point of use (the cost-legible picker, applied to isolation). */
export interface IsolationInfo {
  tier: string; // "gvisor" | "container-remote" | "container" | …
  label: string;
  adversarialSafe: boolean;
}
