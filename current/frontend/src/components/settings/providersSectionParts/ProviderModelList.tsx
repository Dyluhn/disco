import type { FormEvent } from "react";
import type { ModelInfo, ProviderCatalogueModel } from "@/types/models";
import { ModelRow } from "./ModelRow";

export function ProviderModelList({
  rows,
  providerLabel,
  enabledByModel,
  optimistic,
  pending,
  contextAsk,
  onToggle,
  onContextChange,
  onContextSubmit,
}: {
  rows: ProviderCatalogueModel[];
  providerLabel: string;
  enabledByModel: Map<string, ModelInfo>;
  optimistic: Record<string, boolean>;
  pending: boolean;
  contextAsk: { modelId: string; value: string } | null;
  onToggle: (model: ProviderCatalogueModel) => void;
  onContextChange: (modelId: string, value: string) => void;
  onContextSubmit: (model: ProviderCatalogueModel, event: FormEvent) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <ul className="overflow-hidden rounded-control border border-hairline bg-surface-1">
      {rows.slice(0, 100).map((model) => {
        const catalogueEntry = enabledByModel.get(model.model_id);
        const enabled = optimistic[model.model_id] ?? Boolean(catalogueEntry);
        const asking = contextAsk?.modelId === model.model_id;
        return (
          <ModelRow
            key={model.model_id}
            model={model}
            providerLabel={providerLabel}
            enabled={enabled}
            pending={pending}
            contextWindow={catalogueEntry?.context_window ?? model.context_window}
            ctxAskValue={asking ? contextAsk.value : undefined}
            onToggle={() => onToggle(model)}
            onCtxAskChange={(value) => onContextChange(model.model_id, value)}
            onCtxAskSubmit={(event) => onContextSubmit(model, event)}
          />
        );
      })}
    </ul>
  );
}
