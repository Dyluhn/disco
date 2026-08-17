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
import { Plus } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createSchedule,
  deleteSchedule,
  listSchedules,
  previewSchedule,
} from "@/api/schedules";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import { parseScheduleNL } from "@/lib/scheduleNL";
import { EMPTY_DRAFT, type DraftState } from "./scheduleSectionParts/draftState";
import type { PreviewResult, ScheduleRow } from "./scheduleSectionParts/types";
import { ScheduleComposer } from "./scheduleSectionParts/ScheduleComposer";
import { ScheduleList } from "./scheduleSectionParts/ScheduleList";

// ---- schedule presets -------------------------------------------------------

/** Known-good cron presets. Exported so tests can verify each cron is valid. */
export const SCHEDULE_PRESETS = [
  { label: "Daily 9am",     cron: "0 9 * * *",   description: "daily at 9:00 AM" },
  { label: "Weekdays 9am",  cron: "0 9 * * 1-5", description: "every weekday at 9:00 AM" },
  { label: "Weekly Mon 9am", cron: "0 9 * * 1",  description: "every Monday at 9:00 AM" },
  { label: "Hourly",        cron: "0 * * * *",   description: "every hour" },
  { label: "Every 6 hours", cron: "0 */6 * * *", description: "every 6 hours" },
] as const;

// ---- main component ---------------------------------------------------------

export function ScheduleSection({ conversationId }: { conversationId: string }) {
  const timezone = browserScheduleTimezone();
  const qc = useQueryClient();
  const key = ["schedules", conversationId];

  const { data: schedules, isLoading } = useQuery<ScheduleRow[]>({
    queryKey: key,
    queryFn: () => listSchedules(conversationId),
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => deleteSchedule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  const createMutation = useMutation({
    mutationFn: (payload: { rrule: string; description: string; timezone: string }) =>
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

  const handleInputChange = (value: string) => {
    setDraft({ input: value, description: null });
    setParseError(null);
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
      const result = await previewSchedule(parsed.rrule!, timezone, 3);
      setDraft((current) => ({ ...current, description: parsed.description }));
      setPreview({ ...result, rrule: parsed.rrule!, timezone });
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : "Preview failed");
      setPreview(null);
    } finally {
      setPreviewLoading(false);
    }
  };

  /** Select a known-good preset: set the input and immediately fetch a preview. */
  const handleSelectPreset = async (cron: string, description: string) => {
    setDraft({ input: cron, description });
    setParseError(null);
    setPreviewError(null);
    setPreviewLoading(true);
    setPreview(null);
    try {
      const result = await previewSchedule(cron, timezone, 3);
      setPreview({ ...result, rrule: cron, timezone });
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
    createMutation.mutate({
      rrule: parsed.rrule,
      description: draft.description || parsed.description,
      timezone,
    });
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

      <ScheduleComposer
        creating={creating}
        preview={preview}
        draft={draft}
        parseError={parseError}
        previewError={previewError}
        previewLoading={previewLoading}
        busy={createMutation.isPending}
        presets={SCHEDULE_PRESETS}
        onPreview={handlePreview}
        onSelectPreset={handleSelectPreset}
        onConfirm={handleConfirm}
        onCancel={closeForm}
        onInputChange={handleInputChange}
      />

      <ScheduleList
        isLoading={isLoading}
        schedules={schedules}
        creating={creating}
        onDelete={(scheduleId) => removeMutation.mutate(scheduleId)}
      />
    </section>
  );
}
