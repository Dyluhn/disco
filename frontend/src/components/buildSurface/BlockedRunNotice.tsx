import type { AgentEvent, ConversationStatus } from "@/types/agent";

export function BlockedRunNotice({ events, status }: { events: AgentEvent[]; status: ConversationStatus }) {
  if (status !== "PAUSED" && status !== "AWAITING_USER_DECISION") return null;
  const landing = [...events].reverse().find((event) => event.kind === "status");
  if (!landing?.meta?.blocked_landing || landing.meta.driver_error_http_status) return null;
  const message = [...events].reverse().find((event) =>
    event.kind === "message" && event.meta?.blocked_landing && event.message.role === "assistant",
  );
  const reason = message?.kind === "message" ? message.message.content : landing.meta.blocked_reason;
  if (typeof reason !== "string" || !reason.trim()) return null;
  return <div role="status" data-testid="blocked-run-notice"
    className="mb-inline rounded-card border border-warn/40 bg-warn/5 p-body text-text">
    <p className="mb-hair font-ui font-medium">Input needed to continue</p>
    <p className="whitespace-pre-wrap break-words">{reason}</p>
  </div>;
}
