/**
 * RecencySelector — controls the DR-3 recency_window field sent with a new
 * deep-research conversation.  Three options: Off (no time filter), Past month,
 * Past week.  Sits next to DepthTierSelector in the research form.
 *
 * Controlled component: parent owns the value + onChange handler (same
 * pattern as DepthTierSelector).
 */

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { CalendarDays, Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";

export type RecencyWindow = "month" | "week" | null;

const OPTIONS: Array<{ id: RecencyWindow; label: string; note: string }> = [
  {
    id: null,
    label: "Any time",
    note: "No date filter — searches all available sources",
  },
  {
    id: "month",
    label: "Past month",
    note: "Limits search results to the past 30 days",
  },
  {
    id: "week",
    label: "Past week",
    note: "Limits search results to the past 7 days",
  },
];

interface Props {
  value: RecencyWindow;
  onChange: (next: RecencyWindow) => void;
  disabled?: boolean;
}

export function RecencySelector({ value, onChange, disabled }: Props) {
  const active = OPTIONS.find((o) => o.id === value) ?? OPTIONS[0];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label={`Recency filter: ${active.label}`}
          data-disco-control="dr.recency"
          className={cn(
            "flex min-h-11 items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-50 lg:min-h-0",
          )}
        >
          <CalendarDays className="size-3.5 text-text-faint" aria-hidden />
          <span>{active.label}</span>
          <ChevronDown className="size-3 text-text-faint" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side="top"
          align="start"
          sideOffset={6}
          className="z-50 w-[min(24rem,90vw)] rounded-card border border-hairline bg-bg p-hair shadow-rise pmx-rise"
        >
          {OPTIONS.map((opt) => {
            const selected = opt.id === value;
            return (
              <DropdownMenu.Item
                key={String(opt.id)}
                onSelect={() => onChange(opt.id)}
                className={cn(
                  "flex min-h-11 cursor-pointer items-start gap-inline rounded-control px-inline py-inline outline-none transition-colors lg:min-h-0",
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
                    {opt.label}
                  </div>
                  <div className="mt-px font-ui text-[0.74rem] leading-snug text-text-muted">
                    {opt.note}
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
