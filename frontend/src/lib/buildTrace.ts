/**
 * Selectors that turn the raw agent event stream into the THREE-TIER views the Agent
 * surface shows (BoD §13.4): a plain-language Activity Feed (Project-Manager tier), the
 * workspace Files + Terminal (Inspector tier), and the final answer. Keeping this pure +
 * tested means the components stay thin and the "not a debug log" framing is structural.
 */

import type { AgentEvent, ConversationStatus, PlanStep, SecurityRisk } from "@/types/agent";

// Plan-mode meta tools are control signals, not workspace work — they never appear
// as Activity items or Terminal entries; their effect shows in the capstone tracker.
const META_TOOLS = new Set(["submit_plan", "plan_step"]);

export interface ActivityItem {
  id: string;
  /** Discriminates the row's visual style. "action" is the agent's tool call;
   * "user" is the human's message (steer, send_message, revise instruction);
   * "agent_message" is a prose reply from the agent (ask_user free-form,
   * finish-message, etc.); "system_warning" is a ⚠-prefixed ENVIRONMENT
   * message addressed to the human (BP-05's release valve: "finished WITHOUT
   * a clean browser verification") — gate truths are surfaced, other
   * environment meta (nudges, reminders) stays hidden. The feed becomes a
   * unified chat-and-actions log rather than an action-only ledger. */
  kind: "action" | "user" | "agent_message" | "system_warning";
  label: string; // plain language ("Wrote fizzbuzz.py" / "You: skip the cleanup")
  /** The agent's natural-language THOUGHT — its reasoning + plain-English
   * explanation of what it's doing. NEVER truncated; rendered wrapped. This
   * is the model talking to the user, and swallowing it was a bug. */
  thought?: string;
  /** The technical detail — file path, command preview, etc. Single-line OK
   * to truncate (this is a row label, not content). */
  detail?: string;
  /** Rich expandable content the user can drill into when they want the raw
   * tool call + observation. Hidden by default to keep the feed scannable. */
  expandable?: {
    tool_name: string;
    arguments: Record<string, unknown>;
    output?: string; // observation content (truncated to ~2KB)
    error?: string; // error message if the action failed
  };
  status: "done" | "running" | "pending" | "failed" | "pending_send";
  attention: boolean; // confidence gradient: risky/novel steps float up, routine recede
  risk?: SecurityRisk;
}

const VERB: Record<string, (a: Record<string, unknown>) => string> = {
  file_write: (a) => `Wrote ${a.path}`,
  file_edit: (a) => `Edited ${a.path}`,
  file_read: (a) => `Read ${a.path}`,
  file_list: (a) => `Listed ${a.path ?? "the workspace"}`,
  shell: () => `Ran a command`,
  code_exec: (a) => `Ran ${a.language ?? "python"} code`,
  search: (a) => `Searched the web for "${a.query}"`,
  extract: () => `Read a web page`,
};

function plainLabel(toolName: string, args: Record<string, unknown>): string {
  if (toolName === "browser") {
    const act = String(args.action ?? "navigate");
    if (act === "submit" || act === "fill") return `Submitted a web form`;
    return `Opened ${args.url ?? "a page"}`;
  }
  return (VERB[toolName] ?? (() => `Used ${toolName}`))(args);
}

function detailFor(toolName: string, args: Record<string, unknown>): string | undefined {
  if (toolName === "shell") return String(args.command ?? "");
  if (toolName === "browser") return String(args.url ?? "");
  if (toolName.startsWith("file_")) return String(args.path ?? "");
  return undefined;
}

