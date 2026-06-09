/**
 * The Build (Agent) surface — the split-pane "Sidecar" (BoD §13.4/§13.5/§8.2): a control
 * pane (the Project-Manager view: plain-language Activity Feed + confidence gradient, the
 * confirmation gate, the Steering Wheel, status + the kill switch) BESIDE a live execution
 * canvas/Inspector (Files · Terminal · Preview · Live). The technical detail lives in the
 * canvas; the left pane stays a readable narrative — never a black box, never a debug log.
 *
 * Data-flow discipline: reads only the useBuild() hook; never fetches.
 */

import { useEffect, useMemo, useRef } from "react";
import { useBuild } from "@/hooks/useBuild";
import { useBuildNotifications } from "@/hooks/useBuildNotifications";
import { cn } from "@/lib/cn";
import {
  deriveActivity,
  deriveDeliverable,
  deriveLiveSignal,
  latestAgentMessage,
} from "@/lib/buildTrace";
import { agentHttpBase } from "@/api/client";
import type { IsolationInfo } from "@/types/agent";
import { EmptyState, ErrorState } from "@/components/states";
import { Markdown } from "@/components/Markdown";
import { QueryInput } from "@/components/QueryInput";
import { Download, Loader2 } from "lucide-react";
import { useDownloadProject, useExportManifest } from "@/hooks/useProjects";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { LiveSignalBar } from "@/components/build/LiveSignalBar";
import { ResizableSplit } from "@/components/build/ResizableSplit";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import { BuildModelPicker } from "@/components/build/BuildModelPicker";
import { ConfirmationPanel } from "@/components/build/ConfirmationPanel";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import { PlanPanel } from "@/components/build/PlanPanel";
import { DeliverablePanel } from "@/components/build/DeliverablePanel";
import { SteerInput } from "@/components/build/SteerInput";
import { AlternativesGate } from "@/components/build/AlternativesGate";
import { AskPanel } from "@/components/build/AskPanel";

// The active isolation tier, surfaced honestly at the point of use (the local container
// backend the Build surface runs on — shared host kernel, not for adversarial workloads).
// [GAP] the server should send the live tier over the wire; this reflects the deployed
// default until then.
const ISOLATION: IsolationInfo = {
  tier: "local container",
  label:
    "Container-grade isolation (shared host kernel) — appropriate for trusted local use, not adversarial workloads.",
  adversarialSafe: false,
};

