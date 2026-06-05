/**
 * The Build chat model picker — chooses the driver model that runs the agent (the
 * cost-legible picker, applied to the agent). Selecting one pins it for the next task
 * (model_override → AGENT_DRIVER on the agent-server). Free (local) vs paid (overflow)
 * is always legible at the point of choice.
 */

import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Cpu } from "lucide-react";
import { cn } from "@/lib/cn";
import { useDriverModels } from "@/hooks/useDriverModels";
import type { DriverModel } from "@/types/agent";

export function BuildModelPicker({
  value,
  onChange,
  disabled,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  disabled?: boolean;
}) {
  const { data } = useDriverModels();
  const models = data?.models ?? [];
  const defaultId = data?.default ?? null;
  const effectiveId = value ?? defaultId;
  const current = models.find((m) => m.id === effectiveId);

  const local = models.filter((m) => m.provider === "local");
  const overflow = models.filter((m) => m.provider === "openrouter");

  const Row = ({ m }: { m: DriverModel }) => (
    <Dropdown.Item
      onSelect={() => onChange(m.id === defaultId ? null : m.id)}
      className="flex cursor-pointer items-center gap-inline rounded-control px-inline py-hair font-ui text-[0.82rem] text-text outline-none data-[highlighted]:bg-surface-2"
    >
      <Check className={cn("size-3.5 shrink-0", m.id === effectiveId ? "text-accent" : "opacity-0")} aria-hidden />
      <span className="flex-1 truncate">{m.label}</span>
      <span className={cn("shrink-0 font-ui text-[0.7rem]", m.free ? "text-supported" : "text-weak")}>
        {m.free ? "free" : "paid"}
      </span>
    </Dropdown.Item>
  );

  return (
    <Dropdown.Root>
      <Dropdown.Trigger
        disabled={disabled}
        aria-label="Choose the model that runs the agent"
        className="flex max-w-[15rem] items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.76rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text disabled:opacity-50"
      >
        <Cpu className="size-3.5 shrink-0" aria-hidden />
        <span className="truncate">{current?.label ?? "Default model"}</span>
        {current && (
          <span className={cn("shrink-0", current.free ? "text-supported" : "text-weak")}>
            {current.free ? "· free" : "· paid"}
          </span>
        )}
        <ChevronDown className="size-3 shrink-0 opacity-60" aria-hidden />
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content
          align="start"
          sideOffset={6}
          className="z-50 min-w-[14rem] rounded-card border border-hairline bg-bg p-px shadow-none"
        >
          {local.length > 0 && (
            <>
              <Dropdown.Label className="px-inline py-hair font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
                Local — free
              </Dropdown.Label>
              {local.map((m) => (
                <Row key={m.id} m={m} />
              ))}
            </>
          )}
          {overflow.length > 0 && (
            <>
              <Dropdown.Separator className="my-hair h-px bg-hairline" />
              <Dropdown.Label className="px-inline py-hair font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
                Overflow — paid
              </Dropdown.Label>
              {overflow.map((m) => (
                <Row key={m.id} m={m} />
              ))}
            </>
          )}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}
