/**
 * The Build chat model picker — chooses the driver model that runs the agent (the
 * cost-legible picker, applied to the agent). Selecting one pins it for the next task
 * (model_override → AGENT_DRIVER on the agent-server). Free (local) vs paid (overflow)
 * is always legible at the point of choice.
 */

import * as Dropdown from "@radix-ui/react-dropdown-menu";
import * as Dialog from "@radix-ui/react-dialog";
import { Check, ChevronDown, Cpu, Eye } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/cn";
import { costTag, driverCostTag } from "@/lib/cost";
import { useDriverModels, useLastSelectedModel } from "@/hooks/useDriverModels";
import {
  useAssignments,
  useModels,
  useUpdateAssignments,
} from "@/hooks/useModels";
import { useToast } from "@/components/toastApi";
import type { DriverModel } from "@/types/agent";
import type { ModelInfo } from "@/types/models";

const isSubscriptionDriver = (model: DriverModel) =>
  model.pricing_mode === "subscription";

function driverCostClass(model: DriverModel): string {
  if (isSubscriptionDriver(model)) return "text-text-muted";
  return model.free ? "text-supported" : "text-weak";
}

function VisionRecommendationDialog({
  open,
  onOpenChange,
  candidates,
  busy,
  saveFailed,
  onSelect,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidates: ModelInfo[];
  busy: boolean;
  saveFailed: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[80vh] w-[min(34rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col gap-body overflow-y-auto rounded-card border border-hairline bg-bg p-body pmx-rise">
          <div className="flex items-start gap-inline">
            <Eye className="mt-px size-4 shrink-0 text-accent" aria-hidden />
            <div>
              <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
                Add visual inspection?
              </Dialog.Title>
              <Dialog.Description className="font-ui text-[0.8rem] leading-snug text-text-muted">
                Your selected main model is text-only. You can dedicate an
                image-capable model to answer one focused screenshot question at a
                time. It receives no tools or conversation history and never takes
                over the agent.
              </Dialog.Description>
            </div>
          </div>
          {candidates.length > 0 ? (
            <div className="flex flex-col gap-hair">
              {candidates.map((model) => (
                <button
                  key={model.id}
                  type="button"
                  data-disco-control="build.vision-model-recommendation"
                  data-model-id={model.id}
                  disabled={busy}
                  onClick={() => onSelect(model.id)}
                  className="flex items-center justify-between gap-inline rounded-control border border-hairline px-inline py-inline text-left font-ui text-[0.82rem] text-text transition-colors hover:border-hairline-strong disabled:opacity-50"
                >
                  <span>{model.label}</span>
                  <span className="shrink-0 text-text-faint">{costTag(model)}</span>
                </button>
              ))}
            </div>
          ) : (
            <p className="font-ui text-[0.8rem] text-text-muted">
              No configured model currently advertises image support. Mark the
              correct model in Settings when one is available.
            </p>
          )}
          {saveFailed && (
            <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
              The visual model assignment was not saved. Your main-model selection
              is unchanged.
            </p>
          )}
          <div className="flex justify-end gap-inline">
            <Dialog.Close asChild>
              <button
                type="button"
                className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
              >
                Keep text/DOM fallback
              </button>
            </Dialog.Close>
            <Link
              to="/settings#models-providers"
              onClick={() => onOpenChange(false)}
              className="rounded-control bg-accent px-inline py-hair font-ui text-[0.8rem] font-medium text-bg"
            >
              Open model settings
            </Link>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function DriverModelMenu({
  models,
  current,
  effectiveId,
  disabled,
  onSelect,
}: {
  models: DriverModel[];
  current: DriverModel | undefined;
  effectiveId: string | null;
  disabled?: boolean;
  onSelect: (model: DriverModel) => void;
}) {
  const free = models.filter((model) => model.free && !isSubscriptionDriver(model));
  const subscription = models.filter(isSubscriptionDriver);
  const overflow = models.filter(
    (model) => !model.free && !isSubscriptionDriver(model),
  );
  const Row = ({ model }: { model: DriverModel }) => (
    <Dropdown.Item
      onSelect={() => onSelect(model)}
      className="flex cursor-pointer items-center gap-inline rounded-control px-inline py-hair font-ui text-[0.82rem] text-text outline-none data-[highlighted]:bg-surface-2"
    >
      <Check
        className={cn(
          "size-3.5 shrink-0",
          model.id === effectiveId ? "text-accent" : "opacity-0",
        )}
        aria-hidden
      />
      <span className="flex-1 truncate">{model.label}</span>
    </Dropdown.Item>
  );

  return (
    <Dropdown.Root>
      <Dropdown.Trigger
        disabled={disabled}
        aria-label="Choose the model that runs the agent"
        className="flex max-w-[15rem] max-lg:min-h-11 items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.76rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text disabled:opacity-50"
      >
        <Cpu className="size-3.5 shrink-0" aria-hidden />
        <span className="truncate">
          {current?.label ??
            (models.length === 0 ? "Configure model" : "Default model")}
        </span>
        {current && (
          <span className={cn("shrink-0", driverCostClass(current))}>
            · {driverCostTag(current)}
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
          {models.length === 0 && (
            <div className="flex max-w-[16rem] flex-col gap-hair px-inline py-inline font-ui text-[0.78rem] text-text-muted">
              <span>No driver model is configured.</span>
              <Link
                className="font-medium text-accent hover:underline"
                to="/settings#models-providers"
              >
                Configure in Settings
              </Link>
            </div>
          )}
          {free.length > 0 && (
            <>
              <Dropdown.Label className="px-inline py-hair font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
                Local — free
              </Dropdown.Label>
              {free.map((model) => (
                <Row key={model.id} model={model} />
              ))}
            </>
          )}
          {subscription.length > 0 && (
            <>
              {free.length > 0 && (
                <Dropdown.Separator className="my-hair h-px bg-hairline" />
              )}
              <Dropdown.Label className="px-inline py-hair font-ui text-[0.66rem] uppercase tracking-wide text-text-faint">
                Subscription
              </Dropdown.Label>
              {subscription.map((model) => (
                <Row key={model.id} model={model} />
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
              {overflow.map((model) => (
                <Row key={model.id} model={model} />
              ))}
            </>
          )}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

export function BuildModelPicker({
  value,
  onChange,
  disabled,
  surface = "build",
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  disabled?: boolean;
  surface?: "build" | "agent";
}) {
  const { data } = useDriverModels();
  const { data: lastSelected } = useLastSelectedModel();
  const { data: assignments } = useAssignments();
  const { data: settingsModels } = useModels();
  const updateAssignments = useUpdateAssignments();
  const [visionPromptOpen, setVisionPromptOpen] = useState(false);
  const toast = useToast();
  const models = data?.models ?? [];
  const defaultId = data?.default ?? null;
  // P3: prefer last-selected over the static settings default when null (no
  // explicit pick for this conversation). Falls through to defaultId if no
  // last-selected has been persisted yet.
  const effectiveId = value ?? lastSelected ?? defaultId;
  const current = models.find((m) => m.id === effectiveId);

  const visionCandidates = (settingsModels ?? []).filter((m) =>
    m.vision_status === "vision" ||
    (m.vision_status === undefined && m.capabilities.includes("vision")),
  );
  const selectVision = (id: string) => {
    updateAssignments.mutate(
      { vision_model: id },
      { onSuccess: () => setVisionPromptOpen(false) },
    );
  };

  const select = (m: DriverModel) => {
    onChange(m.id === defaultId ? null : m.id);
    const action = surface === "agent" ? "working" : "building";
    // Cost honesty (A2): the build driver IS the whole agent — a paid/subscription
    // pick drives every step. Say it once; a genuinely free model stays silent.
    if (isSubscriptionDriver(m)) {
      toast.show({
        tone: "cost",
        title: `Now ${action} with ${m.label}`,
        body: "This subscription model drives every step of the agent (flat-rate plan, not per-token).",
      });
    } else if (!m.free) {
      toast.show({
        tone: "cost",
        title: `Now ${action} with ${m.label}`,
        body: `This paid model drives every step of the agent for this ${surface === "agent" ? "task" : "build"}.`,
      });
    }
    if (
      assignments &&
      !assignments.vision_model &&
      m.vision_status !== "vision" && !m.capabilities.includes("vision")
    ) {
      setVisionPromptOpen(true);
    }
  };

  return (
    <>
      <DriverModelMenu
        models={models}
        current={current}
        effectiveId={effectiveId}
        disabled={disabled}
        onSelect={select}
      />
      <VisionRecommendationDialog
        open={visionPromptOpen}
        onOpenChange={setVisionPromptOpen}
        candidates={visionCandidates}
        busy={updateAssignments.isPending}
        saveFailed={Boolean(updateAssignments.error)}
        onSelect={selectVision}
      />
    </>
  );
}
