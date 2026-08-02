/**
 * The Save action row for DataSourcesSection — the button + "Saved" / error
 * indicator. Pulled out of the parent purely to shed cyclomatic complexity
 * (TS-0028); the parent still owns the draft/save state and passes it down.
 */

import { Check, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";

export function DataSourcesSaveRow({
  dirty,
  pending,
  error,
  onSave,
}: {
  dirty: boolean;
  pending: boolean;
  error: Error | null;
  onSave: () => void;
}) {
  return (
    <div className="flex items-center gap-inline">
      <button
        type="button"
        data-disco-control="settings.datasource-save"
        disabled={!dirty || pending}
        onClick={onSave}
        className={cn(
          "flex items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.82rem] transition-colors",
          dirty && !pending
            ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
            : "border-hairline text-text-faint",
        )}
      >
        {pending ? (
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <Check className="size-3.5" aria-hidden />
        )}
        Save data sources
      </button>
      {!dirty && !pending && (
        <span className="font-ui text-[0.76rem] text-text-faint">
          Saved
        </span>
      )}
      {error && (
        <span className="font-ui text-[0.78rem] text-warn">
          Couldn't save: {error.message}
        </span>
      )}
    </div>
  );
}
