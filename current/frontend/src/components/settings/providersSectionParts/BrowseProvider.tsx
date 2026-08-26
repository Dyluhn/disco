import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";
import {
  useDisableProviderModel,
  useEnableProviderModel,
  useProviderModels,
} from "@/hooks/useModels";
import { staticVisionStatus, type ModelInfo, type ProviderCatalogueModel, type ProviderInfo } from "@/types/models";
import { VisionConfirmation } from "../VisionConfirmation";
import { CatalogueErrorBanner } from "./CatalogueErrorBanner";
import { errorText } from "./helpers";
import { ProviderModelList } from "./ProviderModelList";
import { FIELD } from "./styles";

function matchingProviderRows(
  models: ProviderCatalogueModel[] | undefined,
  enabledByModel: Map<string, ModelInfo>,
  query: string,
): ProviderCatalogueModel[] {
  const needle = query.trim().toLowerCase();
  return (models ?? [])
    .filter(
      (model) =>
        !needle ||
        model.model_id.toLowerCase().includes(needle) ||
        model.label.toLowerCase().includes(needle),
    )
    .sort((left, right) => {
      const enabledOrder = Number(enabledByModel.has(right.model_id)) - Number(enabledByModel.has(left.model_id));
      return enabledOrder || left.label.localeCompare(right.label);
    });
}

