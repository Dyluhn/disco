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
import { Loader2, OctagonX, Play, ShieldCheck, ShieldHalf, Square, WifiOff } from "lucide-react";
import { cn } from "@/lib/cn";
import type { AgentEvent, ConversationStatus, IsolationInfo } from "@/types/agent";
import { useModels } from "@/hooks/useModels";
import { calculateUsageCost, formatCost } from "@/lib/cost";
import { isMetered, isSubscription, type TokenUsage } from "@/types/models";

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
  connectionState = "connected",
  autonomous,
  assist,
  onKill,
  onStop,
  onResume,
  events = [],
  modelId,
  seq,
  surface = "build",
}: {
  status: ConversationStatus;
  /** BP-15: null until the first state frame — renders muted '…'. */
  isolation: IsolationInfo | null;
  /** Runtime sandbox liveness overlay ('active' | 'suspended' | undefined). */
  sandboxState?: "active" | "suspended";
  /** Live stream health. Degraded means the socket is retrying past the normal window. */
  connectionState?: "connected" | "degraded";
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
  /** Highest observed event seq (stream maxSeq) — exposed as data-seq so the
   *  live gauntlet judges finish-freshness by state VERSION, not by having
   *  witnessed the transitions on a possibly-dead stream. */
  seq?: number;
  surface?: "build" | "agent";
}) {
  // Deliberately unused: `assist` stays in the signature for API stability
  // (see its doc comment) while the pill it drove is deprecated.
  void assist;
  const waiting =
    status === "WAITING_FOR_CONFIRMATION" || status === "AWAITING_PLAN_APPROVAL";

  return (
    <div className="flex items-center justify-between gap-inline">
      <div className="flex items-center gap-inline">
        <StatusLabel status={status} waiting={waiting} seq={seq} />
        <StatusBadges
          isolation={isolation}
          autonomous={autonomous}
          sandboxState={sandboxState}
          connectionState={connectionState}
          events={events}
          modelId={modelId}
        />
      </div>
      <AgentControls
        status={status}
        onKill={onKill}
        onStop={onStop}
        onResume={onResume}
        surface={surface}
      />
    </div>
  );
}

/** The status text + spinner, colored by waiting/active/idle. The raw
 * ConversationStatus is exposed on data-status so tests read the real phase
 * deterministically — never by regexing the page body, where the status label
 * collides with agent chat prose (an agent SAYING "working" is not the run
 * being RUNNING). The visible text stays the friendly label. */
function StatusLabel({
  status,
  waiting,
  seq,
}: {
  status: ConversationStatus;
  waiting: boolean;
  seq?: number;
}) {
  const active = ACTIVE.includes(status);
  return (
    <span
      data-disco-control="build.status"
      data-status={status}
      data-seq={seq ?? 0}
      className={cn(
        "flex items-center gap-hair font-ui text-[0.8rem]",
        waiting ? "text-warn" : active ? "text-text" : "text-text-muted",
      )}
    >
      {status === "RUNNING" && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
      {STATUS_LABEL[status]}
    </span>
  );
}

/** The row of informational pills: isolation tier, autonomous tier, suspended
 * sandbox, degraded connection, and the cumulative cost meter. Each pill
 * decides its own visibility — the caller always renders this fragment.
 *
 * The Assist/Standard execution-tier pill that used to live here is
 * deliberately hidden (deprecated control, not removed) — the server-derived
 * value it read still exists at `useBuildStream`'s `assist` field / the
 * reducer's `extras.assist`, this component just no longer renders it. */
function StatusBadges({
  isolation,
  autonomous,
  sandboxState,
  connectionState,
  events,
  modelId,
}: {
  isolation: IsolationInfo | null;
  autonomous?: boolean;
  sandboxState?: "active" | "suspended";
  connectionState: "connected" | "degraded";
  events: AgentEvent[];
  modelId?: string | null;
}) {
  const Shield = isolation?.adversarialSafe ? ShieldCheck : ShieldHalf;
  return (
    <>
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
      {sandboxState === "suspended" && (
        <span
          title="Sandbox suspended — workspace is saved; it will resume on your next message"
          className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-muted"
        >
          suspended
        </span>
      )}
      {connectionState === "degraded" && (
        <span
          title="Stream reconnecting — activity will replay when the connection returns"
          data-disco-control="build.connection"
          data-connection-state="degraded"
          className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-muted"
        >
          <WifiOff className="size-3" aria-hidden />
          reconnecting…
        </span>
      )}
      {/* RP-14: cost meter mounts only when a model is wired in — callers that
          don't pass modelId (and the pre-RP-14 test suites) never touch the
          models query, so no QueryClientProvider is required of them. */}
      {modelId != null && <CostMeter events={events} modelId={modelId} />}
    </>
  );
}

/** The Resume / Stop / Kill control cluster. Owns the two pieces of purely
 * client-local pending state (the cooperative "Stopping…" wait and the Kill
 * arm/confirm) — neither is derived from server state, both just gate which
 * button is visible right now. */
function AgentControls({
  status,
  onKill,
  onStop,
  onResume,
  surface,
}: {
  status: ConversationStatus;
  onKill: () => void;
  onStop?: () => void;
  onResume?: () => void;
  surface: "build" | "agent";
}) {
  const active = ACTIVE.includes(status);

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
    <div className="flex items-center gap-hair">
      <ResumeButton status={status} onResume={onResume} surface={surface} />
      <StopButton
        status={status}
        onStop={onStop}
        confirmingKill={confirmingKill}
        stopping={stopping}
        onClick={() => {
          setStopping(true);
          onStop?.();
        }}
      />
      <KillControl
        active={active}
        confirmingKill={confirmingKill}
        onArm={() => setConfirmingKill(true)}
        onCancel={() => setConfirmingKill(false)}
        onConfirm={() => {
          setConfirmingKill(false);
          onKill();
        }}
      />
    </div>
  );
}

