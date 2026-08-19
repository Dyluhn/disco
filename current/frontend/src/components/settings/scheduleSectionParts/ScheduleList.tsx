/**
 * The list of existing schedules for ScheduleSection. Pulled out of the
 * parent purely to shed cyclomatic complexity (TS-0036); the parent still
 * owns the query + delete mutation.
 */

import { CalendarClock, Trash2 } from "lucide-react";
import { fmtDatetime } from "./fmtDatetime";
import type { ScheduleRow } from "./types";

export function ScheduleList({
  isLoading,
  schedules,
  creating,
  onDelete,
}: {
  isLoading: boolean;
  schedules: ScheduleRow[] | undefined;
  creating: boolean;
  onDelete: (scheduleId: string) => void;
}) {
  return (
    <ul className="flex flex-col gap-inline">
      {isLoading && (
        <li className="rounded-card border border-hairline bg-surface-1 px-body py-body font-ui text-[0.84rem] text-text-muted">
          Loading schedules…
        </li>
      )}
      {!isLoading && (schedules ?? []).length === 0 && !creating && (
        <li className="rounded-card border border-dashed border-hairline bg-surface-1 px-body py-body text-center font-ui text-[0.84rem] text-text-faint">
          No schedules yet. Create one to automate recurring runs.
        </li>
      )}
      {(schedules ?? []).map((s) => (
        <li
          key={s.schedule_id}
          className="flex items-start justify-between gap-section rounded-card border border-hairline bg-surface-1 px-body py-inline"
        >
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-hair">
              <CalendarClock className="size-3.5 shrink-0 text-accent" aria-hidden />
              <span className="font-ui text-[0.88rem] font-medium text-text">
                {s.description}
              </span>
            </div>
            <p className="font-mono text-[0.74rem] text-text-faint">{s.rrule}</p>
            <p className="font-ui text-[0.74rem] text-text-faint">{s.timezone}</p>
            {s.next_run && (
              <p className="font-ui text-[0.78rem] text-text-muted">
                Next: {fmtDatetime(s.next_run, s.timezone)}
              </p>
            )}
          </div>
          <button
            type="button"
            data-disco-control="settings.schedule-delete"
            onClick={() => onDelete(s.schedule_id)}
            aria-label={`Delete schedule ${s.description}`}
            className="inline-flex min-h-11 min-w-11 items-center justify-center text-text-faint transition-colors hover:text-unsupported lg:min-h-0 lg:min-w-0"
          >
            <Trash2 className="size-3.5" aria-hidden />
          </button>
        </li>
      ))}
    </ul>
  );
}
