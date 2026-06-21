/**
 * The agent run header: the live status (running / waiting / finished / stopped), the
 * active isolation tier (the lower-isolation labeling, surfaced at the point of use),
 * and the always-available KILL SWITCH (BoD §13.6).
 *
 * Two control affordances with deliberately different weights:
 *  - Stop — a cooperative, non-destructive halt. Shows a "Stopping…" pending state
 *    from click until the loop actually leaves RUNNING, so the click isn't a no-op
 *    void (the loop finishes its in-flight step before it can honor the cancel).
 *  - Kill — destructive (tears down the sandbox + revokes the instance's access).
 *    Gated behind an inline confirm so a mis-click can't nuke a long build.
 */

import { useEffect, useMemo, useState } from "react";
import { Loader2, OctagonX, Play, ShieldCheck, ShieldHalf, Square } from "lucide-react";
import { cn } from "@/lib/cn";
import type { AgentEvent, ConversationStatus, IsolationInfo } from "@/types/agent";
import { useModels } from "@/hooks/useModels";
import { calculateUsageCost, formatCost } from "@/lib/cost";
import { isFree, type TokenUsage } from "@/types/models";

const STATUS_LABEL: Record<ConversationStatus, string> = {
  IDLE: "Stopped",
  RUNNING: "Working",
  PAUSED: "Paused",
  STUCK: "Stuck",
  WAITING_FOR_CONFIRMATION: "Waiting for you",
  AWAITING_PLAN_APPROVAL: "Reviewing plan",
  AWAITING_USER_DECISION: "Choose a path",
  AWAITING_USER_QUESTION: "Question for you",
  FINISHED: "Finished",
  ERROR: "Error",
};

// The "kill is meaningful here" set. STUCK + terminal states (FINISHED, ERROR)
// stay in because the sandbox may still be alive and the user may want to clean
// up + clear the broken state, even after the loop exited. Only IDLE is excluded
// — no conversation to kill.
const ACTIVE: ConversationStatus[] = [
  "RUNNING",
  "WAITING_FOR_CONFIRMATION",
  "AWAITING_PLAN_APPROVAL",
  "AWAITING_USER_DECISION",
  "AWAITING_USER_QUESTION",
  "PAUSED",
  "STUCK",
  "FINISHED",
  "ERROR",
];

