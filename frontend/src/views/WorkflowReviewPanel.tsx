import { AlertTriangle, CalendarClock, Check, Loader2, Play, Plus, ShieldCheck, Workflow } from "lucide-react";
import { Link } from "react-router-dom";
import { useState } from "react";
import { useToast } from "@/components/toastApi";
import { WorkflowAuthorForm } from "@/components/workflows/WorkflowAuthorForm";
import {
  useApproveWorkflow,
  useRunWorkflow,
  useScheduleWorkflow,
  useWorkflowReviews,
} from "@/hooks/useWorkflows";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import type { WorkflowReview } from "@/types/workflow";

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

function WorkflowCard({
  workflow,
  approving,
  onApprove,
}: {
  workflow: WorkflowReview;
  approving: boolean;
  onApprove: (workflow: WorkflowReview) => void;
}) {
  const hasError = workflow.validation_findings.some((finding) => finding.severity === "error");
  const status = workflow.approved ? "Approved" : hasError ? "Blocked" : "Draft";
  const run = useRunWorkflow();
  const schedule = useScheduleWorkflow();
  const toast = useToast();
  const [cron, setCron] = useState("* * * * *");
  const [lastConversationId, setLastConversationId] = useState<string | null>(null);
  const [scheduleSaved, setScheduleSaved] = useState(false);

  const runPending = run.isPending && run.variables === workflow.instance_id;
  const schedulePending =
    schedule.isPending && schedule.variables?.instanceId === workflow.instance_id;

  function handleRun() {
    run.mutate(workflow.instance_id, {
      onSuccess: (result) => {
        setLastConversationId(result.conversation_id);
        toast.show({
          title: "Workflow run started",
          body: result.conversation_id,
        });
      },
    });
  }

  function handleSchedule() {
    schedule.mutate(
      {
        instanceId: workflow.instance_id,
        instanceDigest: workflow.definition_digest,
        cron: cron.trim(),
        timezone: browserScheduleTimezone(),
      },
      {
        onSuccess: () => {
          setScheduleSaved(true);
          toast.show({ title: "Workflow schedule saved", body: cron.trim() });
        },
      },
    );
  }
  return (
    <article
      className="flex flex-col gap-section rounded-card border border-hairline bg-surface-1 p-body"
      data-workflow-id={workflow.instance_id}
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
              data-status={status.toLowerCase()}
            >
              {status}
            </span>
          </div>
          <p className="mt-hair font-ui text-[0.84rem] text-text-muted">{workflow.card}</p>
          <div className="mt-hair font-mono text-[0.68rem] text-text-faint">
            {workflow.instance_id} · {workflow.surface_shown_digest}
          </div>
        </div>
        <button
          type="button"
          onClick={() => onApprove(workflow)}
          disabled={workflow.approved || hasError || approving}
          data-validation-blocked={hasError}
          aria-label={`Approve ${workflow.name}`}
          className="inline-flex shrink-0 items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-supported/50 hover:text-supported disabled:cursor-not-allowed disabled:opacity-50"
        >
          {approving ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <Check className="size-3.5" aria-hidden />
          )}
          {workflow.approved ? "Approved" : "Approve"}
        </button>
      </header>

      {workflow.approved && (
        <section className="grid gap-inline rounded-control border border-hairline bg-surface-2 p-inline md:grid-cols-[auto_1fr]">
          <div className="flex flex-wrap items-center gap-hair">
            <button
              type="button"
              onClick={handleRun}
              disabled={runPending}
              className="inline-flex items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
            >
              {runPending ? (
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
              ) : (
                <Play className="size-3.5" aria-hidden />
              )}
              Run
            </button>
            {lastConversationId && (
              <Link
                to={`/agent/${encodeURIComponent(lastConversationId)}`}
                className="font-ui text-[0.78rem] text-accent hover:underline"
              >
                {lastConversationId}
              </Link>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-hair md:justify-end">
            <label className="flex items-center gap-hair font-ui text-[0.78rem] text-text-muted">
              <CalendarClock className="size-3.5" aria-hidden />
              <input
                aria-label={`Cron schedule for ${workflow.name}`}
                value={cron}
                onChange={(event) => {
                  setCron(event.target.value);
                  setScheduleSaved(false);
                }}
                className="w-[9rem] rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.76rem] text-text outline-none focus:border-accent"
              />
            </label>
            <button
              type="button"
              onClick={handleSchedule}
              disabled={schedulePending || !cron.trim()}
              className="inline-flex items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
            >
              {schedulePending ? (
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
              ) : (
                <Check className="size-3.5" aria-hidden />
              )}
              Save
            </button>
            {scheduleSaved && (
              <span className="font-ui text-[0.76rem] text-supported">Saved</span>
            )}
          </div>
          {(run.isError || schedule.isError) && (
            <div role="alert" className="md:col-span-2 font-ui text-[0.78rem] text-unsupported">
              Workflow action failed.
            </div>
          )}
        </section>
      )}

      <section className="flex flex-col gap-inline" aria-label={`${workflow.name} findings`}>
        <h3 className="font-ui text-[0.86rem] font-semibold text-text">Validation Findings</h3>
        <FindingList workflow={workflow} />
      </section>

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
  const [creating, setCreating] = useState(false);

  return (
    <div className="mx-auto w-full max-w-doc px-body py-section">
      <div className="mx-auto flex w-full max-w-[58rem] flex-col gap-section">
        <header className="flex flex-col gap-inline md:flex-row md:items-start md:justify-between">
          <div>
            <h1 className="font-display text-[2rem] tracking-tight text-text">Workflow Instances</h1>
            <p className="font-ui text-[0.88rem] text-text-muted">
              All workflow instances, with their sealed tool surface, validation findings, and approval state.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setCreating((value) => !value)}
            className="inline-flex w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text"
          >
            <Plus className="size-4" aria-hidden />
            Create workflow
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
              className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
            >
              Try again
            </button>
          </div>
        )}

        {approve.isError && (
          <div role="alert" className="rounded-control border border-unsupported/50 bg-surface-1 p-inline font-ui text-[0.82rem] text-unsupported">
            Approval failed.
          </div>
        )}

        {!isLoading && !isError && (data?.workflows ?? []).length === 0 && (
          <div className="rounded-card border border-hairline bg-surface-1 p-major text-center font-ui text-[0.88rem] text-text-muted">
            No workflow instances.
          </div>
        )}

        {!isLoading &&
          !isError &&
          (data?.workflows ?? []).map((workflow) => (
            <WorkflowCard
              key={workflow.instance_id}
              workflow={workflow}
              approving={approve.isPending && approve.variables?.instanceId === workflow.instance_id}
              onApprove={(item) =>
                approve.mutate({
                  instanceId: item.instance_id,
                  surfaceShownDigest: item.surface_shown_digest,
                })
              }
            />
          ))}
      </div>
    </div>
  );
}
