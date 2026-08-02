/**
 * Selectors that turn the raw agent event stream into the THREE-TIER views the Agent
 * surface shows (BoD §13.4): a plain-language Activity Feed (Project-Manager tier), the
 * workspace Files + Terminal (Inspector tier), and the final answer. Keeping this pure +
 * tested means the components stay thin and the "not a debug log" framing is structural.
 *
 * Module-size and complexity decomposition (PKG-12-FE-BUILD/TS-0045..TS-0050): every
 * implementation moved to `./buildTrace/*` so each module and callable stays under the
 * architecture caps — `deriveActivity` alone went from 79 branches to 13.
 *
 * The PUBLIC DECLARATIONS deliberately stay HERE rather than becoming a barrel of
 * `export … from` re-exports. `ts_scan.mjs:publicDeclarations` records a re-export as
 * `{kind: "reexport", name: "export"}`, so the public-API authority reads a barrel as
 * DELETING 21 public targets even when every name, signature and import path is
 * unchanged — and it refuses deletions outright. Frontend function signatures are
 * digested WITHOUT the body (`ts_scan.mjs:withoutBody`), so a thin delegator keeps each
 * digest byte-identical. The types are declared here and imported back by the parts
 * (parent↔parts imports are campaign-normal, standing handover §13); type imports are
 * erased at compile time, so that cycle never exists at runtime.
 */

import type {
  AgentEvent,
  ConversationStatus,
  PlanStep,
  SecurityRisk,
} from "@/types/agent";
import {
  deriveActivity as impl_deriveActivity,
} from "./buildTrace/activity";
import {
  plainError as impl_plainError,
} from "./buildTrace/activityLabels";
import {
  deriveBuildProgress as impl_deriveBuildProgress,
} from "./buildTrace/buildProgress";
import {
  deriveDeliverable as impl_deriveDeliverable,
} from "./buildTrace/deliverable";
import {
  deriveFiles as impl_deriveFiles,
} from "./buildTrace/files";
import {
  deriveLiveSignal as impl_deriveLiveSignal,
} from "./buildTrace/liveSignal";
import {
  derivePlan as impl_derivePlan,
  derivePlanProgress as impl_derivePlanProgress,
  planProgressSummary as impl_planProgressSummary,
} from "./buildTrace/plan";
import {
  deriveSrcDoc as impl_deriveSrcDoc,
} from "./buildTrace/srcdoc";
import {
  deriveTerminal as impl_deriveTerminal,
  firstUserTask as impl_firstUserTask,
  latestAgentMessage as impl_latestAgentMessage,
} from "./buildTrace/terminal";

export interface ActivityItem {
  id: string;
  /** Discriminates the row's visual style. "action" is the agent's tool call;
   * "user" is the human's message (steer, send_message, revise instruction);
   * "agent_message" is a prose reply from the agent (ask_user free-form,
   * finish-message, etc.); "system_warning" is a ⚠-prefixed ENVIRONMENT
   * message addressed to the human (BP-05's release valve: "finished WITHOUT
   * a clean browser verification") — gate truths are surfaced, other
   * environment meta (nudges, reminders) stays hidden; "system_note" is a
   * NEUTRAL environment line the human caused and should see confirmed
   * (BP-11's upload announcement) — informational, not an alarm, so it gets
   * its own kind rather than borrowing system_warning's ⚠ styling. A
   * "rollback_marker" is the one non-chat audit chip allowed for
   * workspace_restored events. The feed becomes a unified chat-and-actions log
   * rather than an action-only ledger. */
  kind:
    | "action"
    | "user"
    | "agent_message"
    | "system_warning"
    | "system_note"
    | "rollback_marker";
  label: string; // plain language ("Wrote fizzbuzz.py" / "You: skip the cleanup")
  /** The agent's natural-language THOUGHT — its reasoning + plain-English
   * explanation of what it's doing. NEVER truncated; rendered wrapped. This
   * is the model talking to the user, and swallowing it was a bug. */
  thought?: string;
  /** The technical detail — file path, command preview, etc. Single-line OK
   * to truncate (this is a row label, not content). */
  detail?: string;
  mention?: { tag: string; text: string };
  /** Rich expandable content the user can drill into when they want the raw
   * tool call + observation. Hidden by default to keep the feed scannable. */
  expandable?: {
    tool_name: string;
    arguments: Record<string, unknown>;
    output?: string; // observation content (truncated to ~2KB)
    error?: string; // error message if the action failed
    plainError?: string; // plain first line for common failure codes
    screenshot_path?: string; // BP-15: relative .pmx/screenshots/… path from structured
    // rp-11: a generated spreadsheet artifact, downloadable via the declared-artifact
    // route (only present for a successful sheet_generate).
    sheet?: { filename: string; title?: string; sheet_names?: string[] };
    // D2: a generated slide deck (Marp HTML / PDF / PPTX), downloadable via
    // the declared-artifact route. Same shape as the AnswerBlock `slides`
    // kind minus the `id` — the activity item already has one.
    slides?: {
      filename: string;
      title?: string;
      format?: "html" | "pdf" | "pptx";
      slide_count?: number;
      slides?: { title?: string; content?: string }[];
      base?: string; // deck base name (no ext) — for the /deck/export template re-render
      editable?: boolean; // has an authored sidecar → template re-render is available
      renderer?: string; // R7: backend renderer provenance (c3-brand/pptx-native/libreoffice/marp = real; fallback = degraded HTML)
    };
    // F2: an agent-emitted file (via serve(kind="files")). Downloadable via the
    // declared-artifact route. Rendered as a first-class download card in the feed.
    file?: { filename: string; title?: string };
  };
  status: "done" | "running" | "pending" | "failed" | "pending_send";
  attention: boolean; // confidence gradient: risky/novel steps float up, routine recede
  risk?: SecurityRisk;
  /** True when the engine auto-approved a sandboxed op that would have gated
   * under the base risk policy. Informational-only; never raises attention. */
  autoApproved?: boolean;
}

