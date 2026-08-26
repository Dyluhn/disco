import { AlertTriangle, ChevronDown, Loader2, Plus, ShieldCheck, Workflow } from "lucide-react";
import { useId, useState } from "react";
import { WorkflowAuthorForm } from "@/components/workflows/WorkflowAuthorForm";
import {
  useApproveWorkflow,
  useSetWorkflowEnabled,
  useWorkflowReviews,
} from "@/hooks/useWorkflows";
import type { WorkflowReview } from "@/types/workflow";
import { WorkflowRunSchedule } from "./workflowReview/WorkflowRunSchedule";
import { WorkflowSchedule } from "./workflowReview/WorkflowSchedule";

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <section className="flex flex-col gap-hair" aria-label={label}>
      <h3 className="font-ui text-[0.78rem] font-semibold uppercase tracking-wide text-text-faint">
        {label}
      </h3>
      <pre className="max-h-[20rem] overflow-auto rounded-control border border-hairline bg-surface-2 p-inline font-mono text-[0.72rem] leading-relaxed text-text-muted">
        {JSON.stringify(value, null, 2)}
      </pre>
    </section>
  );
}

function FindingList({ workflow }: { workflow: WorkflowReview }) {
  if (workflow.validation_findings.length === 0) {
    return (
      <div className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
        <ShieldCheck className="size-4 text-supported" aria-hidden />
        No validation findings.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-hair">
      {workflow.validation_findings.map((finding, index) => (
        <li
          key={`${finding.code}-${finding.path}-${index}`}
          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem]"
          data-severity={finding.severity}
        >
          <span
            className={
              finding.severity === "error"
                ? "font-semibold text-unsupported"
                : "font-semibold text-warn"
            }
          >
            {finding.severity}
          </span>
          <span className="text-text-muted"> · {finding.code}</span>
          <div className="text-text">{finding.message}</div>
          <div className="font-mono text-[0.7rem] text-text-faint">{finding.path}</div>
        </li>
      ))}
    </ul>
  );
}

type WorkflowState = "ready" | "needs_setup" | "off";

function workflowState(workflow: WorkflowReview): WorkflowState {
  return workflow.readiness.status;
}

function workflowStateLabel(state: WorkflowState): string {
  if (state === "ready") return "Ready";
  if (state === "off") return "Off";
  return "Needs setup";
}