export function deriveActivity(
  events: AgentEvent[],
  pendingActionId: string | null,
  status: ConversationStatus,
): ActivityItem[] {
  const observed = new Set<string>(); // action ids that produced an observation
  const failed = new Set<string>(); // action ids that errored
  for (const e of events) {
    if (e.kind === "observation") observed.add(e.action_id);
    if (e.kind === "agent_error" && e.action_id) failed.add(e.action_id);
  }
  // Walk events in order — the activity feed is a chronological chat-and-action
  // log, not an action-only ledger. User messages (steer, send_message, revise)
  // and mid-stream agent prose replies (ask_user free-form questions) are
  // first-class items alongside tool calls. This is what makes typed input feel
  // acknowledged: it appears in the timeline the moment the optimistic event is
  // dispatched, then the server's canonical echo replaces the placeholder.
  //
  // The TRAILING agent message is the surface's "final answer" — rendered as
  // a Markdown panel elsewhere in the BuildSurface. Excluding it from the feed
  // prevents the same text rendering twice + keeps the feed the running narrative.
  let lastAgentMessageIdx = -1;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "message" && e.source === "agent") {
      lastAgentMessageIdx = i;
      break;
    }
  }
  // Index observations + errors by action id so we can attach the raw output
  // to each action's expandable detail (the user explicitly asked to be able
  // to drill into commands + results — hiding them is poor design).
  const observationByActionId = new Map<string, { output?: string; error?: string }>();
  for (const e of events) {
    if (e.kind === "observation") {
      observationByActionId.set(e.action_id, {
        output: (e.tool_result.content || "").slice(0, 2000),
      });
    } else if (e.kind === "agent_error" && e.action_id) {
      observationByActionId.set(e.action_id, { error: e.error });
    }
  }
  const out: ActivityItem[] = [];
  for (let idx = 0; idx < events.length; idx++) {
    const e = events[idx];
    if (e.kind === "action" && e.tool_call && !META_TOOLS.has(e.tool_call.tool_name)) {
      const tc = e.tool_call;
      const risk = e.meta?.risk_assessment?.risk ?? e.self_assessed_risk;
      const isPending = e.id === pendingActionId;
      let st: ActivityItem["status"];
      if (isPending) st = "pending";
      else if (failed.has(e.id)) st = "failed";
      else if (observed.has(e.id)) st = "done";
      else if (status === "RUNNING") st = "running";
      else st = "done";
      const obs = observationByActionId.get(e.id);
      out.push({
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
        },
        status: st,
        attention: isPending || risk === "HIGH" || risk === "UNKNOWN" || st === "failed",
        risk,
      });
    } else if (e.kind === "message" && e.source === "user") {
      // User input (steer/send_message/revise) — render verbatim. The optimistic
      // echo from useBuildStream stamps id="local-pending-…" so we can show a
      // subtle "sending" affordance until the server's canonical echo replaces it.
      const content = e.message?.content ?? "";
      if (!content.trim()) continue;
      const isPendingSend = e.id.startsWith("local-pending-");
      out.push({
        id: e.id,
        kind: "user",
        label: content,
        status: isPendingSend ? "pending_send" : "done",
        attention: false,
      });
    } else if (e.kind === "message" && e.source === "agent") {
      // Mid-stream agent prose (e.g. ask_user free-form questions). Skip the
      // trailing agent message — it's rendered as the final-answer Markdown
      // panel; double-rendering would break getByText assertions + read noisy.
      if (idx === lastAgentMessageIdx) continue;
      const content = e.message?.content ?? "";
      if (!content.trim()) continue;
      out.push({
        id: e.id,
        kind: "agent_message",
        label: content,
        status: "done",
        attention: false,
      });
    } else if (e.kind === "message" && e.source === "environment") {
      // Environment messages are loop meta (nudges, reminders) — hidden, EXCEPT
      // ⚠-prefixed warnings, which the loop explicitly addresses to the human
      // (BP-05 release valve: the run finished WITHOUT a clean browser
      // verification). Truths about delivered work must reach the feed.
      const content = e.message?.content ?? "";
      if (!content.startsWith("⚠")) continue;
      out.push({
        id: e.id,
        kind: "system_warning",
        label: content,
        status: "done",
        attention: true,
      });
    }
  }
  return out;
}

export interface WorkspaceFile {
  path: string;
  content: string;
  bytes: number;
}

/** The files the agent has written, latest content wins — real, straight from the
 * file_write/file_edit events the stream already carries (no extra backend needed). */
export function deriveFiles(events: AgentEvent[]): WorkspaceFile[] {
  const byPath = new Map<string, string>();
  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    const { tool_name, arguments: a } = e.tool_call;
    if (tool_name === "file_write" && typeof a.path === "string") {
      byPath.set(a.path, String(a.content ?? ""));
    } else if (tool_name === "file_append" && typeof a.path === "string") {
      // Accumulate appended content so an incrementally-written file still renders.
      byPath.set(a.path, (byPath.get(a.path) ?? "") + String(a.content ?? ""));
    } else if (tool_name === "file_edit" && typeof a.path === "string") {
      const cur = byPath.get(a.path);
      if (cur === undefined) {
        byPath.set(a.path, ""); // edited a file we didn't see created; content unknown here
      } else if (typeof a.old === "string" && typeof a.new === "string") {
        byPath.set(a.path, cur.replace(a.old, a.new)); // mirror the edit so the preview tracks it
      }
    }
  }
  return [...byPath.entries()].map(([path, content]) => ({
    path,
    content,
    bytes: new TextEncoder().encode(content).length,
  }));
}

/** Cluster 5 (UI 2.1): assemble a self-contained HTML document from the written
 * files for a CLIENT-SIDE `srcdoc` preview — no backend, no dev server. Picks
 * the entry HTML (index.html, else any *.html), inlines local <link
 * rel=stylesheet> and <script src> references from sibling files so the iframe
 * renders the real thing as it's built. Returns null when there's no renderable
 * HTML artifact (so the pane falls back to the live-server preview / placeholder).
 */
