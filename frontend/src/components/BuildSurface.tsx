/**
 * The Build (agent) surface — the live agentic loop made visible + controllable. The
 * Research template, with agent semantics: a task drives a plan→act→observe trace
 * (AgentTrace), risky actions pause at a confirmation gate (ConfirmationPanel) with the
 * risk rationale in view, and a prominent kill switch is always reachable during a run.
 *
 * Data-flow discipline: this component reads only the useBuild() hook; it never fetches.
 */

import { useBuild } from "@/hooks/useBuild";
import type { IsolationInfo } from "@/types/agent";
import { EmptyState, ErrorState } from "@/components/states";
import { QueryInput } from "@/components/QueryInput";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import { AgentTrace } from "@/components/build/AgentTrace";
import { ConfirmationPanel } from "@/components/build/ConfirmationPanel";

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

export function BuildSurface() {
  const b = useBuild();

  if (!b.started) {
    return (
      <div className="flex min-h-full flex-col pt-section">
        <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
          <EmptyState />
          <div className="w-full max-w-measure">
            <QueryInput
              onSubmit={b.submit}
              busy={b.submitting}
              autoFocus
              placeholder="Describe a task for the agent to build or do…"
            />
            <p className="mt-inline text-center font-ui text-[0.78rem] text-text-faint">
              The agent acts in a sandbox. Risky steps pause for your approval.
            </p>
          </div>
        </main>
      </div>
    );
  }

  const running = b.status === "RUNNING" || b.status === "WAITING_FOR_CONFIRMATION";

  return (
    <div className="flex min-h-full flex-col pt-section">
      <main className="mx-auto flex w-full max-w-doc flex-1 flex-col gap-section px-body pb-major">
        <AgentStatusBar status={b.status} isolation={ISOLATION} onKill={b.kill} />

        <h1 className="font-display text-[1.5rem] font-medium leading-tight tracking-tight text-text">
          {b.task}
        </h1>

        {b.status === "ERROR" ? (
          <ErrorState message={b.error ?? "The agent run failed."} onRetry={b.reset} />
        ) : (
          <AgentTrace events={b.events} pendingActionId={b.pendingActionId} />
        )}

        {b.pendingAction && (
          <ConfirmationPanel action={b.pendingAction} onApprove={b.confirm} onReject={b.reject} />
        )}

        {!running && b.status !== "ERROR" && (
          <div className="border-t border-hairline pt-section">
            <QueryInput
              onSubmit={b.submit}
              busy={b.submitting}
              placeholder="Give the agent another task…"
            />
          </div>
        )}
      </main>
    </div>
  );
}
