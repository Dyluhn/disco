/**
 * Card shown BEFORE saving, displaying next 3 run times for user confirmation.
 * Relocated out of ScheduleSection.tsx (still a private, non-exported
 * implementation detail) so ScheduleComposer can render it.
 */

import { CalendarClock } from "lucide-react";
import { fmtDatetime } from "./fmtDatetime";
import type { PreviewResult } from "./types";

export function ConfirmCard({
  preview,
  description,
  onConfirm,
  onCancel,
  busy,
}: {
  preview: PreviewResult;
  description: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  return (
    <div className="flex flex-col gap-inline rounded-card border border-accent/40 bg-surface-1 p-body">
      <div className="flex items-center gap-hair">
        <CalendarClock className="size-4 text-accent" aria-hidden />
        <span className="font-ui text-[0.9rem] font-semibold text-text">Confirm schedule</span>
      </div>
      <p className="font-ui text-[0.84rem] text-text-muted">
        <span className="font-medium text-text">{description}</span>
        {" — "}cron: <code className="rounded bg-surface-2 px-1 font-mono text-[0.78rem]">{preview.rrule}</code>
      </p>
      <div className="flex flex-col gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-inline">
        <p className="font-ui text-[0.78rem] font-medium text-text-muted uppercase tracking-wide">
          Next 3 runs
        </p>
        {/* `data-next-run` carries the raw ISO so a frozen-clock test can assert the
            previewed times deterministically (the visible text is locale-formatted). */}
        <ol className="list-decimal list-inside space-y-px">
          {preview.next_runs.map((t) => (
            <li key={t} data-next-run={t} className="font-ui text-[0.82rem] text-text">
              {fmtDatetime(t, preview.timezone)}
            </li>
          ))}
        </ol>
      </div>
      <p className="font-ui text-[0.78rem] text-text-muted">
        Time zone: <span className="font-medium text-text">{preview.timezone}</span>
      </p>
      <div className="flex items-center justify-end gap-inline">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
        >
          Cancel
        </button>
        <button
          type="button"
          data-disco-control="settings.schedule-save"
          disabled={busy}
          onClick={onConfirm}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save schedule"}
        </button>
      </div>
    </div>
  );
}
