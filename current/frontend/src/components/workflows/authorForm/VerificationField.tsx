import { Plus, X } from "lucide-react";
import type { KeyboardEvent } from "react";

interface VerificationFieldProps {
  verifyChecks: string[];
  verifyDraft: string;
  onVerifyDraftChange: (value: string) => void;
  onVerifyKeyDown: (event: KeyboardEvent<HTMLInputElement>) => void;
  onAddVerifyChecks: () => void;
  onRemoveVerifyCheck: (check: string) => void;
  finalizer: string;
  onFinalizerChange: (value: string) => void;
}

export function VerificationField({
  verifyChecks,
  verifyDraft,
  onVerifyDraftChange,
  onVerifyKeyDown,
  onAddVerifyChecks,
  onRemoveVerifyCheck,
  finalizer,
  onFinalizerChange,
}: VerificationFieldProps) {
  return (
    <section className="grid gap-inline md:grid-cols-2">
      <div className="flex flex-col gap-hair">
        <label className="font-ui text-[0.8rem] text-text-muted">
          Verify checks
          <input
            value={verifyDraft}
            onChange={(event) => onVerifyDraftChange(event.target.value)}
            onKeyDown={onVerifyKeyDown}
            className="mt-hair w-full min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent lg:min-h-0"
          />
        </label>
        <button
          type="button"
          onClick={onAddVerifyChecks}
          className="inline-flex min-h-11 w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text lg:min-h-0"
        >
          <Plus className="size-3.5" aria-hidden />
          Add check
        </button>
        {verifyChecks.length > 0 && (
          <div className="flex flex-wrap gap-hair">
            {verifyChecks.map((check) => (
              <span
                key={check}
                className="inline-flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.72rem] text-text-muted"
              >
                {check}
                <button
                  type="button"
                  onClick={() => onRemoveVerifyCheck(check)}
                  aria-label={`Remove verify check ${check}`}
                  className="inline-flex min-h-11 min-w-11 items-center justify-center text-text-faint hover:text-unsupported lg:min-h-0 lg:min-w-0"
                >
                  <X className="size-3" aria-hidden />
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
      <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
        Finalizer
        <input
          value={finalizer}
          onChange={(event) => onFinalizerChange(event.target.value)}
          className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent lg:min-h-0"
        />
      </label>
    </section>
  );
}
