/**
 * The Save action row for RoleFallbackSection — the button + "Saved" / error
 * indicator. Pulled out of the parent purely to shed cyclomatic complexity
 * (TS-0034); the parent still owns the draft/save state and passes it down.
 */

import { Check, Loader2, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/cn";

export function FallbackSaveRow({
  canSave,
  pending,
  dirty,
  onSave,
  error,
}: {
  canSave: boolean;
  pending: boolean;
  dirty: boolean;
  onSave: () => void;
  error: Error | null;
}) {
  return (
    <>
      <div className="flex items-center gap-inline">
        <button
          type="button"
          data-disco-control="settings.role-fallback-save"
          disabled={!canSave}
          onClick={onSave}
          className={cn(
            "flex min-h-11 items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.8rem] transition-colors lg:min-h-0",
            canSave
              ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
              : "border-hairline text-text-faint",
          )}
        >
          {pending ? (
            <Loader2 className="size-3 animate-spin" aria-hidden />
          ) : (
            <Check className="size-3" aria-hidden />
          )}
          Save
        </button>
        {!dirty && !pending && (
          <span className="flex items-center gap-hair font-ui text-[0.76rem] text-text-faint">
            <ShieldCheck className="size-3" aria-hidden />
            Saved
          </span>
        )}
      </div>
      {error && (
        <p className="font-ui text-[0.8rem] text-warn">
          Couldn't save: {error.message}
        </p>
      )}
    </>
  );
}
