/**
 * Per-event-kind Activity Feed row builders — split out of `buildTrace.ts`'s
 * `deriveActivity`. Its main loop used to be one long if/else-if ladder over
 * `e.kind`; each branch is now its own small, independently-capped callable,
 * and the loop just dispatches to the one matching the discriminant (the same
 * "extend the dispatch idiom" move as the VERB/PLAIN_ERROR tables).
 */
import { stripElementMention } from "@/lib/elementMention";
import type {
  ActionEvent,
  AppKitEjectionEvent,
  ConversationStatus,
  DeliverableEvent,
  MessageEvent,
  WorkspaceRestoredEvent,
} from "@/types/agent";
import { _isWorkspaceRoot, detailFor, plainLabel } from "./activityLabels";
import type { ObservationEntry } from "./activityObservations";
import type { ActivityItem } from "../buildTrace";

// Plan-mode meta tools are control signals, not workspace work — they never appear
// as Activity items or Terminal entries; their effect shows in the capstone tracker.
const META_TOOLS = new Set(["submit_plan", "plan_step", "update_plan_progress"]);

export interface ActivityBuildContext {
  pendingActionId: string | null;
  status: ConversationStatus;
  observed: Set<string>;
  failed: Set<string>;
  observationByActionId: Map<string, ObservationEntry>;
}

export function activityItemForAction(
  e: ActionEvent,
  ctx: ActivityBuildContext,
): ActivityItem | null {
  if (!e.tool_call || META_TOOLS.has(e.tool_call.tool_name)) return null;
  const tc = e.tool_call;
  // W-14/W-28: a workspace-root `file_list` is the agent's silent orientation
  // step — suppress it so the feed shows nothing until there's real content
  // (no "Listed ." first-output stub). A list of a real subdirectory still
  // renders ("Listed src/").
  if (tc.tool_name === "file_list" && _isWorkspaceRoot(tc.arguments.path)) return null;
  const risk = e.meta?.risk_assessment?.risk ?? e.self_assessed_risk;
  const isPending = e.id === ctx.pendingActionId;
  let st: ActivityItem["status"];
  if (isPending) st = "pending";
  else if (ctx.failed.has(e.id)) st = "failed";
  else if (ctx.observed.has(e.id)) st = "done";
  else if (ctx.status === "RUNNING") st = "running";
  else st = "done";
  const obs = ctx.observationByActionId.get(e.id);
  if (e.meta?.verify_probe === true) {
    return {
      id: e.id,
      kind: "system_note",
      label: "Host ran an advisory finish check",
      detail: detailFor(tc.tool_name, tc.arguments),
      expandable: {
        tool_name: tc.tool_name,
        arguments: tc.arguments,
        output: obs?.output,
        error: obs?.error,
        plainError: obs?.plainError,
      },
      status: st,
      attention: false,
    };
  }
  return {
    id: e.id,
    kind: "action",
    label: plainLabel(tc.tool_name, tc.arguments),
    thought: e.thought || undefined, // the model talking — NEVER truncate
    detail: detailFor(tc.tool_name, tc.arguments),
    expandable: {
      tool_name: tc.tool_name,
      arguments: tc.arguments,
      output: obs?.output,
      error: obs?.error,
      plainError: obs?.plainError,
      screenshot_path: obs?.screenshotPath,
      sheet: obs?.sheet,
      slides: obs?.slides,
    },
    status: st,
    attention: isPending || risk === "HIGH" || risk === "UNKNOWN" || st === "failed",
    risk,
    autoApproved: e.meta?.auto_approved === "sandboxed",
  };
}

export function activityItemForUserMessage(e: MessageEvent): ActivityItem | null {
  // User input (steer/send_message/revise) — render the human text, not any
  // machine-readable Point-flow element payload. The optimistic echo from
  // useBuildStream stamps id="local-pending-…" so we can show a subtle
  // "sending" affordance until the server's canonical echo replaces it.
  const content = e.message?.content ?? "";
  const { clean, mention } = stripElementMention(content);
  if (!clean && !mention) return null;
  const isPendingSend = e.id.startsWith("local-pending-");
  return {
    id: e.id,
    kind: "user",
    label: clean || `Pointed at <${mention?.tag ?? "element"}>`,
    mention: mention ? { tag: mention.tag, text: mention.text } : undefined,
    status: isPendingSend ? "pending_send" : "done",
    attention: false,
  };
}

export function activityItemForAgentMessage(
  e: MessageEvent,
  idx: number,
  lastAgentMessageIdx: number,
): ActivityItem | null {
  // Mid-stream agent prose (e.g. ask_user free-form questions). Skip the
  // trailing agent message — it's rendered as the final-answer Markdown
  // panel; double-rendering would break getByText assertions + read noisy.
  if (idx === lastAgentMessageIdx) return null;
  const content = e.message?.content ?? "";
  if (!content.trim()) return null;
  return {
    id: e.id,
    kind: "agent_message",
    label: content,
    status: "done",
    attention: false,
  };
}

export function activityItemForEnvironmentMessage(e: MessageEvent): ActivityItem | null {
  // Environment messages are loop meta (nudges, reminders) — hidden, EXCEPT
  // ⚠-prefixed warnings, which the loop explicitly addresses to the human
  // (BP-05 release valve: the run finished WITHOUT a clean browser
  // verification), and BP-11 upload announcements, which confirm an action
  // the HUMAN took. Truths about delivered work must reach the feed.
  const content = e.message?.content ?? "";
  if (content.startsWith("User uploaded:")) {
    // Neutral note, not a warning — the user did this on purpose, and
    // ⚠-styling a routine confirmation would be a false alarm.
    return { id: e.id, kind: "system_note", label: content, status: "done", attention: false };
  }
  if (!content.startsWith("⚠")) return null;
  return { id: e.id, kind: "system_warning", label: content, status: "done", attention: true };
}

export function activityItemForDeliverable(e: DeliverableEvent): ActivityItem | null {
  // F2: Agent handed off a file via serve(kind="files"). Render as a
  // download card in the feed so the user can grab it immediately.
  // Only render for "files" kind (not "app" which opens a live URL).
  if (e.artifact_kind !== "files") return null;
  return {
    id: e.id,
    kind: "action",
    label: `Delivered: ${e.title}`,
    detail: e.path,
    expandable: {
      tool_name: "serve",
      arguments: {},
      file: { filename: e.path, title: e.title },
    },
    status: "done",
    attention: false,
  };
}

export function activityItemForAppkitEjection(e: AppKitEjectionEvent): ActivityItem {
  return {
    id: e.id,
    kind: "system_warning",
    label: `AppKit ejected to Freeform at v${e.ejected_version_seq}; governed v${e.source_version_seq} remains available`,
    detail: `Not AppKit-verified. Lost guarantees: ${e.lost_guarantees.join("; ")}.`,
    status: "done",
    attention: true,
  };
}

export function activityItemForWorkspaceRestored(e: WorkspaceRestoredEvent): ActivityItem {
  return {
    id: e.id,
    kind: "rollback_marker",
    label: `↩ rolled back to v${e.version_seq}`,
    status: "done",
    attention: false,
  };
}
