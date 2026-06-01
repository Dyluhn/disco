import { Search } from "lucide-react";
import { CodeBlocksIcon } from "@/components/icons/CodeBlocksIcon";
import { cn } from "@/lib/cn";
import { useMode } from "./mode";

/**
 * The top-level mode slider (the ONLY mode control). A segmented rounded-pill
 * toggle holding exactly two modes — Search (live) and Build (dormant/SOON). It is
 * both the indicator (the filled segment shows the active mode) and the selector.
 * Build is present but not switchable yet; it carries a SOON tag and a coming-soon
 * tooltip, and selecting it does nothing (the thumb stays on Search).
 */
export function ModeSlider() {
  const { mode, setMode, modes } = useMode();

  return (
    <div
      role="radiogroup"
      aria-label="Mode"
      className="inline-flex items-center rounded-full border border-hairline bg-surface-1 p-px"
    >
      {modes.map((m) => {
        const active = m.id === mode;
        const Icon = m.id === "search" ? Search : CodeBlocksIcon;
        return (
          <button
            key={m.id}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={m.label}
            disabled={m.dormant}
            title={m.dormant ? `${m.label} — coming soon` : undefined}
            onClick={() => setMode(m.id)}
            className={cn(
              "flex items-center gap-hair rounded-full px-body py-hair font-ui text-[0.8rem] transition-colors",
              active && "bg-surface-2 text-text",
              !active && !m.dormant && "text-text-muted hover:text-text",
              m.dormant && "cursor-not-allowed text-text-faint",
            )}
          >
            <Icon className="size-3.5 shrink-0" />
            {m.label}
            {m.dormant && (
              <span className="rounded-full border border-hairline px-1 text-[0.56rem] uppercase tracking-wide">
                soon
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
