import { Bot, Search } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { CodeBlocksIcon } from "@/components/icons/CodeBlocksIcon";
import { cn } from "@/lib/cn";
import { useMode, type Mode } from "./mode";

/** One icon per mode. A total Record (not a ternary) so a new mode is a compile
 * error here rather than silently inheriting another mode's glyph. */
const MODE_ICON: Record<Mode, LucideIcon | typeof CodeBlocksIcon> = {
  search: Search,
  build: CodeBlocksIcon,
  agent: Bot,
};

/**
 * The top-level mode slider (the ONLY mode control). A segmented rounded-pill
 * toggle holding the live modes — Search (grounded research), Build (the agent
 * surface framed for software), and Agent (the same machinery, general-task
 * framing). It is both the indicator (the filled segment shows the active mode)
 * and the selector. Dormant modes (if any) carry a SOON tag and don't switch.
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
        const Icon = MODE_ICON[m.id];
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
              // min-h-9 (36px) gives a finger-friendly touch target on phones; on
              // sm+ it collapses back to the tight desktop pill height (py-hair).
              "flex min-h-9 items-center gap-hair rounded-full px-body py-hair font-ui text-[0.8rem] transition-colors sm:min-h-0",
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
