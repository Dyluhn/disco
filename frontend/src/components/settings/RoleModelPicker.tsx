import * as Dialog from "@radix-ui/react-dialog";
import { Check, ChevronDown } from "lucide-react";
import { useMemo, useState } from "react";
import { CapabilityBadges } from "@/components/CapabilityBadges";
import { cn } from "@/lib/cn";
import { costLabel } from "@/lib/cost";
import { findModel, useModels } from "@/hooks/useModels";
import { isFree, type ModelInfo, type ModelProvider } from "@/types/models";

/**
 * A model picker for a single assignment slot (Settings matrix, Prompt 4). The
 * trigger shows the currently-assigned model; the dialog lists the catalogue
 * grouped local vs. overflow, every option cost-legible with capability badges,
 * so the user assigns with capability AND spend in view. Assignments are
 * ABSOLUTE — selecting writes exactly that model; the UI predicts/blocks nothing.
 */
interface Props {
  /** id of the currently-assigned model */
  value: string;
  onSelect: (id: string) => void;
  /** accessible label for the trigger, e.g. "Choose model for RAG answerer" */
  ariaLabel: string;
  busy?: boolean;
  /** hard-disable: the assignment isn't consumed by the runtime yet (NotWired). */
  disabled?: boolean;
}

const GROUP_LABEL: Record<ModelProvider, string> = {
  local: "Local — free",
  openrouter: "Overflow — paid",
};

export function RoleModelPicker({ value, onSelect, ariaLabel, busy, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const { data: models } = useModels();
  const selected = findModel(models, value);

  const groups = useMemo(() => {
    const by: Record<ModelProvider, ModelInfo[]> = { local: [], openrouter: [] };
    // Defensive: an entry with an unexpected/missing provider must not crash the
    // whole Settings page — bucket it under the remote group so it stays visible.
    for (const m of models ?? []) (by[m.provider] ?? by.openrouter).push(m);
    return by;
  }, [models]);

  const pick = (id: string) => {
    onSelect(id);
    setOpen(false);
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          data-disco-control="settings.model-assign"
          data-model-id={value || undefined}
          aria-label={ariaLabel}
          disabled={busy || disabled}
          className="flex w-full items-center justify-between gap-inline rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text transition-colors hover:border-hairline-strong disabled:cursor-not-allowed disabled:opacity-50 sm:w-64"
        >
          <span className="truncate">{selected?.label ?? value}</span>
          <ChevronDown className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        </button>
      </Dialog.Trigger>

      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[80vh] w-[min(34rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-card border border-hairline bg-bg pmx-rise">
          <div className="border-b border-hairline px-body py-inline">
            <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
              Assign a model
            </Dialog.Title>
            <Dialog.Description className="font-ui text-[0.8rem] text-text-muted">
              Absolute — the system uses exactly what you assign. No automatic routing.
            </Dialog.Description>
          </div>
          <div className="flex-1 overflow-y-auto px-body py-inline">
            {(["local", "openrouter"] as ModelProvider[]).map((prov) =>
              groups[prov].length === 0 ? null : (
                <div key={prov} className="mb-section">
                  <div className="px-inline pb-hair font-ui text-[0.68rem] font-semibold uppercase tracking-wide text-text-faint">
                    {GROUP_LABEL[prov]}
                  </div>
                  <ul className="flex flex-col gap-hair">
                    {groups[prov].map((m) => {
                      const active = m.id === value;
                      return (
                        <li key={m.id}>
                          <button
                            type="button"
                            data-disco-control="settings.model-assign-option"
                            data-model-id={m.id}
                            onClick={() => pick(m.id)}
                            className={cn(
                              "flex w-full flex-col gap-hair rounded-control border px-inline py-inline text-left transition-colors",
                              active
                                ? "border-accent/50 bg-surface-1"
                                : "border-transparent hover:bg-surface-1",
                            )}
                          >
                            <span className="flex items-center justify-between gap-inline">
                              <span className="flex items-center gap-hair font-ui text-[0.84rem] text-text">
                                {m.label}
                                {active && (
                                  <Check className="size-4 shrink-0 text-accent" aria-hidden />
                                )}
                              </span>
                              <span
                                className={cn(
                                  "shrink-0 font-ui text-[0.74rem]",
                                  isFree(m) ? "text-text-muted" : "text-accent",
                                )}
                              >
                                {costLabel(m)}
                              </span>
                            </span>
                            <CapabilityBadges capabilities={m.capabilities} />
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ),
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