export function AgentStatusBar({
  status,
  isolation,
  sandboxState,
  autonomous,
  assist,
  onKill,
  onStop,
  onResume,
  events = [],
  modelId,
}: {
  status: ConversationStatus;
  /** BP-15: null until the first state frame — renders muted '…'. */
  isolation: IsolationInfo | null;
  /** Runtime sandbox liveness overlay ('active' | 'suspended' | undefined). */
  sandboxState?: "active" | "suspended";
  /** Headless run: no ask_user, auto-approved plan, clean forfeit instead of halting. */
  autonomous?: boolean;
  /** Server-derived execution tier (Order D). true = weak/assist; false/absent = standard. */
  assist?: boolean;
  onKill: () => void;
  /** Cluster 6: graceful stop (cooperative cancel — no sandbox teardown). */
  onStop?: () => void;
  /** Resume a PAUSED build (stopped, or interrupted by a server restart). */
  onResume?: () => void;
  /** RP-14: cumulative cost from usage in events. */
  events?: AgentEvent[];
  /** RP-14: the currently selected model id for rate lookup. */
  modelId?: string | null;
}) {
  const active = ACTIVE.includes(status);

  const waiting =
    status === "WAITING_FOR_CONFIRMATION" || status === "AWAITING_PLAN_APPROVAL";
  // Graceful Stop is meaningful while the agent is actually running or waiting
  // on a gate — a non-destructive halt, distinct from the red teardown Kill.
  const canStop =
    status === "RUNNING" ||
    status === "WAITING_FOR_CONFIRMATION" ||
    status === "AWAITING_USER_DECISION" ||
    status === "AWAITING_USER_QUESTION" ||
    status === "PAUSED";
  const Shield = isolation?.adversarialSafe ? ShieldCheck : ShieldHalf;

  // "Stopping…" pending: Stop is cooperative — the loop honors the cancel only at
  // its next checkpoint, so the click would otherwise feel dead. Show pending from
  // click until the status actually leaves the running/active phase.
  const [stopping, setStopping] = useState(false);
  useEffect(() => {
    // Once the loop has settled (paused/idle/finished/stuck/error), clear pending.
    if (status !== "RUNNING") setStopping(false);
  }, [status]);

  // Kill confirmation: destructive, so require a second click.
  const [confirmingKill, setConfirmingKill] = useState(false);
  useEffect(() => {
    if (!active) setConfirmingKill(false); // nothing to kill → drop the prompt
  }, [active]);

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
        {isolation === null ? (
          <span
            aria-label="isolation tier loading"
            className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-faint"
          >
            …
          </span>
        ) : (
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
        )}
        {autonomous && (
          <span
            title="Autonomous run — the agent works headless: it won't ask you questions, auto-approves its own plan, and stops cleanly instead of waiting for you"
            className="flex items-center gap-hair rounded-full border border-accent/40 px-inline py-px font-ui text-[0.7rem] text-accent"
          >
            autonomous
          </span>
        )}
        {/* Execution-tier badge: "Assist" (weak/small-model tier) vs "Standard" (capable).
            Server-derived — no user toggle this pass; read from state.extras.assist. */}
        {assist !== undefined && (
          <span
            title={
              assist
                ? "Assist tier — running with small-model compensations (weak driver)"
                : "Standard tier — running with a capable model"
            }
            className={cn(
              "flex items-center gap-hair rounded-full border px-inline py-px font-ui text-[0.7rem]",
              assist
                ? "border-warn/40 text-warn"
                : "border-hairline text-text-faint",
            )}
          >
            {assist ? "Assist" : "Standard"}
          </span>
        )}
        {sandboxState === "suspended" && (
          <span
            title="Sandbox suspended — workspace is saved; it will resume on your next message"
            className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-muted"
          >
            suspended
          </span>
        )}
        {/* RP-14: cost meter mounts only when a model is wired in — callers that
            don't pass modelId (and the pre-RP-14 test suites) never touch the
            models query, so no QueryClientProvider is required of them. */}
        {modelId != null && <CostMeter events={events} modelId={modelId} />}
      </div>

      <div className="flex items-center gap-hair">
        {onResume && (status === "PAUSED" || status === "IDLE") && (
          <button
            type="button"
            onClick={onResume}
            aria-label="Resume the agent — continue this build where it left off"
            className="flex items-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:border-accent hover:bg-accent/5"
          >
            <Play className="size-3.5" aria-hidden />
            Resume
          </button>
        )}
        {onStop && canStop && status !== "PAUSED" && !confirmingKill && (
          <button
            type="button"
            onClick={() => {
              setStopping(true);
              onStop();
            }}
            disabled={stopping}
            aria-label="Stop the agent gracefully (does not tear down the sandbox)"
            className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-text-muted hover:text-text disabled:opacity-60"
          >
            {stopping ? (
              <Loader2 className="size-3.5 animate-spin" aria-hidden />
            ) : (
              <Square className="size-3.5" aria-hidden />
            )}
            {stopping ? "Stopping…" : "Stop"}
          </button>
        )}
        {/* Kill: destructive → inline confirm. First click arms; second confirms. */}
        {confirmingKill ? (
          <div className="flex items-center gap-hair">
            <span className="whitespace-nowrap font-ui text-[0.74rem] text-unsupported">
              Kill this run?
            </span>
            <button
              type="button"
              onClick={() => {
                setConfirmingKill(false);
                onKill();
              }}
              aria-label="Confirm kill: stop, tear down the sandbox, revoke its access"
              className="flex items-center gap-hair rounded-control border border-unsupported bg-unsupported px-inline py-hair font-ui text-[0.78rem] font-medium text-bg transition-opacity hover:opacity-90"
            >
              <OctagonX className="size-3.5" aria-hidden />
              Confirm
            </button>
            <button
              type="button"
              onClick={() => setConfirmingKill(false)}
              aria-label="Cancel — keep the run"
              className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmingKill(true)}
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
        )}
      </div>
    </div>
  );
}

/** RP-14: cumulative session cost. Isolated in its own component so the
 * models query only runs when a caller actually wires in a model — and so
 * the meter can be hidden on paid models until the event stream carries real
 * usage data (backend field pending, wave 2): a meter pinned at "≥$0.00"
 * regardless of real spend would be a false affordance, not a lower bound. */
function CostMeter({ events, modelId }: { events: AgentEvent[]; modelId: string }) {
  const { data: models } = useModels();

  const { cost, hasMissingUsage, allFree, sawUsage } = useMemo(() => {
    let total = 0;
    let missing = false;
    let anyPaid = false;
    let usageSeen = false;

    // The current session model's rate decides whether we're in "paid" mode.
    const currentModel = models?.find((m) => m.id === modelId);
    if (currentModel && !isFree(currentModel)) anyPaid = true;

    for (const e of events) {
      if (e.kind !== "action") continue;
      // Usage may be in a top-level field or tucked in meta.
      const usage = (e as { usage?: TokenUsage }).usage || (e.meta?.usage as TokenUsage | undefined);
      if (usage) usageSeen = true;
      // Attribute the cost using the model that was active for THAT action.
      const mId = (e.meta?.model_id as string) || modelId;
      const m = models?.find((m) => m.id === mId);

      if (m && !isFree(m)) {
        anyPaid = true;
        if (usage) {
          total += calculateUsageCost(m, usage);
        } else {
          missing = true;
        }
      }
    }
    return { cost: total, hasMissingUsage: missing, allFree: !anyPaid, sawUsage: usageSeen };
  }, [events, models, modelId]);

  if (!allFree && !sawUsage) return null;

  return (
    <span
      title={
        allFree
          ? "This model is free"
          : hasMissingUsage
            ? "Usage data is missing for some turns; this is a lower bound."
            : "Cumulative session cost"
      }
      className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-muted"
    >
      {allFree ? (
        "Free"
      ) : (
        <>
          {hasMissingUsage && <span aria-hidden>&ge;</span>}
          {formatCost(cost)}
        </>
      )}
    </span>
  );
}
