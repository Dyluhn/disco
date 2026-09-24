import { useSetWorkflowScheduleEnabled } from "@/hooks/useWorkflows";
import type { WorkflowSchedule } from "@/types/workflow";

export function WorkflowScheduleList({ rows }: { rows: WorkflowSchedule[] }) {
  const update = useSetWorkflowScheduleEnabled();
  if (!rows.length) return null;
  return (
    <div className="grid gap-hair font-ui text-[0.78rem] text-text-muted">
      <ul aria-label="Saved workflow schedules" className="grid gap-hair">
        {rows.map((row) => (
          <li key={row.schedule_id} className="flex flex-wrap items-center gap-inline">
            <span>{row.spec.cron} · {row.spec.timezone}</span>
            <span>{row.spec.enabled ? "Active" : "Paused"}</span>
            {row.spec.enabled && row.next_run && <span>Next: {new Date(row.next_run).toLocaleString()}</span>}
            <button
              type="button"
              disabled={update.isPending}
              onClick={() => update.mutate({ scheduleId: row.schedule_id, enabled: !row.spec.enabled })}
              className="min-h-11 rounded-control border border-hairline px-inline py-hair hover:text-text disabled:opacity-50"
            >
              {row.spec.enabled ? "Pause schedule" : "Resume schedule"}
            </button>
          </li>
        ))}
      </ul>
      <p>Pausing stops future runs. A run already in progress continues; its history is kept.</p>
      {update.isError && <p role="alert">Could not confirm schedule update. Check the displayed state before retrying.</p>}
    </div>
  );
}
