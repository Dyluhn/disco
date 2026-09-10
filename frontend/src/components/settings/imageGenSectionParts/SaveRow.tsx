import { Check, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";

export function SaveRow({
  fieldsDirty,
  savePending,
  onSave,
}: {
  fieldsDirty: boolean;
  savePending: boolean;
  onSave: () => void;
}) {
  return (
    <div className="flex items-center gap-inline">
      <button
        type="button"
        data-disco-control="settings.imagegen-save"
        disabled={!fieldsDirty || savePending}
        onClick={onSave}
        className={cn(
          "flex min-h-11 items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.8rem] transition-colors lg:min-h-0",
          fieldsDirty && !savePending
            ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
            : "border-hairline text-text-faint",
        )}
      >
        {savePending ? (
          <Loader2 className="size-3 animate-spin" aria-hidden />
        ) : (
          <Check className="size-3" aria-hidden />
        )}
        Save
      </button>
      {!fieldsDirty && !savePending && (
        <span className="font-ui text-[0.76rem] text-text-faint">Saved</span>
      )}
    </div>
  );
}
