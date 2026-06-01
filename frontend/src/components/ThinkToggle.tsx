import { Brain } from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * The "Think" toggle (Prompt 3C): a per-conversation reasoning-effort control
 * (off / on). It IS wired — the flag is passed to the backend on submit — but the
 * backend's full handling is DEFERRED (its effect depends on the chosen models),
 * so it must not pretend to do more than it does. The title makes that honest.
 * Quiet styling; chroma only when on (it becomes an actionable, engaged control).
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
      aria-label="Think — extra reasoning effort (effect depends on the chosen model; backend handling in progress)"
      title="Extra reasoning effort. Wired to the backend; full handling depends on the chosen model and is still in progress."
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