export function deriveSrcDoc(files: WorkspaceFile[]): string | null {
  if (files.length === 0) return null;
  const byName = new Map<string, string>();
  for (const f of files) {
    // index by basename and by path so both `href="style.css"` and
    // `href="./css/style.css"` resolve.
    byName.set(f.path, f.content);
    byName.set(f.path.split("/").pop() ?? f.path, f.content);
  }
  const entry =
    files.find((f) => /(^|\/)index\.html$/i.test(f.path)) ??
    files.find((f) => /\.html$/i.test(f.path));
  if (!entry) return null;
  let html = entry.content;
  // Inline <link rel="stylesheet" href="local.css">
  html = html.replace(
    /<link[^>]*rel=["']?stylesheet["']?[^>]*href=["']([^"']+)["'][^>]*>/gi,
    (m, href) => {
      const css = byName.get(href) ?? byName.get(href.replace(/^\.?\//, ""));
      return css != null ? `<style>\n${css}\n</style>` : m;
    },
  );
  // Inline <script src="local.js">
  html = html.replace(
    /<script[^>]*src=["']([^"']+)["'][^>]*><\/script>/gi,
    (m, src) => {
      const js = byName.get(src) ?? byName.get(src.replace(/^\.?\//, ""));
      return js != null ? `<script>\n${js}\n</script>` : m;
    },
  );
  return html;
}

export interface TerminalEntry {
  id: string;
  command: string;
  output: string;
  success: boolean;
  running: boolean;
}

/** The terminal/output Inspector view: shell + code_exec commands paired with their
 * observed output. The "raw" tier lives here, not on the Activity Feed. */
export function deriveTerminal(events: AgentEvent[]): TerminalEntry[] {
  const obs = new Map<string, { output: string; success: boolean }>();
  for (const e of events) {
    if (e.kind === "observation") {
      obs.set(e.action_id, {
        output: e.tool_result.content || e.tool_result.error || "",
        success: e.tool_result.success,
      });
    } else if (e.kind === "agent_error" && e.action_id) {
      obs.set(e.action_id, { output: e.error, success: false });
    }
  }
  const out: TerminalEntry[] = [];
  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    const { tool_name, arguments: a } = e.tool_call;
    if (tool_name !== "shell" && tool_name !== "code_exec") continue;
    const cmd = tool_name === "shell" ? String(a.command ?? "") : `${a.language ?? "python"} «code»`;
    const r = obs.get(e.id);
    out.push({
      id: e.id,
      command: cmd,
      output: r?.output ?? "",
      success: r?.success ?? true,
      running: !r,
    });
  }
  return out;
}

export function latestAgentMessage(events: AgentEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "message" && e.source === "agent") return e.message.content;
  }
  return null;
}

// ---- deliverable handoff --------------------------------------------------

export interface DeliverableView {
  id: string;
  title: string;
  path: string;
  kind: "app" | "files";
  /** Canonical URL the deliverable is reachable at, if the agent served one. */
  deploymentUrl?: string;
}

/** The latest finished-artifact handoff the agent declared via `serve` (a
 * DeliverableEvent). The newest wins — a later serve supersedes an earlier one
 * (the agent refined or replaced the deliverable). Returns null before any
 * handoff, so the panel stays hidden until there is a real thing to hand off
 * (no false affordance). */
export function deriveDeliverable(events: AgentEvent[]): DeliverableView | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "deliverable") {
      return {
        id: e.id,
        title: e.title,
        path: e.path,
        kind: e.artifact_kind,
        deploymentUrl: e.deployment_url || undefined,
      };
    }
  }
  return null;
}

// ---- plan mode ------------------------------------------------------------

export type StepState = "pending" | "active" | "done" | "stalled";

export interface PlanView {
  id: string;
  summary: string;
  steps: PlanStep[];
  revision: number;
  context: string;
}

/** The latest proposed plan (highest revision wins — a re-plan supersedes the prior
 * one). Returns null before any plan exists. */
export function derivePlan(events: AgentEvent[]): PlanView | null {
  let latest: PlanView | null = null;
  for (const e of events) {
    if (e.kind !== "plan") continue;
    if (latest === null || e.revision >= latest.revision) {
      latest = {
        id: e.id,
        summary: e.summary,
        steps: e.steps,
        revision: e.revision,
        context: e.context ?? "",
      };
    }
  }
  return latest;
}

/** Per-step progress (1-based index → state), derived from the agent's `plan_step`
 * reports in the stream. Honest by construction: a step the agent never reported
 * stays "pending" — progress is agent-driven, never inferred from action counts.
 *
 * Status-aware: when the conversation reaches a terminal-without-completion state
 * (FINISHED/STUCK/ERROR — anything that means "the loop stopped before this step
 * could be marked done"), any lingering "active" step is rewritten to "stalled"
 * so the UI doesn't lie with a spinning blue icon on work that isn't progressing. */
