/**
 * DepthTierSelector — a small UI affordance that picks the run's depth tier
 * at submit time. Three options, each honest about the cost/time tradeoff
 * (the spec's "set expectations rather than appearing hung" beat).
 */

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Gauge } from "lucide-react";
import { cn } from "@/lib/cn";

type Tier = "quick" | "standard_deep" | "exhaustive";

const TIERS: Array<{ id: Tier; label: string; note: string }> = [
  {
    id: "quick",
    label: "Quick",
    note: "3 sub-questions · 1 round · ~90s · fast verification runs",
  },
  {
    id: "standard_deep",
    label: "Standard-deep",
    note: "6 sub-questions · up to 4 rounds · ~5–10 min · the everyday tier",
  },
  {
    id: "exhaustive",
    label: "Exhaustive",
    note: "12 sub-questions · 5 rounds · 15–30 min · long-form survey",
  },
];

interface Props {
  value: Tier;
  onChange: (next: Tier) => void;
  disabled?: boolean;
}

export function DepthTierSelector({ value, onChange, disabled }: Props) {
  const active = TIERS.find((t) => t.id === value) ?? TIERS[1];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label={`Depth tier: ${active.label}`}
          data-disco-control="dr.depth-tier"
          className={cn(
            "flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-50",
          )}
        >
          <Gauge className="size-3.5 text-text-faint" aria-hidden />
          <span>{active.label}</span>
          <ChevronDown className="size-3 text-text-faint" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side="top"
          align="start"
          sideOffset={6}
          className="z-50 w-[min(28rem,90vw)] rounded-card border border-hairline bg-bg p-hair shadow-rise pmx-rise"
        >
          {TIERS.map((t) => {
            const selected = t.id === value;
            return (
              <DropdownMenu.Item
                key={t.id}
                onSelect={() => onChange(t.id)}
                className={cn(
                  "flex cursor-pointer items-start gap-inline rounded-control px-inline py-inline outline-none transition-colors",
                  "text-text data-[highlighted]:bg-surface-1",
                )}
              >
                <Check
                  className={cn(
                    "mt-px size-3.5 shrink-0",
                    selected ? "text-accent" : "text-transparent",
                  )}
                  aria-hidden
                />
                <div className="flex-1">
                  <div className="font-ui text-[0.86rem] font-medium text-text">
                    {t.label}
                  </div>
                  <div className="mt-px font-ui text-[0.74rem] leading-snug text-text-muted">
                    {t.note}
                  </div>
                </div>
              </DropdownMenu.Item>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

export type { Tier };
