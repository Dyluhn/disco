/**
 * RP-08 — Schedule section for conversation settings.
 *
 * Lets the user create a cron-style recurring schedule for a conversation.
 * The confirm card shows the next 3 run times before the schedule is saved
 * (requirement: never save without showing the user what they're committing to).
 *
 * Mirror of SkillsSection: same card/button/error styling.
 */

import { useState } from "react";
import { CalendarClock, Plus, Trash2 } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { agentFetch } from "@/api/client";
import { parseScheduleNL } from "@/lib/scheduleNL";

// ---- API functions ----------------------------------------------------------

interface ScheduleRow {
  schedule_id: string;
  conversation_id: string;
  rrule: string;
  description: string;
  depth: string | null;
  model_override: string | null;
  created_at: string;
  enabled: boolean;
  next_run: string | null;
}

interface PreviewResult {
  next_runs: string[];
  rrule: string;
}

async function fetchSchedules(cid: string): Promise<ScheduleRow[]> {
  const r = await agentFetch(`/api/conversations/${encodeURIComponent(cid)}/schedules`);
  if (!r.ok) throw new Error(`Failed to load schedules: ${r.status}`);
  const body = await r.json();
  return body.schedules ?? [];
}

async function createSchedule(
  cid: string,
  payload: { rrule: string; description: string; depth?: string; model_override?: string },
): Promise<ScheduleRow> {
  const r = await agentFetch(`/api/conversations/${encodeURIComponent(cid)}/schedules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err?.detail?.reason ?? `Error ${r.status}`);
  }
  return r.json();
}

async function deleteSchedule(scheduleId: string): Promise<void> {
  const r = await agentFetch(`/api/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(`Delete failed: ${r.status}`);
}

async function previewSchedule(rrule: string, n = 3): Promise<PreviewResult> {
  const r = await agentFetch(`/api/schedules/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rrule, n }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err?.detail?.reason ?? `Error ${r.status}`);
  }
  return r.json();
}

// ---- schedule presets -------------------------------------------------------

/** Known-good cron presets. Exported so tests can verify each cron is valid. */
export const SCHEDULE_PRESETS = [
  { label: "Daily 9am",     cron: "0 9 * * *",   description: "daily at 9:00 AM" },
  { label: "Weekdays 9am",  cron: "0 9 * * 1-5", description: "every weekday at 9:00 AM" },
  { label: "Weekly Mon 9am", cron: "0 9 * * 1",  description: "every Monday at 9:00 AM" },
  { label: "Hourly",        cron: "0 * * * *",   description: "every hour" },
  { label: "Every 6 hours", cron: "0 */6 * * *", description: "every 6 hours" },
] as const;

// ---- sub-components ---------------------------------------------------------

function fmtDatetime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", timeZoneName: "short",
    });
  } catch {
    return iso;
  }
}

/** Card shown BEFORE saving, displaying next 3 run times for user confirmation. */
function ConfirmCard({
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
              {fmtDatetime(t)}
            </li>
          ))}
        </ol>
      </div>
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

// ---- main component ---------------------------------------------------------

interface DraftState {
  input: string;  // raw NL input from the user
}

const EMPTY_DRAFT: DraftState = { input: "" };

export function ScheduleSection({ conversationId }: { conversationId: string }) {
  const qc = useQueryClient();
  const key = ["schedules", conversationId];

  const { data: schedules, isLoading } = useQuery<ScheduleRow[]>({
    queryKey: key,
    queryFn: () => fetchSchedules(conversationId),
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => deleteSchedule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  const createMutation = useMutation({
    mutationFn: (payload: { rrule: string; description: string }) =>
      createSchedule(conversationId, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setDraft(EMPTY_DRAFT);
      setParseError(null);
      setPreview(null);
    },
  });

  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<DraftState>(EMPTY_DRAFT);
  const [parseError, setParseError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const closeForm = () => {
    setCreating(false);
    setDraft(EMPTY_DRAFT);
    setParseError(null);
    setPreview(null);
    setPreviewError(null);
  };

  const handlePreview = async () => {
    const parsed = parseScheduleNL(draft.input);
    if (!parsed.success) {
      setParseError(parsed.error);
      setPreview(null);
      return;
    }
    setParseError(null);
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const result = await previewSchedule(parsed.rrule!, 3);
      setPreview({ ...result, rrule: parsed.rrule!, });
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : "Preview failed");
      setPreview(null);
    } finally {
      setPreviewLoading(false);
    }
  };

  /** Select a known-good preset: set the input and immediately fetch a preview. */
  const handleSelectPreset = async (cron: string) => {
    setDraft({ input: cron });
    setParseError(null);
    setPreviewError(null);
    setPreviewLoading(true);
    setPreview(null);
    try {
      const result = await previewSchedule(cron, 3);
      setPreview({ ...result, rrule: cron });
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleConfirm = () => {
    if (!preview) return;
    const parsed = parseScheduleNL(draft.input);
    if (!parsed.success || !parsed.rrule) return;
    createMutation.mutate({ rrule: parsed.rrule, description: parsed.description });
  };

  const anyError = removeMutation.error || createMutation.error;

  return (
    <section aria-labelledby="schedule-heading" className="flex flex-col gap-inline">
      <div className="flex items-center justify-between gap-inline">
        <h2 id="schedule-heading" className="font-ui text-[1.05rem] font-semibold text-text">
          Schedules
        </h2>
        {!creating && (
          <button
            type="button"
            data-disco-control="settings.schedule-new"
            onClick={() => setCreating(true)}
            className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text"
          >
            <Plus className="size-3.5" aria-hidden />
            New schedule
          </button>
        )}
      </div>

      <p className="font-ui text-[0.84rem] text-text-muted">
        Re-run this conversation automatically on a cron-style schedule. Each
        run appends to the same conversation so you can track results over time.
      </p>

      {anyError && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          {anyError instanceof Error ? anyError.message : "Something went wrong."}
        </p>
      )}

      {creating && preview && (
        <ConfirmCard
          preview={preview}
          description={parseScheduleNL(draft.input).description || draft.input}
          onConfirm={handleConfirm}
          onCancel={closeForm}
          busy={createMutation.isPending}
        />
      )}

      {creating && !preview && (
        <div className="flex flex-col gap-inline rounded-card border border-accent/40 bg-surface-1 p-body">
          {/* Quick-pick presets — emit known-good cron directly */}
          <div className="flex flex-wrap gap-hair">
            {SCHEDULE_PRESETS.map(({ label, cron }) => (
              <button
                key={cron}
                type="button"
                data-disco-control="settings.schedule-preset"
                data-cron={cron}
                disabled={previewLoading}
                onClick={() => handleSelectPreset(cron)}
                className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text disabled:opacity-40"
              >
                {label}
              </button>
            ))}
          </div>
          <p className="font-ui text-[0.74rem] text-text-faint">
            Or type your own:
          </p>
          <input
            data-disco-control="settings.schedule-input"
            value={draft.input}
            onChange={(e) => {
              setDraft({ input: e.target.value });
              setParseError(null);
              setPreviewError(null);
            }}
            placeholder='e.g. "every day at 9am" or "*/5 * * * *"'
            aria-label="Schedule expression"
            className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.88rem] text-text outline-none focus:border-accent"
          />
          {parseError && (
            <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
              {parseError}
            </p>
          )}
          {previewError && (
            <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
              {previewError}
            </p>
          )}
          <div className="flex items-center justify-end gap-inline">
            <button
              type="button"
              onClick={closeForm}
              className="rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
            >
              Cancel
            </button>
            <button
              type="button"
              data-disco-control="settings.schedule-preview"
              disabled={!draft.input.trim() || previewLoading}
              onClick={handlePreview}
              className="rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40"
            >
              {previewLoading ? "Checking…" : "Preview schedule"}
            </button>
          </div>
        </div>
      )}

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
              {s.next_run && (
                <p className="font-ui text-[0.78rem] text-text-muted">
                  Next: {fmtDatetime(s.next_run)}
                </p>
              )}
            </div>
            <button
              type="button"
              data-disco-control="settings.schedule-delete"
              onClick={() => removeMutation.mutate(s.schedule_id)}
              aria-label={`Delete schedule ${s.description}`}
              className="text-text-faint transition-colors hover:text-unsupported"
            >
              <Trash2 className="size-3.5" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
