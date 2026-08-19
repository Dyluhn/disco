/**
 * The plan-approval gate + plan view, shared by Build and Deep Research.
 * Before any work runs, the agent's proposed plan is shown here; the human Approves to
 * start or Revises to send focused changes back for a new plan.
 *
 * TWO progress modes — by design, because the two surfaces have different progress
 * truth-sources:
 *
 *  • `progress` (Deep Research): a per-step state map DERIVED FROM THE ENGINE's real
 *    `section_done` ActionEvents — reliable, not model self-report — so DR keeps an
 *    honest ticking checklist.
 *
 *  • `status` (Build): runthru-v2 (#3) removed Build's per-step ticking checklist. It
 *    relied on the model emitting incremental `plan_step(idx, done)` deltas, which EVERY
 *    model tier (Qwen 27B through DeepSeek/GPT/Claude) maintains unreliably — one missed
 *    delta left a step wrong forever, and the finish gate even bounced completed builds
 *    over unticked boxes. Build now shows the plan as an explanation + a STATIC outline
 *    of the approach, with one honest status chip (Building… → Done) reflecting the REAL
 *    run state. This keeps the surface truthful for tiny local models (Gemma 3 E4B) that
 *    can't self-report — "done" comes from the build actually finishing, not from boxes.
 */

import { useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  CircleDashed,
  CirclePause,
  ClipboardList,
  Loader2,
  TriangleAlert,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";
import { Markdown } from "@/components/Markdown";
import { defaultPlanPanelExpanded } from "@/components/build/planPanelState";
import { planProgressSummary } from "@/lib/buildTrace";
import type { StepState, PlanView } from "@/lib/buildTrace";
import type { ConversationStatus } from "@/types/agent";

function StepIcon({ state }: { state: StepState }) {
  if (state === "done") return <Check className="size-3.5 shrink-0 text-supported" aria-hidden />;
  if (state === "active")
    return <Loader2 className="size-3.5 shrink-0 animate-spin text-accent" aria-hidden />;
  if (state === "stalled")
    return (
      <CirclePause
        className="size-3.5 shrink-0 text-warn"
        aria-label="step started but never completed — the run stopped while this step was in progress"
      />
    );
  return <CircleDashed className="size-3.5 shrink-0 text-text-faint" aria-hidden />;
}

/** Build's single, honest progress signal: derived from the REAL conversation status,
 * not from model-reported step marks. Building while the loop runs; Done when it
 * actually finishes; Paused/Stopped surfaced truthfully. */
function StatusChip({ status }: { status: ConversationStatus }) {
  if (status === "FINISHED")
    return (
      <span className="flex items-center gap-hair font-ui text-[0.72rem] font-medium text-supported">
        <Check className="size-3.5" aria-hidden /> Done
      </span>
    );
  if (status === "PAUSED")
    return (
      <span className="flex items-center gap-hair font-ui text-[0.72rem] font-medium text-warn">
        <CirclePause className="size-3.5" aria-hidden /> Paused
      </span>
    );
  if (status === "STUCK" || status === "ERROR")
    return (
      <span className="flex items-center gap-hair font-ui text-[0.72rem] font-medium text-warn">
        <TriangleAlert className="size-3.5" aria-hidden /> Stopped
      </span>
    );
  if (status === "IDLE") return null;
  // RUNNING and the awaiting-* states all read as actively in-flight.
  return (
    <span className="flex items-center gap-hair font-ui text-[0.72rem] font-medium text-accent">
      <Loader2 className="size-3.5 animate-spin" aria-hidden /> Building…
    </span>
  );
}

type StepSummary = { done: number; total: number; fraction: number } | null;

/** The glanceable "N of M" + progress-bar figure for the header. Checklist mode
 * (Deep Research) uses the engine-derived progress map; Build mode has no
 * per-step truth, so it reads all-done-or-nothing off the real run status. No
 * steps at all → no figure (there's nothing to summarize). */
function derivePlanProgress(
  plan: PlanView,
  status: ConversationStatus | undefined,
  progress: Map<number, StepState> | undefined,
  checklist: boolean,
): StepSummary {
  if (plan.steps.length === 0) return null;
  if (checklist) return planProgressSummary(plan.steps.length, progress!);
  const finished = status === "FINISHED";
  return {
    done: finished ? plan.steps.length : 0,
    total: plan.steps.length,
    fraction: finished ? 1 : 0,
  };
}

export function PlanPanel({
  plan,
  progress,
  status,
  onApprove,
  onRevise,
  approveLabel,
}: {
  plan: PlanView;
  /** Deep Research tracker mode: per-step state DERIVED FROM ENGINE section_done
   *  events (reliable). When present, renders the honest ticking checklist. */
  progress?: Map<number, StepState>;
  /** Build tracker mode: the live conversation status, used for the honest status
   *  chip. Omitted at the approval gate (where the chip would be meaningless). */
  status?: ConversationStatus;
  /** Gate mode: present only while awaiting approval. Omit for the read-only view. */
  onApprove?: () => void;
  onRevise?: (text: string) => void;
  /** Override the approve button label. Defaults to "Approve & build" — the
   *  build surface keeps that; the research surface overrides to
   *  "Approve research plan" (fix-c #1 — build & research gates share a panel
   *  but mean different things; the button text must reflect which). */
  approveLabel?: string;
}) {
  const [revising, setRevising] = useState(false);
  const [text, setText] = useState("");
  const gate = Boolean(onApprove);
  // Checklist mode only when a reliable engine-derived progress map is supplied
  // (Deep Research) and we're not at the approval gate.
  const checklist = !gate && progress !== undefined;
  const stepSummary = derivePlanProgress(plan, status, progress, checklist);
  const defaultExpanded = defaultPlanPanelExpanded({ gate, status });
  const [manualExpanded, setManualExpanded] = useState<boolean | null>(null);
  const expanded = manualExpanded ?? defaultExpanded;

  const submitRevision = () => {
    const t = text.trim();
    if (!t || !onRevise) return;
    onRevise(t);
    setText("");
    setRevising(false);
  };

  return (
    <section
      role={gate ? "alertdialog" : undefined}
      aria-label={gate ? "Plan needs your approval" : "Plan"}
      className={cn(
        "rounded-card bg-surface-1 px-body py-body",
        gate ? "border-2 border-accent" : "border border-hairline",
      )}
    >
      <PlanPanelHeader
        plan={plan}
        gate={gate}
        checklist={checklist}
        status={status}
        stepSummary={stepSummary}
        expanded={expanded}
        onToggle={() => setManualExpanded(!expanded)}
      />

      <div
        id={`plan-panel-body-${plan.id}`}
        data-testid="plan-panel-body"
        aria-hidden={!expanded}
        className={cn(
          "grid transition-[grid-template-rows,opacity] duration-200 ease-out",
          expanded ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
        )}
      >
        <div className="min-h-0 overflow-hidden">
          <p className="mt-inline font-ui text-[0.84rem] leading-snug text-text-muted">
            {plan.summary}
          </p>

          {plan.context && (
            <details
              className="mt-inline rounded-control border border-hairline bg-surface-2 px-inline py-hair"
              // Open by default at the gate (the rationale is the user's load-bearing input
              // to the approve/revise decision); collapsed during execution.
              open={gate}
            >
              <summary className="cursor-pointer font-ui text-[0.74rem] uppercase tracking-wide text-text-faint">
                Context &amp; rationale
              </summary>
              <div className="mt-hair text-[0.84rem] leading-relaxed text-text-muted">
                <Markdown>{plan.context}</Markdown>
              </div>
            </details>
          )}

          <PlanStepsList steps={plan.steps} checklist={checklist} progress={progress} />

          {gate && (
            <PlanApprovalGate
              revising={revising}
              text={text}
              onTextChange={setText}
              onStartRevise={() => setRevising(true)}
              onCancelRevise={() => setRevising(false)}
              onSubmitRevise={submitRevision}
              onApprove={onApprove}
              approveLabel={approveLabel}
            />
          )}
        </div>
      </div>
    </section>
  );
}

/** The collapsible header: chevron toggle, title, revision badge, the glanceable
 * step-progress figure or Build's honest status chip, and the gate's "needs your
 * approval" pill. */
function PlanPanelHeader({
  plan,
  gate,
  checklist,
  status,
  stepSummary,
  expanded,
  onToggle,
}: {
  plan: PlanView;
  gate: boolean;
  checklist: boolean;
  status?: ConversationStatus;
  stepSummary: StepSummary;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <header className="flex items-center gap-inline">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={`plan-panel-body-${plan.id}`}
        aria-label={expanded ? "Collapse plan" : "Expand plan"}
        data-disco-control="plan-panel-toggle"
        // Mobile (<lg): the row WRAPS instead of overflowing — the fixed-width
        // badges (revision chip, step figure) drop to their own line the moment
        // the title + chevron + icon don't leave them room, so nothing ever
        // paints on top of the "Review the plan" heading. `min-h-11` keeps the
        // whole toggle a real 44px touch target even when it's one short line
        // (e.g. the collapsed read-only "Plan" state). lg+: reverts to the
        // original single-line, fixed-height row — pixel-identical to before.
        className="-mx-hair flex min-h-11 min-w-0 flex-1 flex-wrap items-center gap-x-inline gap-y-hair rounded-control px-hair py-hair text-left transition-colors hover:bg-surface-2/60 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent lg:min-h-0 lg:flex-nowrap"
      >
        {expanded ? (
          <ChevronDown className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        )}
        <ClipboardList className="size-4 shrink-0 text-accent" aria-hidden />
        {/* Mobile: the title can shrink/truncate as a last-resort safety net
            instead of forcing the row wider than the viewport — it never
            needs to in practice ("Review the plan"/"Plan" are short, fixed
            strings), but the row must never rely on that. lg+: back to the
            original fixed-width, non-wrapping label. */}
        <span className="min-w-0 shrink truncate font-ui text-[0.9rem] font-medium text-text lg:min-w-fit lg:shrink-0 lg:whitespace-nowrap">
          {gate ? "Review the plan" : "Plan"}
        </span>
        <span className="shrink-0 rounded-full border border-hairline px-inline py-px font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
          revision {plan.revision}
        </span>
        <span className="flex shrink-0 items-center gap-inline lg:ml-auto">
          {stepSummary && (
            <span className="flex items-center gap-hair">
              <span className="font-mono text-[0.72rem] text-text-faint">
                {stepSummary.done}/{stepSummary.total}
              </span>
              <span className="h-1 w-14 overflow-hidden rounded-full bg-surface-2" aria-hidden>
                <span
                  className="block h-full bg-accent transition-[width]"
                  style={{ width: `${Math.round(stepSummary.fraction * 100)}%` }}
                />
              </span>
            </span>
          )}
          {/* Build: one honest status chip from the real run state. */}
          {!gate && !checklist && status && <StatusChip status={status} />}
        </span>
      </button>
      {gate && (
        <span className="shrink-0 rounded-full border border-accent px-inline py-px font-ui text-[0.7rem] uppercase tracking-wide text-accent">
          needs your approval
        </span>
      )}
    </header>
  );
}

/** The numbered outline of steps. Checklist mode (DR) shows a per-step icon;
 * Build shows a static numbered outline of the approach (no per-step state at
 * all). Omitted entirely when there are no real steps (never a fake "1." placeholder). */
function PlanStepsList({
  steps,
  checklist,
  progress,
}: {
  steps: PlanView["steps"];
  checklist: boolean;
  progress?: Map<number, StepState>;
}) {
  if (steps.length === 0) return null;
  return (
    <ol className="mt-body flex flex-col gap-hair">
      {steps.map((step, i) => {
        const state = checklist ? (progress!.get(i + 1) ?? "pending") : null;
        return (
          <li key={i} className="flex items-start gap-inline">
            <span className="mt-px flex items-center gap-hair">
              <span className="w-4 text-right font-mono text-[0.72rem] text-text-faint">
                {i + 1}
              </span>
              {state && <StepIcon state={state} />}
            </span>
            <span
              className={cn(
                "font-ui text-[0.84rem] leading-snug",
                state === "done" ? "text-text-muted" : "text-text",
              )}
            >
              {step.title}
              {step.detail && (
                <span className="block text-[0.76rem] text-text-faint">{step.detail}</span>
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** The approval gate's action row: Approve/Revise… normally, or the revision
 * textarea + Send/Cancel once Revise… has been clicked. */
function PlanApprovalGate({
  revising,
  text,
  onTextChange,
  onStartRevise,
  onCancelRevise,
  onSubmitRevise,
  onApprove,
  approveLabel,
}: {
  revising: boolean;
  text: string;
  onTextChange: (text: string) => void;
  onStartRevise: () => void;
  onCancelRevise: () => void;
  onSubmitRevise: () => void;
  onApprove?: () => void;
  approveLabel?: string;
}) {
  if (revising) {
    return (
      <div className="mt-body flex flex-col gap-hair">
        <textarea
          value={text}
          onChange={(e) => onTextChange(e.target.value)}
          rows={2}
          autoFocus
          placeholder="Describe the changes you want in the plan…"
          className="w-full resize-none rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
        />
        {/* Mobile: full-width, stacked, thumb-sized actions — Send (primary)
            on top via flex-col-reverse (it's the later DOM child), Cancel
            below. lg+: reverts to the original single-line right-aligned
            row of auto-width buttons. */}
        <div className="flex flex-col-reverse gap-inline lg:flex-row lg:items-center lg:justify-end">
          <button
            type="button"
            onClick={onCancelRevise}
            className={cn(
              TAP_TARGET,
              "w-full rounded-control border border-hairline px-body py-hair text-center font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text lg:w-auto",
            )}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onSubmitRevise}
            disabled={!text.trim()}
            data-disco-control="revise-plan"
            className={cn(
              TAP_TARGET,
              "w-full rounded-control bg-accent px-body py-hair text-center font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40 lg:w-auto",
            )}
          >
            Send revision
          </button>
        </div>
      </div>
    );
  }
  return (
    // Mobile: full-width, stacked, thumb-sized actions — Approve (primary)
    // on top via flex-col-reverse (it's the later DOM child), Revise below
    // as the clearly-secondary outline button. lg+: reverts to the original
    // single-line right-aligned row of auto-width buttons.
    <div className="mt-body flex flex-col-reverse gap-inline lg:flex-row lg:items-center lg:justify-end">
      <button
        type="button"
        onClick={onStartRevise}
        data-disco-control="revise-plan-open"
        className={cn(
          TAP_TARGET,
          "w-full rounded-control border border-hairline px-body py-hair text-center font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text lg:w-auto",
        )}
      >
        Revise…
      </button>
      <button
        type="button"
        onClick={onApprove}
        data-disco-control="approve-plan"
        className={cn(
          TAP_TARGET,
          "w-full rounded-control bg-accent px-body py-hair text-center font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90 lg:w-auto",
        )}
      >
        {approveLabel ?? "Approve & build"}
      </button>
    </div>
  );
}
