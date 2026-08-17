import { cn } from "@/lib/cn";

export function ToggleSwitch({
  checked,
  label,
  busy,
  onClick,
}: {
  checked: boolean;
  label: string;
  busy: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={busy}
      onClick={onClick}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:opacity-60",
        checked
          ? "border-accent/50 bg-accent/30"
          : "border-hairline bg-surface-2",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "absolute top-1/2 size-3.5 -translate-y-1/2 rounded-full transition-all",
          checked ? "left-[1.15rem] bg-accent" : "left-[0.15rem] bg-text-faint",
        )}
      />
    </button>
  );
}
