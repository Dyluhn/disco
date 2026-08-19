/**
 * The "creating" flow for ScheduleSection — either the confirm-before-save
 * card (once a preview has been fetched) or the input form (presets + free
 * text). Pulled out of the parent purely to shed cyclomatic complexity
 * (TS-0036); the parent still owns all the draft/preview state and mutations.
 */

import { parseScheduleNL } from "@/lib/scheduleNL";
import type { DraftState } from "./draftState";
import type { PreviewResult } from "./types";
import { ConfirmCard } from "./ConfirmCard";

interface SchedulePreset {
  label: string;
  cron: string;
  description: string;
}

export function ScheduleComposer({
  creating,
  preview,
  draft,
  parseError,
  previewError,
  previewLoading,
  busy,
  presets,
  onPreview,
  onSelectPreset,
  onConfirm,
  onCancel,
  onInputChange,
}: {
  creating: boolean;
  preview: PreviewResult | null;
  draft: DraftState;
  parseError: string | null;
  previewError: string | null;
  previewLoading: boolean;
  busy: boolean;
  presets: readonly SchedulePreset[];
  onPreview: () => void;
  onSelectPreset: (cron: string, description: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
  onInputChange: (value: string) => void;
}) {
  if (!creating) return null;

  if (preview) {
    return (
      <ConfirmCard
        preview={preview}
        description={
          draft.description ||
          parseScheduleNL(draft.input).description ||
          draft.input
        }
        onConfirm={onConfirm}
        onCancel={onCancel}
        busy={busy}
      />
    );
  }

  return (
    <div className="flex flex-col gap-inline rounded-card border border-accent/40 bg-surface-1 p-body">
      {/* Quick-pick presets — emit known-good cron directly */}
      <div className="flex flex-wrap gap-hair">
        {presets.map(({ label, cron, description }) => (
          <button
            key={cron}
            type="button"
            data-disco-control="settings.schedule-preset"
            data-cron={cron}
            disabled={previewLoading}
            onClick={() => onSelectPreset(cron, description)}
            className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text disabled:opacity-40 lg:min-h-0"
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
        onChange={(e) => onInputChange(e.target.value)}
        placeholder='e.g. "every day at 9am" or "*/5 * * * *"'
        aria-label="Schedule expression"
        className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.88rem] text-text outline-none focus:border-accent lg:min-h-0"
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
          onClick={onCancel}
          className="min-h-11 rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text lg:min-h-0"
        >
          Cancel
        </button>
        <button
          type="button"
          data-disco-control="settings.schedule-preview"
          disabled={!draft.input.trim() || previewLoading}
          onClick={onPreview}
          className="min-h-11 rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40 lg:min-h-0"
        >
          {previewLoading ? "Checking…" : "Preview schedule"}
        </button>
      </div>
    </div>
  );
}