export function BrowseProvider({
  provider,
  enabledModels,
}: {
  provider: ProviderInfo;
  enabledModels: ModelInfo[];
}) {
  const { data, isLoading, isError, error } = useProviderModels(provider.id, true);
  const enable = useEnableProviderModel(provider.id);
  const disable = useDisableProviderModel(provider.id);
  const [q, setQ] = useState("");
  const [manualId, setManualId] = useState("");
  const [manualCtx, setManualCtx] = useState("");
  const [optimistic, setOptimistic] = useState<Record<string, boolean>>({});
  const [toggleError, setToggleError] = useState<string | null>(null);
  const [visionAsk, setVisionAsk] = useState<ProviderCatalogueModel | null>(null);
  const [manualVisionAsk, setManualVisionAsk] = useState<ProviderCatalogueModel | null>(null);

  const enabledByModel = useMemo(
    () => new Map(enabledModels.map((m) => [m.model_id, m])),
    [enabledModels],
  );
  const rows = useMemo(
    () => matchingProviderRows(data, enabledByModel, q),
    [data, enabledByModel, q],
  );

  const pending = enable.isPending || disable.isPending;
  // Context-required enable: the engine budgets context from context_window, so
  // when the catalogue doesn't report one we ASK instead of silently defaulting
  // (the pre-fix 8192 default poisoned every enabled model's context budget).
  const [ctxAsk, setCtxAsk] = useState<{ modelId: string; value: string } | null>(null);
  const [ctxVision, setCtxVision] = useState<boolean | null>(null);
  const toggle = (m: ProviderCatalogueModel) => {
    const catalogueEntry = enabledByModel.get(m.model_id);
    const nextEnabled = !catalogueEntry;
    const visionStatus =
      m.vision_status ??
      (m.capabilities.includes("vision") ? "vision" : staticVisionStatus(m.model_id));
    if (nextEnabled && visionStatus === "unknown") {
      setVisionAsk(m);
      setToggleError(null);
      return;
    }
    if (nextEnabled && m.context_window == null) {
      setCtxAsk({ modelId: m.model_id, value: "" });
      setToggleError(null);
      return;
    }
    setOptimistic((current) => ({ ...current, [m.model_id]: nextEnabled }));
    setToggleError(null);
    const cleanup = () =>
      setOptimistic((current) => {
        const next = { ...current };
        delete next[m.model_id];
        return next;
      });
    if (catalogueEntry) {
      disable.mutate(catalogueEntry.id, {
        onError: (err) => setToggleError(errorText(err)),
        onSettled: cleanup,
      });
    } else {
      enable.mutate(
        {
          model_id: m.model_id,
          label: m.label,
          context_window: m.context_window,
          max_output_tokens: m.max_output_tokens,
          vision: undefined,
        },
        { onError: (err) => setToggleError(errorText(err)), onSettled: cleanup },
      );
    }
  };

  const confirmCtxEnable = (m: ProviderCatalogueModel, ctx: number, vision?: boolean) => {
    setOptimistic((current) => ({ ...current, [m.model_id]: true }));
    setCtxAsk(null);
    setCtxVision(null);
    enable.mutate(
      {
        model_id: m.model_id,
        label: m.label,
        context_window: ctx,
        ...(vision === undefined ? {} : { vision }),
      },
      {
        onError: (err) => setToggleError(errorText(err)),
        onSettled: () =>
          setOptimistic((current) => {
            const next = { ...current };
            delete next[m.model_id];
            return next;
          }),
      },
    );
  };

  const manualAdd = (e: React.FormEvent) => {
    e.preventDefault();
    const modelId = manualId.trim();
    const ctx = Number(manualCtx);
    if (!modelId || !(Number.isFinite(ctx) && ctx >= 1024)) return;
    if (staticVisionStatus(modelId) === "unknown") {
      setManualVisionAsk({
        model_id: modelId,
        label: modelId,
        context_window: ctx,
        max_output_tokens: null,
        capabilities: [],
        vision_status: "unknown",
      });
      return;
    }
    enable.mutate(
      { model_id: modelId, label: modelId, context_window: ctx },
      {
        onSuccess: () => {
          setManualId("");
          setManualCtx("");
        },
        onError: (err) => setToggleError(errorText(err)),
      },
    );
  };

  return (
    <div className="flex flex-col gap-inline border-t border-hairline bg-bg px-body py-inline">
      <div className="flex items-center gap-inline">
        <Search className="size-3.5 text-text-faint" aria-hidden />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label={`Search ${provider.label} models`}
          placeholder="Search models"
          className={cn(FIELD, TAP_TARGET)}
        />
      </div>
      {isLoading && (
        <p className="font-ui text-[0.82rem] text-text-muted">Loading models...</p>
      )}
      {isError && (
        <CatalogueErrorBanner
          providerLabel={provider.label}
          error={error}
          manualId={manualId}
          setManualId={setManualId}
          manualCtx={manualCtx}
          setManualCtx={setManualCtx}
          onManualAdd={manualAdd}
          addPending={enable.isPending}
        />
      )}
      {!isLoading && !isError && rows.length === 0 && (
        <p className="rounded-control border border-dashed border-hairline px-body py-inline font-ui text-[0.8rem] text-text-faint">
          No matching models.
        </p>
      )}
      <ProviderModelList
        rows={rows}
        providerLabel={provider.label}
        enabledByModel={enabledByModel}
        optimistic={optimistic}
        pending={pending}
        contextAsk={ctxAsk}
        onToggle={toggle}
        onContextChange={(modelId, value) => setCtxAsk({ modelId, value })}
        onContextSubmit={(model, event) => {
          event.preventDefault();
          if (ctxAsk?.modelId !== model.model_id) return;
          const contextWindow = Number(ctxAsk.value);
          if (Number.isFinite(contextWindow) && contextWindow >= 1024) {
            confirmCtxEnable(model, contextWindow, ctxVision ?? undefined);
          }
        }}
      />
      {rows.length > 100 && (
        <p className="font-ui text-[0.76rem] text-text-faint">
          Showing the first 100 of {rows.length}; refine the search.
        </p>
      )}
      {toggleError && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {toggleError}
        </p>
      )}
      <VisionConfirmation
        open={visionAsk !== null}
        onOpenChange={(open) => {
          if (!open) setVisionAsk(null);
        }}
        onChoose={(vision) => {
          const model = visionAsk;
          if (!model) return;
          setVisionAsk(null);
          if (model.context_window == null) {
            setCtxVision(vision);
            setCtxAsk({ modelId: model.model_id, value: "" });
            return;
          }
          setOptimistic((current) => ({ ...current, [model.model_id]: true }));
          enable.mutate(
            {
              model_id: model.model_id,
              label: model.label,
              context_window: model.context_window,
              max_output_tokens: model.max_output_tokens,
              vision,
            },
            {
              onError: (err) => setToggleError(errorText(err)),
              onSettled: () =>
                setOptimistic((current) => {
                  const next = { ...current };
                  delete next[model.model_id];
                  return next;
                }),
            },
          );
        }}
      />
      <VisionConfirmation
        open={manualVisionAsk !== null}
        onOpenChange={(open) => {
          if (!open) setManualVisionAsk(null);
        }}
        onChoose={(vision) => {
          const model = manualVisionAsk;
          if (!model) return;
          setManualVisionAsk(null);
          enable.mutate(
            {
              model_id: model.model_id,
              label: model.label,
              context_window: model.context_window,
              max_output_tokens: model.max_output_tokens,
              vision,
            },
            {
              onSuccess: () => {
                setManualId("");
                setManualCtx("");
              },
              onError: (err) => setToggleError(errorText(err)),
            },
          );
        }}
      />
    </div>
  );
}
