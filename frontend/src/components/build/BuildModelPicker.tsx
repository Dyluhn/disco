/**
 * The Build chat model picker — chooses the driver model that runs the agent (the
 * cost-legible picker, applied to the agent). Selecting one pins it for the next task
 * (model_override → AGENT_DRIVER on the agent-server). Free (local) vs paid (overflow)
 * is always legible at the point of choice.
 */

import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { Check, ChevronDown, Cpu } from "lucide-react";
import { cn } from "@/lib/cn";
import { driverCostTag } from "@/lib/cost";
import { useDriverModels, useLastSelectedModel } from "@/hooks/useDriverModels";
import { useToast } from "@/components/Toast";
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
  const { data: lastSelected } = useLastSelectedModel();
  const toast = useToast();
  const models = data?.models ?? [];
  const defaultId = data?.default ?? null;
  // P3: prefer last-selected over the static settings default when null (no
  // explicit pick for this conversation). Falls through to defaultId if no
  // last-selected has been persisted yet.
  const effectiveId = value ?? lastSelected ?? defaultId;
  const current = models.find((m) => m.id === effectiveId);

  // W-05-fu: a subscription model has price 0/token but is NOT free (it costs a
  // flat plan fee) — give it its OWN cost tag + group so it never reads as "free".
  const isSub = (m: DriverModel) => m.pricing_mode === "subscription";
  const free = models.filter((m) => m.free && !isSub(m));
  const subscription = models.filter(isSub);
  const overflow = models.filter((m) => !m.free && !isSub(m));

  // W-05: render the DISPLAY label ("Subscription"/"Free"/"Paid") via the shared
  // formatter — never the raw lowercase pricing_mode enum. Same source of truth the
  // catalogue/matrix/pill use, so "Subscription" reads identically everywhere.
  const costTag = driverCostTag;
  const costClass = (m: DriverModel) =>
    isSub(m) ? "text-text-muted" : m.free ? "text-supported" : "text-weak";

  const select = (m: DriverModel) => {
    onChange(m.id === defaultId ? null : m.id);
    // Cost honesty (A2): the build driver IS the whole agent — a paid/subscription
    // pick drives every step. Say it once; a genuinely free model stays silent.
    if (isSub(m)) {
      toast.show({
        tone: "cost",
        title: `Now building with ${m.label}`,
        body: "This subscription model drives every step of the agent (flat-rate plan, not per-token).",
      });
    } else if (!m.free) {
      toast.show({
        tone: "cost",
        title: `Now building with ${m.label}`,
        body: "This paid model drives every step of the agent for this build.",
      });
    }
  };

  // W-05: each option sits under an explicit cost SECTION header below
  // ("Local — free" / "Subscription" / "Overflow — paid"), so the row needs no
  // redundant per-row tag — and never the raw lowercase pricing_mode enum. The
  // collapsed trigger carries the cost label via the shared driverCostTag formatter.
  const Row = ({ m }: { m: DriverModel }) => (
    <Dropdown.Item
      onSelect={() => select(m)}
      className="flex cursor-pointer items-center gap-inline rounded-control px-inline py-hair font-ui text-[0.82rem] text-text outline-none data-[highlighted]:bg-surface-2"
    >
      <Check className={cn("size-3.5 shrink-0", m.id === effectiveId ? "text-accent" : "opacity-0")} aria-hidden />
      <span className="flex-1 truncate">{m.label}</span>
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
          <span className={cn("shrink-0", costClass(current))}>· {costTag(current)}</span>
        )}
        <ChevronDown className="size-3 shrink-0 opacity-60" aria-hidden />
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content
          align="start"
          sideOffset={6}
          className="z-50 min-w-[14rem] rounded-card border border-hairline bg-bg p-px shadow-none"
        >
          {free.length > 0 && (
            <>
              <Dropdown.Label className="px-inline py-hair font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
                Local — free
              </Dropdown.Label>
              {free.map((m) => (
                <Row key={m.id} m={m} />
              ))}
            </>
          )}
          {subscription.length > 0 && (
            <>
              {free.length > 0 && <Dropdown.Separator className="my-hair h-px bg-hairline" />}
              <Dropdown.Label className="px-inline py-hair font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
                Subscription
              </Dropdown.Label>
              {subscription.map((m) => (
                <Row key={m.id} m={m} />
              ))}
            </>
          )}
          {overflow.length > 0 && (
            <>
              {(free.length > 0 || subscription.length > 0) && (
                <Dropdown.Separator className="my-hair h-px bg-hairline" />
              )}
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