export function BuildSurface({ resumeCid }: { resumeCid?: string | null } = {}) {
  const b = useBuild(resumeCid);
  const download = useDownloadProject();
  const exportManifest = useExportManifest();
  const activity = useMemo(
    () => deriveActivity(b.events, b.pendingActionId, b.status),
    [b.events, b.pendingActionId, b.status],
  );
  const liveSignal = useMemo(
    () => deriveLiveSignal(b.events, b.status),
    [b.events, b.status],
  );
  const finalMessage = useMemo(() => latestAgentMessage(b.events), [b.events]);
  const deliverable = useMemo(() => deriveDeliverable(b.events), [b.events]);

  // Attention when tabbed away: badge the title + (best-effort) OS-notify when the
  // run finishes or needs the user while the tab is hidden.
  useBuildNotifications(b.status, b.task);

  // Auto-scroll the feed to the bottom ONLY on a "you need to look now" moment —
  // never on every event (that would yank the user off whatever they're reading).
  // Two triggers: (1) the agent needs input (a gate just opened), or (2) a
  // capstone happened (the run finished/stuck/errored, or a deliverable landed).
  // We detect the TRANSITION into those, so re-renders mid-state don't re-scroll.
  const feedScrollRef = useRef<HTMLDivElement>(null);
  const prevStatusRef = useRef(b.status);
  const prevDeliverableRef = useRef<string | null>(null);
  useEffect(() => {
    const el = feedScrollRef.current;
    const prevStatus = prevStatusRef.current;
    const prevDeliverable = prevDeliverableRef.current;
    const deliverableId = deliverable?.id ?? null;
    prevStatusRef.current = b.status;
    prevDeliverableRef.current = deliverableId;
    if (!el) return;

    const needsInput =
      b.status === "WAITING_FOR_CONFIRMATION" ||
      b.status === "AWAITING_USER_DECISION" ||
      b.status === "AWAITING_USER_QUESTION" ||
      b.status === "AWAITING_PLAN_APPROVAL";
    const capstone = b.status === "FINISHED" || b.status === "STUCK" || b.status === "ERROR";
    const statusTransitioned = b.status !== prevStatus;
    const newDeliverable = deliverableId !== null && deliverableId !== prevDeliverable;

    if ((statusTransitioned && (needsInput || capstone)) || newDeliverable) {
      // Smooth in the browser; fall back to the scrollTop property (jsdom has no
      // scrollTo) so the behavior is testable.
      if (typeof el.scrollTo === "function") {
        el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      } else {
        el.scrollTop = el.scrollHeight;
      }
    }
  }, [b.status, deliverable?.id]);

  // Continuous "follow the stream" auto-scroll: as the feed grows during a run,
  // keep the latest line in view — but ONLY when the user is already near the
  // bottom (within 120px). If they've scrolled up to read history, we don't yank
  // them back down. Keyed on the activity length so it fires per new line.
  useEffect(() => {
    const el = feedScrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [activity.length, b.streamingFile?.content]);
  // Steer is available whenever a conversation EXISTS to steer — including
  // STUCK/FINISHED, since the engine reopens on send_message (engine.py:671).
  // Plan-approval is the only state where steering is wrong (the plan IS the
  // input). Keeping SteerInput disabled mid-confirmation matches the prior shape.
  const steerable =
    b.status === "RUNNING" ||
    b.status === "WAITING_FOR_CONFIRMATION" ||
    b.status === "AWAITING_USER_DECISION" ||
    b.status === "STUCK" ||
    b.status === "FINISHED" ||
    b.status === "PAUSED";
  const settled = b.status === "FINISHED" || b.status === "IDLE" || b.status === "STUCK";
  // Front-door "drafting…" moment: the run is live but no plan exists yet (the
  // agent is exploring + composing the first plan). Show a skeleton so the surface
  // never reads as frozen between submit and the plan-approval gate.
  const draftingPlan = b.status === "RUNNING" && !b.plan;
  const terminalIncomplete = b.status === "STUCK" || b.status === "ERROR";

  if (!b.started) {
    return (
      <div className="flex min-h-full flex-col pt-section">
        <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
          <EmptyState />
          <div className="w-full max-w-measure">
            <div className="mb-inline flex items-center justify-between gap-inline">
              <BuildModelPicker value={b.modelId} onChange={b.setModelId} />
              <span className="font-ui text-[0.72rem] text-text-faint">runs the agent</span>
            </div>
            <QueryInput
              onSubmit={b.submit}
              busy={b.submitting}
              autoFocus
              placeholder="Describe what you want the agent to build or do…"
            />
            <p className="mt-inline text-center font-ui text-[0.78rem] text-text-faint">
              The agent works in a sandbox and shows its plan. Risky steps pause for your approval.
            </p>
            {b.submitError && (
              <p role="alert" className="mt-inline text-center font-ui text-[0.8rem] text-unsupported">
                {b.submitError instanceof Error
                  ? b.submitError.message
                  : "Couldn't start the build — the server didn't respond. Try again."}
              </p>
            )}
          </div>
        </main>
      </div>
    );
  }

  // ── Control pane (chat) — the Project-Manager view ─────────────────────
  const chatPane = (
    <>
        <div className="flex flex-col gap-inline px-body pt-section">
          <AgentStatusBar
            status={b.status}
            isolation={ISOLATION}
            onKill={b.kill}
            onStop={b.cancel}
            onResume={b.resume}
          />
          <div className="flex items-center justify-between gap-inline">
            <h1 className="font-display text-[1.3rem] font-medium leading-tight tracking-tight text-text">
              {b.task}
            </h1>
            {/* Export the saved project as a zip — only available for resumed
                projects and finished builds (a snapshot must exist on disk). */}
            {b.cid && (b.resumed || b.status === "FINISHED") && (
              <div className="flex shrink-0 flex-col items-end gap-hair">
                <button
                  type="button"
                  onClick={() => b.cid && download.mutate(b.cid)}
                  disabled={download.isPending}
                  aria-label="Download the project as a zip"
                  title="Download a zip of the project files"
                  className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-40"
                >
                  <Download className="size-3.5" aria-hidden />
                  {download.isPending ? "Preparing…" : "Export"}
                </button>
                {download.error && (
                  <span role="alert" className="font-ui text-[0.7rem] text-unsupported">
                    Download failed — try again
                  </span>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="mt-section flex min-h-0 flex-1 flex-col px-body">
          <div className="flex items-baseline justify-between">
            <h2 className="font-ui text-[0.72rem] font-medium uppercase tracking-wide text-text-faint">
              Activity
            </h2>
            <span
              className="font-ui text-[0.68rem] text-text-faint"
              title="The agent works one step at a time; an up-front plan preview lands with the planner step."
            >
              live task list
            </span>
          </div>
          <div
            ref={feedScrollRef}
            className="mt-inline min-h-0 flex-1 overflow-y-auto pb-inline lg:pr-hair"
          >
            {b.status === "ERROR" ? (
              <ErrorState message={b.error ?? "The agent run failed."} onRetry={b.reset} />
            ) : b.awaitingPlan && b.plan ? (
              // Plan gate is the hero: review + Approve/Revise before any work runs.
              <div className="flex flex-col gap-section">
                <PlanPanel
                  plan={b.plan}
                  progress={b.planProgress}
                  onApprove={b.approvePlan}
                  onRevise={b.requestPlan}
                />
                {activity.length > 0 && <ActivityFeed items={activity} />}
              </div>
            ) : (
              <>
                {/* front-door: drafting the first plan (live, no plan yet) */}
                {draftingPlan && (
                  <div
                    aria-label="Drafting a plan"
                    className="mb-section flex flex-col gap-hair rounded-control border border-hairline bg-surface-1 p-body"
                  >
                    <div className="flex items-center gap-hair font-ui text-[0.84rem] text-text-muted">
                      <Loader2 className="size-3.5 animate-spin" aria-hidden />
                      Drafting a plan — exploring the workspace and shaping the steps…
                    </div>
                    <div className="mt-hair flex flex-col gap-hair" aria-hidden>
                      <div className="h-2.5 w-2/3 animate-pulse rounded bg-surface-2" />
                      <div className="h-2.5 w-5/6 animate-pulse rounded bg-surface-2" />
                      <div className="h-2.5 w-1/2 animate-pulse rounded bg-surface-2" />
                    </div>
                  </div>
                )}
                {/* read-only capstone tracker: steps check off as the agent reports
                    them. Sticky to the top of the scroll area so the plan + progress
                    stay visible while the activity feed scrolls beneath it. */}
                {b.plan && (
                  <div className="sticky top-0 z-10 mb-section bg-bg pb-inline">
                    <PlanPanel plan={b.plan} progress={b.planProgress} />
                  </div>
                )}
                <ActivityFeed items={activity} />
                <div className="mt-inline">
                  <LiveSignalBar signal={liveSignal} />
                </div>
                {finalMessage &&
                  (b.status === "FINISHED" ||
                    b.status === "STUCK" ||
                    b.status === "ERROR" ||
                    b.status === "AWAITING_USER_DECISION") && (
                    <div
                      className={cn(
                        "mt-section rounded-card border px-body py-inline text-[0.95rem]",
                        b.status === "STUCK" || b.status === "ERROR"
                          ? "border-warn/40 bg-warn/5"
                          : "border-hairline bg-surface-1",
                      )}
                    >
                      {(b.status === "STUCK" || b.status === "ERROR") && (
                        <p className="mb-hair font-ui text-[0.74rem] uppercase tracking-wide text-warn">
                          Agent's latest reply
                        </p>
                      )}
                      <Markdown>{finalMessage}</Markdown>
                    </div>
                  )}
              </>
            )}
          </div>
        </div>

        {/* gate · steer · re-plan — pinned under the feed */}
        <div className="flex flex-col gap-inline border-t border-hairline px-body py-inline">
          {/* finished-artifact handoff: open the live app / download the files */}
          <DeliverablePanel
            deliverable={deliverable}
            onOpen={() =>
              b.cid &&
              window.open(
                `${agentHttpBase()}/conversations/${b.cid}/preview-app/`,
                "_blank",
                "noopener,noreferrer",
              )
            }
            onDownload={() => b.cid && download.mutate(b.cid)}
            onExportManifest={() => b.cid && exportManifest.mutate(b.cid)}
          />
          {b.pendingAction && (
            <ConfirmationPanel action={b.pendingAction} onApprove={b.confirm} onReject={b.reject} />
          )}
          {b.awaitingDecision && b.pendingAlternatives && (
            <AlternativesGate
              alternatives={b.pendingAlternatives}
              onPick={b.pickAlternative}
            />
          )}
          {b.awaitingQuestion && (
            <AskPanel
              question={b.pendingQuestion?.message?.content ?? finalMessage ?? "The agent has a question."}
              onAnswer={b.answer}
            />
          )}
          {steerable && (
            <div className="flex flex-col gap-hair">
              {terminalIncomplete && (
                <p className="font-ui text-[0.74rem] text-warn">
                  The run stopped before the plan was complete. Send a message to
                  steer the agent back in, or use “Plan a change…” below to
                  re-enter plan mode.
                </p>
              )}
              <SteerInput
                onSteer={b.steer}
                disabled={b.status === "WAITING_FOR_CONFIRMATION"}
              />
            </div>
          )}
          {settled && (
            <div className="flex flex-col gap-hair">
              <BuildModelPicker value={b.modelId} onChange={b.setModelId} />
              {/* re-enter plan mode: a focused, diff-style change is planned + re-approved */}
              <QueryInput onSubmit={b.requestPlan} placeholder="Plan a change to this build…" />
            </div>
          )}
        </div>
    </>
  );

  return (
    <ResizableSplit
      chat={chatPane}
      inspector={
        <ExecutionCanvas
          events={b.events}
          status={b.status}
          cid={b.cid}
          streamingFile={b.streamingFile}
        />
      }
    />
  );
}
