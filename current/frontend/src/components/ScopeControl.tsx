import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  BookOpen,
  Check,
  ChevronDown,
  Globe,
  Layers,
  Monitor,
  Shapes,
  SlidersHorizontal,
  Smartphone,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import {
  defaultScope,
  SCOPE_OPTIONS,
  type ScopeId,
  useMode,
} from "@/shell/mode";

/**
 * The in-chat scope control (Prompt 3 refinement A) — ONE reusable component whose
 * option set is a function of the active top-level mode (the slider). When Search
 * is active: Standard (default) + Deep Research (dormant). When Build is active
 * (future): Site / Desktop Application / Mobile Application / Other. Same shape and
 * design across both modes — the options swap, the component does not.
 *
 * Deep Research lives HERE, as a child of Search — never a top-level mode.
 */
interface Props {
  value: ScopeId;
  onChange: (next: ScopeId) => void;
}

// Per-option glyphs (kept out of the data module so it stays React-free).
const SCOPE_ICON: Record<ScopeId, LucideIcon> = {
  standard: Layers,
  deep_research: BookOpen, // chosen: open book (calmer than a graduation cap)
  site: Globe,
  desktop: Monitor,
  mobile: Smartphone,
  other: Shapes,
};

export function ScopeControl({ value, onChange }: Props) {
  const { mode } = useMode();
  const options = SCOPE_OPTIONS[mode];
  // If the held scope doesn't belong to the active mode's set, fall back to the
  // mode's default — so the option-set swaps cleanly when the mode changes.
  const current = options.find((o) => o.id === value) ?? options.find((o) => o.id === defaultScope(mode))!;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={`Scope: ${current.label}`}
          data-disco-control="search.scope"
          className="flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text"
        >
          <SlidersHorizontal className="size-3.5 shrink-0 text-text-faint" aria-hidden />
          <span className="truncate">{current.label}</span>
          <ChevronDown className="size-3 shrink-0 text-text-faint" aria-hidden />
        </button>
      </DropdownMenu.Trigger>

      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side="top"
          align="start"
          sideOffset={6}
          collisionPadding={12}
          className="z-50 min-w-[14rem] rounded-card border border-hairline bg-bg p-px pmx-rise"
        >
          {options.map((o) => {
            const Icon = SCOPE_ICON[o.id];
            const active = o.id === current.id;
            return (
              <DropdownMenu.Item
                key={o.id}
                disabled={o.dormant}
                onSelect={() => onChange(o.id)}
                className={cn(
                  "flex cursor-pointer items-center gap-inline rounded-control px-inline py-inline font-ui text-[0.82rem] outline-none transition-colors",
                  o.dormant
                    ? "cursor-not-allowed text-text-faint"
                    : "text-text data-[highlighted]:bg-surface-1",
                )}
              >
                <Icon className="size-3.5 shrink-0 text-text-faint" aria-hidden />
                <span className="flex-1">{o.label}</span>
                {o.dormant && (
                  <span className="rounded-[0.25rem] border border-hairline px-1 text-[0.56rem] uppercase tracking-wide">
                    soon
                  </span>
                )}
                {active && <Check className="size-3.5 shrink-0 text-accent" aria-hidden />}
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
