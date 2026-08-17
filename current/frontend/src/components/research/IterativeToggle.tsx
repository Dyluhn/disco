/**
 * IterativeToggle — A4: a small on/off control that enables iterative grounding
 * for a new deep-research run. When ON the engine re-searches weakly-grounded
 * claims and re-checks, up to 3 rounds (slower, better-grounded).
 *
 * Sits next to DepthTierSelector / RecencySelector in the research form.
 * Controlled component: parent owns the value + onChange handler (same pattern
 * as DepthTierSelector / RecencySelector — the value flows into useDeepResearch's
 * submit() and is sent as `iterative` in the create frame).
 */

import { Check, Repeat } from "lucide-react";
import { cn } from "@/lib/cn";

interface Props {
  value: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}

export function IterativeToggle({ value, onChange, disabled }: Props) {
  return (
    <button
      type="button"
      disabled={disabled}
      aria-pressed={value}
      aria-label={`Iterative grounding: ${value ? "on" : "off"}`}
      data-disco-control="dr.iterative"
      title="Re-searches weakly-grounded claims and re-checks, up to 3 rounds — slower, better-grounded"
      onClick={() => onChange(!value)}
      className={cn(
        "flex items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.78rem] transition-colors disabled:opacity-50",
        value
          ? "border-accent bg-surface-1 text-text"
          : "border-hairline bg-surface-1 text-text-muted hover:text-text",
      )}
    >
      <Repeat
        className={cn("size-3.5", value ? "text-accent" : "text-text-faint")}
        aria-hidden
      />
      <span>Iterative grounding</span>
      <Check
        className={cn("size-3.5 shrink-0", value ? "text-accent" : "text-transparent")}
        aria-hidden
      />
    </button>
  );
}
