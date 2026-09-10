import { deriveLiveSignal } from "@/lib/buildTrace";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

export type AgentStage =
  | "planning"
  | "reading"
  | "executing"
  | "building"
  | "stalled"
  | "done"
  | "idle";

/** The `tool_executing` signal carries a tool name, not an AgentStage — this
 * classifies it. Split out of `deriveStage`'s switch so the per-tool-family
 * branching (plan tools / file+shell tools / read-ish tools) lives in its own
 * capped callable instead of inflating the outer switch's complexity. */
function stageForToolExecuting(tool: string): AgentStage {
  if (tool === "update_plan_progress" || tool === "plan" || tool.startsWith("plan_")) {
    return "planning";
  }
  if (tool.startsWith("file_") || tool === "shell") return "building";
  if (
    tool === "browser" ||
    tool === "search" ||
    tool === "fetch" ||
    tool === "read" ||
    tool.startsWith("mcp__")
  ) {
    return "reading";
  }
  return "executing";
}

export function deriveStage(events: AgentEvent[], status: ConversationStatus): AgentStage {
  if (status === "FINISHED") return "done";
  if (status === "STUCK" || status === "ERROR") return "stalled";
  const signal = deriveLiveSignal(events, status);
  switch (signal.kind) {
    case "idle":
      return "idle";
    case "starting":
    case "waiting_for_you":
      return "planning";
    case "thinking_about_user_message":
      return "reading";
    case "composing_next_step":
      return "executing";
    case "tool_executing":
      return stageForToolExecuting(signal.tool_name);
    default:
      return "idle";
  }
}
