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
        // The pill itself must stay this exact size (it's a recognizable
        // on/off shape) — the mobile tap target grows via an invisible
        // ::after hit-slop instead of resizing the visible control. Gone
        // entirely at lg: (content-none), so desktop is untouched.
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors after:absolute after:-inset-x-1 after:-inset-y-3 after:content-[''] disabled:opacity-60 lg:after:content-none",
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
