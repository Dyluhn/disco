/**
 * Selectors that turn the raw agent event stream into the THREE-TIER views the Agent
 * surface shows (BoD §13.4): a plain-language Activity Feed (Project-Manager tier), the
 * workspace Files + Terminal (Inspector tier), and the final answer. Keeping this pure +
 * tested means the components stay thin and the "not a debug log" framing is structural.
 */

import type { AgentEvent, ConversationStatus, SecurityRisk } from "@/types/agent";

export interface ActivityItem {
  id: string;
  label: string; // plain language ("Wrote fizzbuzz.py")
  detail?: string; // the technical specifics live in the canvas, summarized here
  status: "done" | "running" | "pending" | "failed";
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
  const actions = events.filter((e) => e.kind === "action" && e.tool_call);
  return actions.map((e) => {
    if (e.kind !== "action" || !e.tool_call) throw new Error("unreachable");
    const tc = e.tool_call;
    const risk = e.meta?.risk_assessment?.risk ?? e.self_assessed_risk;
    const isPending = e.id === pendingActionId;
    let st: ActivityItem["status"];
    if (isPending) st = "pending";
    else if (failed.has(e.id)) st = "failed";
    else if (observed.has(e.id)) st = "done";
    else if (status === "RUNNING") st = "running";
    else st = "done";
    return {
      id: e.id,
      label: plainLabel(tc.tool_name, tc.arguments),
      detail: e.thought || detailFor(tc.tool_name, tc.arguments),
      status: st,
      // the confidence gradient: pending approvals + risky/unknown actions get attention
      attention: isPending || risk === "HIGH" || risk === "UNKNOWN" || st === "failed",
      risk,
    };
  });
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
    } else if (tool_name === "file_edit" && typeof a.path === "string" && !byPath.has(a.path)) {
      byPath.set(a.path, ""); // edited a file we didn't see created; content unknown here
    }
  }
  return [...byPath.entries()].map(([path, content]) => ({
    path,
    content,
    bytes: new TextEncoder().encode(content).length,
  }));
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
