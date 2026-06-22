import { Brain } from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * The "Think" toggle (Prompt 3C): a per-conversation reasoning-effort control
 * (off / on).
 *
 * Gap #35 — WIRED, not a false affordance. The `think` flag is threaded straight
 * into the submit payload (`ResearchSurface.submit → r.submit(query, { think })`)
 * and sent to the backend, so toggling it has a real, immediate effect on the
 * request. The only honest caveat is that the *magnitude* of the effect depends
 * on whether the chosen model exposes a reasoning-effort knob — so the label says
 * "more reasoning effort where the model supports it" rather than claiming a
 * guaranteed change. The old "backend handling in progress" wording wrongly read
 * as a stubbed/inert control; replaced with the accurate description.
 */
interface Props {
  value: boolean;
  onChange: (next: boolean) => void;
}

export function ThinkToggle({ value, onChange }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={value}
      aria-label="Think — request more reasoning effort for this conversation (where the chosen model supports it)"
      title="Request more reasoning effort. Sent to the backend on submit; how much it changes depends on whether the chosen model exposes a reasoning-effort control."
      data-disco-control="search.think-toggle"
      onClick={() => onChange(!value)}
      className={cn(
        "flex items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.76rem] transition-colors",
        value
          ? "border-accent/50 bg-surface-1 text-accent"
          : "border-hairline text-text-muted hover:text-text",
      )}
    >
      <Brain className="size-3.5 shrink-0" aria-hidden />
      Think
    </button>
  );
}