function ResumeButton({
  status,
  onResume,
  surface,
}: {
  status: ConversationStatus;
  onResume?: () => void;
  surface: "build" | "agent";
}) {
  if (!onResume || (status !== "PAUSED" && status !== "IDLE")) return null;
  return (
    <button
      type="button"
      onClick={onResume}
      aria-label={`Resume the agent — continue this ${surface === "agent" ? "task" : "build"} where it left off`}
      data-disco-control="resume"
      className="flex max-lg:min-h-11 items-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:border-accent hover:bg-accent/5"
    >
      <Play className="size-3.5" aria-hidden />
      Resume
    </button>
  );
}

/** Graceful Stop is meaningful while the agent is actually running or waiting
 * on a gate — a non-destructive halt, distinct from the red teardown Kill.
 * Hidden while a Kill confirm is armed (mutually exclusive controls). */
function StopButton({
  status,
  onStop,
  confirmingKill,
  stopping,
  onClick,
}: {
  status: ConversationStatus;
  onStop?: () => void;
  confirmingKill: boolean;
  stopping: boolean;
  onClick: () => void;
}) {
  const canStop =
    status === "RUNNING" ||
    status === "WAITING_FOR_CONFIRMATION" ||
    status === "AWAITING_USER_DECISION" ||
    status === "AWAITING_USER_QUESTION" ||
    status === "PAUSED";
  if (!onStop || !canStop || status === "PAUSED" || confirmingKill) return null;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={stopping}
      aria-label="Stop the agent gracefully (does not tear down the sandbox)"
      data-disco-control="stop"
      // #22: stable, deterministic phase of the cooperative stop. "idle" = can
      // stop, not yet clicked; "stopping" = clicked, loop still finishing its
      // in-flight step. The terminal "stopped" phase is the control's ABSENCE
      // (the loop settled to PAUSED/IDLE and this button unmounts) — asserted
      // via the status label + Resume affordance, never a lying live Stop.
      data-stop-state={stopping ? "stopping" : "idle"}
      className="flex max-lg:min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-text-muted hover:text-text disabled:opacity-60"
    >
      {stopping ? (
        <Loader2 className="size-3.5 animate-spin" aria-hidden />
      ) : (
        <Square className="size-3.5" aria-hidden />
      )}
      {stopping ? "Stopping…" : "Stop"}
    </button>
  );
}

