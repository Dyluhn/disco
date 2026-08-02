import { AlertTriangle, ArrowRight, Loader2, Pencil } from "lucide-react";
import type { FormEvent } from "react";
import { errorMessage } from "./draftMapping";

interface DescribeStepProps {
  description: string;
  onDescriptionChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  canDraft: boolean;
  isDrafting: boolean;
  draftIsError: boolean;
  draftError: unknown;
  advancedOpen: boolean;
  hasResult: boolean;
  onOpenAdvanced: () => void;
}

export function DescribeStep({
  description,
  onDescriptionChange,
  onSubmit,
  canDraft,
  isDrafting,
  draftIsError,
  draftError,
  advancedOpen,
  hasResult,
  onOpenAdvanced,
}: DescribeStepProps) {
  return (
    <form className="flex flex-col gap-inline" onSubmit={onSubmit}>
      <div>
        <h2 className="font-ui text-[1rem] font-semibold text-text">Create workflow</h2>
      </div>
      <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
        What should this workflow do?
        <textarea
          aria-label="What should this workflow do?"
          value={description}
          onChange={(event) => onDescriptionChange(event.target.value)}
          rows={5}
          placeholder="Every morning, pull my unread emails, summarize them into 5 bullets, and save a markdown brief."
          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
        />
      </label>

      {draftIsError && (
        <div
          role="alert"
          className="flex items-start gap-hair rounded-control border border-unsupported/50 bg-surface-2 p-inline font-ui text-[0.82rem] text-unsupported"
        >
          <AlertTriangle className="size-4 shrink-0" aria-hidden />
          {errorMessage(draftError, "Workflow drafting failed.")}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-hair">
        <button
          type="submit"
          disabled={!canDraft}
          className="inline-flex w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isDrafting ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <ArrowRight className="size-3.5" aria-hidden />
          )}
          Draft it →
        </button>
        {!advancedOpen && !hasResult && (
          <button
            type="button"
            onClick={onOpenAdvanced}
            className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
          >
            <Pencil className="size-3.5" aria-hidden />
            Edit details (Advanced)
          </button>
        )}
      </div>
    </form>
  );
}
