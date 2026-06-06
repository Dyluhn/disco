/**
 * The agent run header: the live status (running / waiting / finished / stopped), the
 * active isolation tier (the lower-isolation labeling, surfaced at the point of use),
 * and the always-available KILL SWITCH (BoD §13.6).
 */

import { Loader2, OctagonX, ShieldCheck, ShieldHalf } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ConversationStatus, IsolationInfo } from "@/types/agent";

const STATUS_LABEL: Record<ConversationStatus, string> = {
  IDLE: "Stopped",
  RUNNING: "Working",
  PAUSED: "Paused",
  STUCK: "Stuck",
  WAITING_FOR_CONFIRMATION: "Waiting for you",
  AWAITING_PLAN_APPROVAL: "Reviewing plan",
  FINISHED: "Finished",
  ERROR: "Error",
};

const ACTIVE: ConversationStatus[] = [
  "RUNNING",
  "WAITING_FOR_CONFIRMATION",
  "AWAITING_PLAN_APPROVAL",
  "PAUSED",
  "STUCK",
];

export function AgentStatusBar({
  status,
  isolation,
  onKill,
}: {
  status: ConversationStatus;
  isolation: IsolationInfo;
  onKill: () => void;
}) {
  const active = ACTIVE.includes(status);
  const waiting =
    status === "WAITING_FOR_CONFIRMATION" || status === "AWAITING_PLAN_APPROVAL";
  const Shield = isolation.adversarialSafe ? ShieldCheck : ShieldHalf;

  return (
    <div className="flex items-center justify-between gap-inline">
      <div className="flex items-center gap-inline">
        <span
          className={cn(
            "flex items-center gap-hair font-ui text-[0.8rem]",
            waiting ? "text-warn" : active ? "text-text" : "text-text-muted",
          )}
        >
          {status === "RUNNING" && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
          {STATUS_LABEL[status]}
        </span>
        {/* the isolation tier — honest about a shared-kernel tier not being adversarial-safe */}
        <span
          title={isolation.label}
          className={cn(
            "flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem]",
            isolation.adversarialSafe ? "text-text-muted" : "text-weak",
          )}
        >
          <Shield className="size-3" aria-hidden />
          {isolation.tier}
        </span>
      </div>

      <button
        type="button"
        onClick={onKill}
        disabled={!active}
        aria-label="Kill the agent: stop, tear down the sandbox, revoke its access"
        className={cn(
          "flex items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.78rem] font-medium transition-colors",
          active
            ? "border-unsupported text-unsupported hover:bg-unsupported hover:text-bg"
            : "cursor-not-allowed border-hairline text-text-faint",
        )}
      >
        <OctagonX className="size-3.5" aria-hidden />
        Kill
      </button>
    </div>
  );
}