/** Kill: destructive → inline confirm. First click arms; second confirms. */
function KillControl({
  active,
  confirmingKill,
  onArm,
  onCancel,
  onConfirm,
}: {
  active: boolean;
  confirmingKill: boolean;
  onArm: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  if (confirmingKill) {
    return (
      <div className="flex items-center gap-hair">
        <span className="whitespace-nowrap font-ui text-[0.74rem] text-unsupported">
          Kill this run?
        </span>
        <button
          type="button"
          onClick={onConfirm}
          aria-label="Confirm kill: stop, tear down the sandbox, revoke its access"
          data-disco-control="kill-confirm"
          className="flex max-lg:min-h-11 items-center gap-hair rounded-control border border-unsupported bg-unsupported px-inline py-hair font-ui text-[0.78rem] font-medium text-bg transition-opacity hover:opacity-90"
        >
          <OctagonX className="size-3.5" aria-hidden />
          Confirm
        </button>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Cancel — keep the run"
          className="max-lg:min-h-11 rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text"
        >
          Cancel
        </button>
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onArm}
      disabled={!active}
      aria-label="Kill the agent: stop, tear down the sandbox, revoke its access"
      data-disco-control="kill"
      className={cn(
        "flex max-lg:min-h-11 items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.78rem] font-medium transition-colors",
        active
          ? "border-unsupported text-unsupported hover:bg-unsupported hover:text-bg"
          : "cursor-not-allowed border-hairline text-text-faint",
      )}
    >
      <OctagonX className="size-3.5" aria-hidden />
      Kill
    </button>
  );
}

/** RP-14: cumulative session cost. Isolated in its own component so the
 * models query only runs when a caller actually wires in a model — and so
 * the meter can be hidden on paid models until the event stream carries real
 * usage data (backend field pending, wave 2): a meter pinned at "≥$0.00"
 * regardless of real spend would be a false affordance, not a lower bound. */
function CostMeter({ events, modelId }: { events: AgentEvent[]; modelId: string }) {
  const { data: models } = useModels();

  const { cost, hasMissingUsage, mode, sawUsage } = useMemo(() => {
    let total = 0;
    let missing = false;
    let anyPaid = false;
    let usageSeen = false;

    // The current session model's pay model decides the meter mode. Only METERED
    // (per-token) models drive the $ meter; a SUBSCRIPTION model is a flat plan fee
    // with no usage-derived cost (W-05) — it must read "Subscription", never "Free"
    // and never a price; a truly free model reads "Free".
    const currentModel = models?.find((m) => m.id === modelId);
    if (currentModel && isMetered(currentModel)) anyPaid = true;

    for (const e of events) {
      if (e.kind !== "action") continue;
      // Usage may be in a top-level field or tucked in meta.
      const usage = (e as { usage?: TokenUsage }).usage || (e.meta?.usage as TokenUsage | undefined);
      if (usage) usageSeen = true;
      // Attribute the cost using the model that was active for THAT action.
      const mId = (e.meta?.model_id as string) || modelId;
      const m = models?.find((m) => m.id === mId);

      if (m && isMetered(m)) {
        anyPaid = true;
        if (usage) {
          total += calculateUsageCost(m, usage);
        } else {
          missing = true;
        }
      }
    }
    // Precedence: any real per-token spend → metered $; else a subscription current
    // model → "Subscription"; else "Free". A subscription model NEVER collapses to
    // "Free" and NEVER shows a per-token price (W-05).
    const resolved: "metered" | "subscription" | "free" = anyPaid
      ? "metered"
      : currentModel && isSubscription(currentModel)
        ? "subscription"
        : "free";
    return { cost: total, hasMissingUsage: missing, mode: resolved, sawUsage: usageSeen };
  }, [events, models, modelId]);

  // #21: when metered-but-no-usage-yet we render NO visible meter (a meter pinned at
  // "$0.00" would be a false lower bound). Emit a zero-size, aria-hidden sentinel
  // carrying data-cost-state="hidden" so a test can deterministically assert the
  // branch — it is a regression seam, not a visible affordance.
  if (mode === "metered" && !sawUsage)
    return <span hidden aria-hidden data-disco-control="build.cost-meter" data-cost-state="hidden" />;

  // W-05: a subscription model is a flat plan — show "Subscription" (never "Free",
  // never a $ rate), so nobody reads a paid plan as free.
  if (mode === "subscription")
    return (
      <span
        data-disco-control="build.cost-meter"
        data-cost-state="subscription"
        title="This model is on a flat-rate subscription plan — no per-token charge"
        className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-muted"
      >
        Subscription
      </span>
    );

  return (
    <span
      data-disco-control="build.cost-meter"
      data-cost-state={mode === "free" ? "free" : "usd"}
      title={
        mode === "free"
          ? "This model is free"
          : hasMissingUsage
            ? "Usage data is missing for some turns; this is a lower bound."
            : "Cumulative session cost"
      }
      className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-muted"
    >
      {mode === "free" ? (
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
