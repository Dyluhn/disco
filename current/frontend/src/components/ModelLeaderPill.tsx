import * as Dialog from "@radix-ui/react-dialog";
import { Check, ChevronDown, Cpu, Globe } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { costLabel, costTag } from "@/lib/cost";
import { useDriverModels } from "@/hooks/useDriverModels";
import { findModel, useAssignments, useModels } from "@/hooks/useModels";
import { isFree, isMetered, type ModelInfo, type ModelProvider } from "@/types/models";
import { CapabilityBadges } from "./CapabilityBadges";
import { useToast } from "./toastApi";

/**
 * The model "leader" pill (Prompt 3C). Selects, by hand, the model that LEADS
 * this conversation — overriding the Settings default for this conversation only.
 * The picker is cost-legible (Free vs $/Mtok), grouped local vs. overflow, and the
 * selection is ABSOLUTE (no automatic routing). `value === null` means "use the
 * Settings default"; otherwise it pins a specific model for the conversation.
 */
interface Props {
  value: string | null; // leader model id, or null → settings default
  onChange: (id: string | null) => void;
}

const GROUP_LABEL: Record<ModelProvider, string> = {
  local: "Local — free",
  openrouter: "Overflow — paid",
};

/** The pill face shows the same short name the driver catalogue (and the
 * DriverModelNotice) uses for this model; the surface catalogue's label, role
 * prefix stripped, is only the fallback for models the driver list omits. */
function faceName(
  drivers: ReturnType<typeof useDriverModels>["data"],
  effective: ModelInfo | null,
): string | null {
  if (!effective) return null;
  const short = drivers?.models.find((m) => m.id === effective.id)?.label;
  return short ?? effective.label.replace(/^Driver\s[^—]*—\s*/u, "");
}

export function ModelLeaderPill({ value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const { data: models } = useModels();
  const { data: assignments } = useAssignments();
  const { data: driverModels } = useDriverModels();
  const toast = useToast();

  const defaultModel = findModel(models, assignments?.default_model ?? null);
  const selected = findModel(models, value);
  // What actually leads: the explicit pick, else the settings default.
  const effective = selected ?? defaultModel;

  const groups = useMemo(() => {
    const by: Record<ModelProvider, ModelInfo[]> = { local: [], openrouter: [] };
    // Defensive: an unexpected/missing provider must not crash the surface —
    // bucket it under the remote group so it stays visible.
    for (const m of models ?? []) (by[m.provider] ?? by.openrouter).push(m);
    return by;
  }, [models]);

  const pick = (id: string | null) => {
    onChange(id);
    setOpen(false);
    // Cost honesty (A2): a PAID pick drives the WHOLE generative pipeline
    // (answering, rewriting, summarizing) — not just the answer. Say so once, so
    // nobody runs up a bill thinking they only changed one model. Free/local: silent.
    const m = id ? findModel(models, id) : null;
    if (m && isMetered(m)) {
      // Only METERED (per-token) picks run up a bill — a subscription model is a flat
      // plan fee, so the "you'll run a bill" warning would be false for it (W-05).
      toast.show({
        tone: "cost",
        title: `Now using ${m.label} for all generative work`,
        body: "Answering, rewriting, and summarizing all run on this paid model for this conversation.",
      });
    }
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          aria-label="Choose the model that leads this conversation"
          data-disco-control="search.model-pill"
          className="flex min-h-11 max-w-[14rem] items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
        >
          {effective && isFree(effective) ? (
            <Cpu className="size-3.5 shrink-0 text-text-faint" aria-hidden />
          ) : (
            <Globe className="size-3.5 shrink-0 text-accent" aria-hidden />
          )}
          {/* Fresh install honesty: an EMPTY catalogue means there is no default to
              fall back to — say "Configure model" (mirrors BuildModelPicker) instead
              of implying a working default that doesn't exist. The face strips the
              catalogue's "Driver Local — " role prefix (the dialog rows keep it);
              the pill names the model, not the assignment slot. */}
          <span className="truncate">
            {faceName(driverModels, effective) ??
              ((models?.length ?? 0) === 0 ? "Configure model" : "Default model")}
          </span>
          {effective && (
            <span className="shrink-0 text-text-faint">· {costTag(effective)}</span>
          )}
          <ChevronDown className="size-3 shrink-0 text-text-faint" aria-hidden />
        </button>
      </Dialog.Trigger>

      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[80vh] w-[min(34rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-card border border-hairline bg-bg pmx-rise">
          <div className="flex items-start justify-between gap-inline border-b border-hairline px-body py-inline">
            <div>
              <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
                Conversation lead model
              </Dialog.Title>
              <Dialog.Description className="font-ui text-[0.8rem] text-text-muted">
                Pins the model that leads this conversation. Absolute — no automatic routing.
              </Dialog.Description>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-body py-inline">
            {/* Use-the-default option */}
            <button
              type="button"
              onClick={() => pick(null)}
              data-model-id="__default__"
              className={cn(
                "flex min-h-11 w-full items-center justify-between gap-inline rounded-control border px-inline py-inline text-left transition-colors lg:min-h-0",
                value === null
                  ? "border-accent/50 bg-surface-1"
                  : "border-transparent hover:bg-surface-1",
              )}
            >
              <span className="font-ui text-[0.84rem] text-text">
                Use Settings default
                {defaultModel && (
                  <span className="text-text-faint"> · {defaultModel.label}</span>
                )}
              </span>
              {value === null && <Check className="size-4 shrink-0 text-accent" aria-hidden />}
            </button>

            {(["local", "openrouter"] as ModelProvider[]).map((prov) =>
              groups[prov].length === 0 ? null : (
                <div key={prov} className="mt-section">
                  <div className="px-inline pb-hair font-ui text-[0.68rem] font-semibold uppercase tracking-wide text-text-faint">
                    {GROUP_LABEL[prov]}
                  </div>
                  <ul className="flex flex-col gap-hair">
                    {groups[prov].map((m) => {
                      const active = value === m.id;
                      return (
                        <li key={m.id}>
                          <button
                            type="button"
                            onClick={() => pick(m.id)}
                            data-model-id={m.id}
                            className={cn(
                              "flex min-h-11 w-full flex-col gap-hair rounded-control border px-inline py-inline text-left transition-colors lg:min-h-0",
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