export function plainError(code: string): string | null {
  return impl_plainError(code);
}

export function deriveActivity(
  events: AgentEvent[],
  pendingActionId: string | null,
  status: ConversationStatus,
): ActivityItem[] {
  return impl_deriveActivity(events, pendingActionId, status);
}

export interface WorkspaceFile {
  path: string;
  content: string;
  bytes: number;
}

export interface ManifestFile {
  path: string;
  bytes: number;
}

export function deriveFiles(
  events: AgentEvent[],
  manifestFiles: ManifestFile[] = [],
): WorkspaceFile[] {
  return impl_deriveFiles(events, manifestFiles);
}

export function deriveSrcDoc(
  files: WorkspaceFile[],
  injectionScript?: string,
  baseUrl?: string,
  selectedEntryPath?: string,
): string | null {
  return impl_deriveSrcDoc(files, injectionScript, baseUrl, selectedEntryPath);
}

export interface TerminalEntry {
  id: string;
  command: string;
  output: string;
  success: boolean;
  running: boolean;
}

export function deriveTerminal(events: AgentEvent[]): TerminalEntry[] {
  return impl_deriveTerminal(events);
}

export function latestAgentMessage(events: AgentEvent[]): string | null {
  return impl_latestAgentMessage(events);
}

export function firstUserTask(events: AgentEvent[]): string | null {
  return impl_firstUserTask(events);
}

export interface DeliverableView {
  id: string;
  title: string;
  path: string;
  kind: "app" | "files";
  /** Canonical URL the deliverable is reachable at, if the agent served one. */
  deploymentUrl?: string;
}

export function deriveDeliverable(events: AgentEvent[]): DeliverableView | null {
  return impl_deriveDeliverable(events);
}

export type StepState = "pending" | "active" | "done" | "stalled";

export interface PlanView {
  id: string;
  summary: string;
  steps: PlanStep[];
  revision: number;
  context: string;
}

export function derivePlan(events: AgentEvent[]): PlanView | null {
  return impl_derivePlan(events);
}

export function derivePlanProgress(
  events: AgentEvent[],
  status?: ConversationStatus,
): Map<number, StepState> {
  return impl_derivePlanProgress(events, status);
}

export function deriveBuildProgress(
  events: AgentEvent[],
  status?: ConversationStatus,
): Map<number, StepState> {
  return impl_deriveBuildProgress(events, status);
}

export type LiveSignal =
  | { kind: "idle" }
  | { kind: "thinking_about_user_message"; preview: string }
  | { kind: "tool_executing"; tool_name: string; detail?: string }
  | { kind: "composing_next_step" }
  | { kind: "starting" }
  | { kind: "waiting_for_you"; label: string };

export function deriveLiveSignal(
  events: AgentEvent[],
  status: ConversationStatus,
): LiveSignal {
  return impl_deriveLiveSignal(events, status);
}

export function planProgressSummary(
  totalSteps: number,
  progress: Map<number, StepState>,
): { done: number; total: number; fraction: number } {
  return impl_planProgressSummary(totalSteps, progress);
}
