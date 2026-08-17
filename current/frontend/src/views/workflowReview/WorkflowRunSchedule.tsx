import { CalendarClock, Check, Loader2, Play } from "lucide-react";
import { Link } from "react-router-dom";
import { useState } from "react";
import { useToast } from "@/components/toastApi";
import { useRunWorkflow, useScheduleWorkflow } from "@/hooks/useWorkflows";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import type { WorkflowReview } from "@/types/workflow";

export function WorkflowRunSchedule({ workflow }: { workflow: WorkflowReview }) {
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
        {scheduleSaved && <span className="font-ui text-[0.76rem] text-supported">Saved</span>}
      </div>
      {(run.isError || schedule.isError) && (
        <div role="alert" className="md:col-span-2 font-ui text-[0.78rem] text-unsupported">
          Workflow action failed.
        </div>
      )}
    </section>
  );
}
