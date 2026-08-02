/**
 * The live activity signal — what the agent is doing RIGHT NOW between events.
 * Split out of `buildTrace.ts`. Without true token streaming, the UI would
 * otherwise show a static "Working" label that feels frozen during slow
 * local-model turns (5-30s for Qwen 27B). This selector inspects the event
 * tail and the status to produce a precise label for what the user is waiting
 * on.
 *
 * `toolExecutingDetail` and `liveSignalForTailEvent` carry the per-event-kind
 * branching that used to be inline in the backward walk, so `deriveLiveSignal`
 * itself stays a thin status/loop dispatcher.
 */
import { stripElementMention } from "@/lib/elementMention";
import type { LiveSignal } from "../buildTrace";
import type { AgentEvent, ConversationStatus, ToolCall } from "@/types/agent";

function toolExecutingDetail(tc: ToolCall): string | undefined {
  if (tc.tool_name === "shell" || tc.tool_name === "shell_exec") {
    return String(tc.arguments.command ?? "");
  }
  if (
    tc.tool_name.startsWith("file_") ||
    tc.tool_name === "exact_replace" ||
    tc.tool_name === "safe_write_file"
  ) {
    return String(tc.arguments.path ?? "");
  }
  return undefined;
}

/** Classify one tail event into a live signal, or null to keep walking
 * backward (a non-classifying event, e.g. a plain agent action-in-flight
 * that fell through — none do today, but the null keeps this composable). */
function liveSignalForTailEvent(e: AgentEvent): LiveSignal | null {
  if (e.kind === "message" && e.source === "user") {
    // Just received a user message → the model is reading + composing a reply.
    const { clean, mention } = stripElementMention(e.message?.content ?? "");
    const text = (clean || (mention ? `Pointed at <${mention.tag}>` : "")).slice(0, 80);
    return { kind: "thinking_about_user_message", preview: text };
  }
  if (e.kind === "action" && e.tool_call) {
    // Action emitted but no observation yet → tool is executing.
    // (If a later observation/error existed, we'd have hit it first walking back.)
    const tc = e.tool_call;
    return {
      kind: "tool_executing",
      tool_name: tc.tool_name,
      detail: toolExecutingDetail(tc),
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
  return null;
}

export function deriveLiveSignal(events: AgentEvent[], status: ConversationStatus): LiveSignal {
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
    const signal = liveSignalForTailEvent(e);
    if (signal) return signal;
  }
  // No prior signal → we just kicked off; model is reading the goal.
  return { kind: "starting" };
}
