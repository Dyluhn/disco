/**
 * The enable/disable toggle row for RoleFallbackSection — pulled out of the
 * component purely to shed cyclomatic complexity (TS-0034).
 */

import { RotateCcw } from "lucide-react";
import { cn } from "@/lib/cn";

export function FallbackToggle({
  enabled,
  pending,
  onChange,
}: {
  enabled: boolean;
  pending: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <label className="flex min-h-11 cursor-pointer items-start gap-inline lg:min-h-0">
      <input
        type="checkbox"
        checked={enabled}
        disabled={pending}
        onChange={(event) => onChange(event.target.checked)}
        className="peer sr-only"
        data-disco-control="settings.role-fallback-toggle"
      />
      <span
        className={cn(
          "mt-px flex h-5 w-9 shrink-0 items-center rounded-full border p-[2px] transition-colors",
          enabled
            ? "border-accent/60 bg-accent/70"
            : "border-hairline-strong bg-surface-2",
          pending && "opacity-60",
        )}
        aria-hidden
      >
        <span
          className={cn(
            "size-3.5 rounded-full bg-bg shadow-sm transition-transform",
            enabled && "translate-x-4",
          )}
        />
      </span>
      <span className="flex min-w-0 flex-col gap-hair">
        <span className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
          <RotateCcw className="size-3.5 text-accent" aria-hidden />
          Fall back to a local model for auxiliary roles
        </span>
        <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
          Only summarizer, query-rewriter and judge roles fall back. The
          agent driver and the grounded answer never do.
        </span>
      </span>
    </label>
  );
}