export function derivePlanProgress(
  events: AgentEvent[],
  status?: ConversationStatus,
): Map<number, StepState> {
  const progress = new Map<number, StepState>();
  // Only count plan_step marks AFTER the latest plan — a re-plan starts a fresh
  // checklist, so the prior plan's "done" marks must not show on the new one
  // (else every step looks done after a re-plan).
  let latestPlanIdx = -1;
  let latestRev = -1;
  events.forEach((e, i) => {
    if (e.kind === "plan" && e.revision >= latestRev) {
      latestRev = e.revision;
      latestPlanIdx = i;
    }
  });
  events.forEach((e, i) => {
    if (i < latestPlanIdx) return; // belongs to a superseded plan
    if (e.kind !== "action" || !e.tool_call || e.tool_call.tool_name !== "plan_step") return;
    const idx = Number(e.tool_call.arguments.index);
    const state = String(e.tool_call.arguments.state);
    if (!Number.isFinite(idx)) return;
    if (state === "done") progress.set(idx, "done");
    else if (state === "active" && progress.get(idx) !== "done") progress.set(idx, "active");
  });
  // Stalled-step reconciliation: in terminal states, an "active" marker means
  // the agent started a step and the loop stopped before it finished. Show that
  // honestly instead of pretending it's still working.
  const terminalStopped =
    status === "FINISHED" || status === "STUCK" || status === "ERROR" || status === "IDLE";
  if (terminalStopped) {
    for (const [idx, st] of progress) {
      if (st === "active") progress.set(idx, "stalled");
    }
  }
  return progress;
}

/** The live activity signal — what the agent is doing RIGHT NOW between events.
 * Without true token streaming, the UI would otherwise show a static "Working"
 * label that feels frozen during slow local-model turns (5-30s for Qwen 27B).
 * This selector inspects the event tail and the status to produce a precise
 * label for what the user is waiting on. */
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
  // Cluster 6: liveness is no longer RUNNING-only. During gate/await states the
  // bar narrates what the agent is waiting ON, so the surface never reads as
  // frozen between turns or while a gate is open.
  if (status === "WAITING_FOR_CONFIRMATION")
    return { kind: "waiting_for_you", label: "Waiting for you to approve an action" };
  if (status === "AWAITING_PLAN_APPROVAL")
    return { kind: "waiting_for_you", label: "Waiting for you to review the plan" };
  if (status === "AWAITING_USER_DECISION")
    return { kind: "waiting_for_you", label: "Waiting for your decision" };
  if (status === "AWAITING_USER_QUESTION")
    return { kind: "waiting_for_you", label: "The agent asked you a question" };
  if (status !== "RUNNING") return { kind: "idle" };
  // Walk the tail backward to classify what we're waiting on. Skip noise
  // events (environment reminders) — they're meta, not the live signal.
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "status") continue;
    if (e.kind === "message" && e.source === "environment") continue;
    if (e.kind === "message" && e.source === "user") {
      // Just received a user message → the model is reading + composing a reply.
      const text = (e.message?.content ?? "").slice(0, 80);
      return { kind: "thinking_about_user_message", preview: text };
    }
    if (e.kind === "action" && e.tool_call) {
      // Action emitted but no observation yet → tool is executing.
      // (If a later observation/error existed, we'd have hit it first walking back.)
      const tc = e.tool_call;
      return {
        kind: "tool_executing",
        tool_name: tc.tool_name,
        detail:
          tc.tool_name === "shell"
            ? String(tc.arguments.command ?? "")
            : tc.tool_name.startsWith("file_")
              ? String(tc.arguments.path ?? "")
              : undefined,
      };
    }
    if (e.kind === "observation" || e.kind === "agent_error") {
      // Last event was a tool result; model is composing its next step.
      return { kind: "composing_next_step" };
    }
    if (e.kind === "message" && e.source === "agent") {
      // Agent just spoke; another step is in progress.
      return { kind: "composing_next_step" };
    }
  }
  // No prior signal → we just kicked off; model is reading the goal.
  return { kind: "starting" };
}

/** Cluster 6: aggregate plan progress for a glanceable "N of M" + bar. Built
 * from the same progress Map the PlanPanel already has. */
export function planProgressSummary(
  totalSteps: number,
  progress: Map<number, StepState>,
): { done: number; total: number; fraction: number } {
  let done = 0;
  for (const st of progress.values()) if (st === "done") done += 1;
  const total = Math.max(totalSteps, 0);
  return { done, total, fraction: total > 0 ? done / total : 0 };
}
