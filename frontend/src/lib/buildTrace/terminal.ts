/**
 * The terminal/output Inspector view + the two message-recovery selectors.
 * Split out of `buildTrace.ts`.
 */
import { stripElementMention } from "@/lib/elementMention";
import type { TerminalEntry } from "../buildTrace";
import type { AgentEvent } from "@/types/agent";

/** Index shell/code_exec observations + errors by action id. */
function collectTerminalObservations(
  events: AgentEvent[],
): Map<string, { output: string; success: boolean }> {
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
  return obs;
}

/** The terminal/output Inspector view: shell + code_exec commands paired with their
 * observed output. The "raw" tier lives here, not on the Activity Feed. */
export function deriveTerminal(events: AgentEvent[]): TerminalEntry[] {
  const obs = collectTerminalObservations(events);
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

/** W-01: recover the REAL task from the replayed event stream — the first user
 * message's text. Build's resume path seeds `session.task` with the internal
 * "(resumed)" sentinel (useBuild.ts), which must never reach the UI; the surface
 * uses this to render the actual task instead, mirroring how DR recovers its query
 * (useDeepResearch.ts). Matches on `source` OR `message.role` so it's robust to
 * however the backend tags the human turn. Returns null before any user message
 * exists (the caller falls back to a neutral "Resumed project" label). */
export function firstUserTask(events: AgentEvent[]): string | null {
  for (const e of events) {
    if (e.kind === "message" && (e.source === "user" || e.message?.role === "user")) {
      const { clean, mention } = stripElementMention(e.message?.content ?? "");
      if (clean) return clean;
      if (mention) return `Pointed at <${mention.tag}>`;
    }
  }
  return null;
}