function WorkflowCard({
  workflow,
  approving,
  toggling,
  onApprove,
  onToggle,
}: {
  workflow: WorkflowReview;
  approving: boolean;
  toggling: boolean;
  onApprove: (workflow: WorkflowReview) => void;
  onToggle: (workflow: WorkflowReview) => void;
}) {
  const hasError = workflow.validation_findings.some((finding) => finding.severity === "error");
  const state = workflowState(workflow);
  const status = workflowStateLabel(state);
  const advancedId = useId();
  const [advancedOpen, setAdvancedOpen] = useState(false);

  return (
    <article
      className="flex flex-col gap-section rounded-card border border-hairline bg-surface-1 p-body"
      data-workflow-state={state}
    >
      <header className="flex flex-col gap-inline md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-inline">
            <Workflow className="size-4 shrink-0 text-accent" aria-hidden />
            <h2 className="truncate font-ui text-[1rem] font-semibold text-text">
              {workflow.name}
            </h2>
            <span
              className="rounded-full border border-hairline px-inline py-px font-ui text-[0.68rem] uppercase tracking-wide text-text-muted"
              data-status={state}
            >
              {status}
            </span>
          </div>
          <p className="mt-hair font-ui text-[0.84rem] text-text-muted">{workflow.card}</p>
        </div>
        <div className="flex shrink-0 items-center gap-inline">
          {!workflow.approved && (
            <button
              type="button"
              onClick={() => onApprove(workflow)}
              disabled={hasError || approving}
              data-validation-blocked={hasError}
              aria-label={`Set up ${workflow.name}`}
              className="inline-flex min-h-11 items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-supported/50 hover:text-supported disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
            >
              {approving && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
              Set up
            </button>
          )}
          <button
            type="button"
            role="switch"
            aria-checked={workflow.enabled}
            aria-label={`Enable ${workflow.name}`}
            onClick={() => onToggle(workflow)}
            disabled={
              toggling || (!workflow.enabled && (!workflow.approved || hasError))
            }
            className="inline-flex min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.76rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
          >
            <span
              aria-hidden
              className={`relative h-4 w-7 rounded-full transition-colors ${workflow.enabled ? "bg-accent" : "bg-surface-3"}`}
            >
              <span
                className={`absolute top-0.5 size-3 rounded-full bg-surface-1 transition-transform ${workflow.enabled ? "translate-x-3.5" : "translate-x-0.5"}`}
              />
            </span>
            {workflow.enabled ? "Enabled" : "Off"}
          </button>
        </div>
      </header>

      {state === "ready" && <WorkflowRunSchedule workflow={workflow} />}

      {/* The workflow library is the default view. Power users still have the
          complete sealed surface, but it is deliberately behind one explicit
          disclosure so JSON never competes with the Run action. */}
      <div className="rounded-control border border-hairline bg-surface-2">
        <button
          type="button"
          aria-expanded={advancedOpen}
          aria-controls={advancedId}
          onClick={() => setAdvancedOpen((open) => !open)}
          className="group flex min-h-11 w-full items-center gap-hair px-inline py-hair text-left font-ui text-[0.78rem] text-text-muted hover:text-text lg:min-h-0"
        >
          <ChevronDown className="size-3.5 shrink-0 text-text-faint transition-transform group-aria-expanded:rotate-180" aria-hidden />
          <span>Advanced details</span>
          <span className="text-text-faint">Sealed Agent capability surface</span>
        </button>
        {advancedOpen && (
          <div id={advancedId} className="flex flex-col gap-section border-t border-hairline p-inline">
            <section className="flex flex-col gap-inline" aria-label={`${workflow.name} identity`}>
              <h3 className="font-ui text-[0.78rem] font-semibold uppercase tracking-wide text-text-faint">
                Instance identity
              </h3>
              <div className="grid gap-hair font-mono text-[0.7rem] text-text-muted md:grid-cols-2">
                <span>Instance ID: {workflow.instance_id}</span>
                <span>Definition digest: {workflow.definition_digest}</span>
                <span>Surface digest: {workflow.surface_shown_digest}</span>
              </div>
            </section>
            <section className="flex flex-col gap-inline" aria-label={`${workflow.name} findings`}>
              <h3 className="font-ui text-[0.86rem] font-semibold text-text">Validation findings</h3>
              <FindingList workflow={workflow} />
            </section>
            {state === "ready" && <WorkflowSchedule workflow={workflow} />}
            <div className="grid gap-section xl:grid-cols-2">
              <JsonBlock
                label={`${workflow.name} tool surface`}
                value={{
                  allowed_tools: workflow.compiled_surface.allowed_tools,
                  advertised_tools: workflow.compiled_surface.advertised_tools,
                  tool_definitions: workflow.compiled_surface.tool_definitions,
                }}
              />
              <JsonBlock label={`${workflow.name} MCP mounts`} value={workflow.compiled_surface.mcp_mounts} />
              <JsonBlock label={`${workflow.name} skills`} value={workflow.compiled_surface.skills} />
              <JsonBlock label={`${workflow.name} policies`} value={workflow.compiled_surface.policies} />
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

function Skeleton() {
  return (
    <div aria-hidden className="flex flex-col gap-inline">
      {[0, 1].map((i) => (
        <div key={i} className="rounded-card border border-hairline bg-surface-1 p-body">
          <div className="h-4 w-1/3 animate-pulse rounded-full bg-surface-2" />
          <div className="mt-inline h-3 w-2/3 animate-pulse rounded-full bg-surface-2" />
          <div className="mt-section h-32 animate-pulse rounded-control bg-surface-2" />
        </div>
      ))}
    </div>
  );
}

export function WorkflowReviewPanel() {
  const { data, isLoading, isError, refetch } = useWorkflowReviews();
  const approve = useApproveWorkflow();
  const setEnabled = useSetWorkflowEnabled();
  const [creating, setCreating] = useState(false);

  return (
    <div className="mx-auto w-full max-w-doc px-body py-section">
      <div className="mx-auto flex w-full max-w-[58rem] flex-col gap-section">
        <header className="flex flex-col gap-inline md:flex-row md:items-start md:justify-between">
          <div>
            <h1 className="font-display text-[2rem] tracking-tight text-text">Workflows</h1>
            <p className="font-ui text-[0.88rem] text-text-muted">
              Reusable capabilities your Agent can use in any chat.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setCreating((value) => !value)}
            className="inline-flex min-h-11 w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text lg:min-h-0"
          >
            <Plus className="size-4" aria-hidden />
            New workflow
          </button>
        </header>

        {creating && <WorkflowAuthorForm />}

        {isLoading && <Skeleton />}

        {isError && (
          <div
            role="alert"
            className="flex flex-col items-center gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body text-center"
          >
            <AlertTriangle className="size-5 text-warn" aria-hidden />
            <div className="font-ui text-[0.88rem] text-text">Couldn't load workflows.</div>
            <button
              type="button"
              onClick={() => refetch()}
              className="min-h-11 rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text lg:min-h-0"
            >
              Try again
            </button>
          </div>
        )}

        {(approve.isError || setEnabled.isError) && (
          <div role="alert" className="rounded-control border border-unsupported/50 bg-surface-1 p-inline font-ui text-[0.82rem] text-unsupported">
            Workflow update failed.
          </div>
        )}

        {!isLoading && !isError && (data?.workflows ?? []).length === 0 && (
          <div className="rounded-card border border-hairline bg-surface-1 p-major text-center font-ui text-[0.88rem] text-text-muted">
            No workflow instances.
          </div>
        )}

        {!isLoading && !isError &&
          ([
            ["ready", "Available to your Agent"],
            ["needs_setup", "Needs setup"],
            ["off", "Off"],
          ] as const).map(([groupState, groupTitle]) => {
            const workflows = (data?.workflows ?? []).filter(
              (workflow) => workflowState(workflow) === groupState,
            );
            if (workflows.length === 0) return null;
            return (
              <section key={groupState} aria-labelledby={`workflow-group-${groupState}`} className="flex flex-col gap-inline">
                <h2 id={`workflow-group-${groupState}`} className="font-ui text-[0.82rem] font-semibold uppercase tracking-wide text-text-faint">
                  {groupTitle}
                </h2>
                {workflows.map((workflow) => (
                  <WorkflowCard
                    key={workflow.instance_id}
                    workflow={workflow}
                    approving={approve.isPending && approve.variables?.instanceId === workflow.instance_id}
                    toggling={setEnabled.isPending && setEnabled.variables?.instanceId === workflow.instance_id}
                    onApprove={(item) =>
                      approve.mutate({
                        instanceId: item.instance_id,
                        surfaceShownDigest: item.surface_shown_digest,
                      })
                    }
                    onToggle={(item) =>
                      setEnabled.mutate({
                        instanceId: item.instance_id,
                        enabled: !item.enabled,
                      })
                    }
                  />
                ))}
              </section>
            );
          })}
      </div>
    </div>
  );
}
