import { CalendarClock, Check, Loader2, Play } from "lucide-react";
import { Link } from "react-router-dom";
import { useState } from "react";
import { useToast } from "@/components/toastApi";
import { useRunWorkflow, useScheduleWorkflow, useWorkflowSchedules } from "@/hooks/useWorkflows";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import type { WorkflowReview } from "@/types/workflow";
import { WorkflowScheduleList } from "./WorkflowScheduleList";

export function WorkflowRunSchedule({ workflow }: { workflow: WorkflowReview }) {
  const run = useRunWorkflow();
  const schedule = useScheduleWorkflow();
  const schedules = useWorkflowSchedules();
  const toast = useToast();
  const [cron, setCron] = useState("* * * * *");
  const [lastConversationId, setLastConversationId] = useState<string | null>(null);
  const rows = (schedules.data ?? []).filter((row) => row.spec.instance_id === workflow.instance_id);
  const alreadySaved = rows.some((row) => row.spec.instance_digest === workflow.definition_digest
    && row.spec.cron === cron.trim() && row.spec.timezone === browserScheduleTimezone());

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
    if (alreadySaved || schedulePending) return;
    schedule.mutate(
      {
        instanceId: workflow.instance_id,
        instanceDigest: workflow.definition_digest,
        cron: cron.trim(),
        timezone: browserScheduleTimezone(),
      },
      {
        onSuccess: () => {
          toast.show({ title: "Workflow schedule saved", body: cron.trim() });
        },
      },
    );
  }

  return (
    <section className="grid gap-inline rounded-control border border-hairline bg-surface-2 p-inline md:grid-cols-[auto_1fr]">
      <div className="flex flex-wrap items-center gap-hair">
        <button
          type="button"
          onClick={handleRun}
          disabled={runPending}
          className="inline-flex min-h-11 items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
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
            }}
            className="min-h-11 w-[9rem] rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.76rem] text-text outline-none focus:border-accent lg:min-h-0"
          />
        </label>
        <button
          type="button"
          onClick={handleSchedule}
          disabled={schedulePending || schedules.isPending || alreadySaved || !cron.trim()}
          className="inline-flex min-h-11 items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
        >
          {schedulePending ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <Check className="size-3.5" aria-hidden />
          )}
          Save
        </button>
        {alreadySaved && <span className="font-ui text-[0.76rem] text-supported">Saved</span>}
      </div>
      <div className="md:col-span-2">
        {schedules.isPending && <p role="status">Loading schedules…</p>}
        {schedules.isError && <p role="alert">Could not load schedules. <button type="button" onClick={() => void schedules.refetch()}>Retry</button></p>}
        <WorkflowScheduleList rows={rows} />
      </div>
      {(run.isError || schedule.isError) && (
        <div role="alert" className="md:col-span-2 font-ui text-[0.78rem] text-unsupported">
          Workflow action failed.
        </div>
      )}
    </section>
  );
}
