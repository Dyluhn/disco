import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";
import {
  useDisableProviderModel,
  useEnableProviderModel,
  useProviderModels,
} from "@/hooks/useModels";
import type { ModelInfo, ProviderCatalogueModel, ProviderInfo } from "@/types/models";
import { CatalogueErrorBanner } from "./CatalogueErrorBanner";
import { DEFAULT_CONTEXT_WINDOW, errorText } from "./helpers";
import { ModelRow } from "./ModelRow";
import { FIELD } from "./styles";

export function BrowseProvider({
  provider,
  enabledModels,
}: {
  provider: ProviderInfo;
  enabledModels: ModelInfo[];
}) {
  const { data, isError, error } = useProviderModels(provider.id, true);
  const enable = useEnableProviderModel(provider.id);
  const disable = useDisableProviderModel(provider.id);
  const [q, setQ] = useState("");
  const [manualId, setManualId] = useState("");
  const [manualCtx, setManualCtx] = useState("");
  const [optimistic, setOptimistic] = useState<Record<string, boolean>>({});
  const [toggleError, setToggleError] = useState<string | null>(null);

  const enabledByModel = useMemo(
    () => new Map(enabledModels.map((m) => [m.model_id, m])),
    [enabledModels],
  );
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const matched = (data ?? []).filter(
      (m) =>
        !needle ||
        m.model_id.toLowerCase().includes(needle) ||
        m.label.toLowerCase().includes(needle),
    );
    return matched.sort((a, b) => {
      const ae = enabledByModel.has(a.model_id) ? 0 : 1;
      const be = enabledByModel.has(b.model_id) ? 0 : 1;
      if (ae !== be) return ae - be;
      return a.label.localeCompare(b.label);
    });
  }, [data, enabledByModel, q]);

  const pending = enable.isPending || disable.isPending;
  // Context-required enable: the engine budgets context from context_window, so
  // when the catalogue doesn't report one we ASK instead of silently defaulting
  // (the pre-fix 8192 default poisoned every enabled model's context budget).
  const [ctxAsk, setCtxAsk] = useState<{ modelId: string; value: string } | null>(null);
  const toggle = (m: ProviderCatalogueModel) => {
    const catalogueEntry = enabledByModel.get(m.model_id);
    const nextEnabled = !catalogueEntry;
    if (nextEnabled && m.context_window == null) {
      setCtxAsk({ modelId: m.model_id, value: String(DEFAULT_CONTEXT_WINDOW) });
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
        },
        { onError: (err) => setToggleError(errorText(err)), onSettled: cleanup },
      );
    }
  };

  const confirmCtxEnable = (m: ProviderCatalogueModel, ctx: number) => {
    setOptimistic((current) => ({ ...current, [m.model_id]: true }));
    setCtxAsk(null);
    enable.mutate(
      { model_id: m.model_id, label: m.label, context_window: ctx },
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
      {/* Exactly one of these always renders: the probe's failure, the wait, an
          empty catalogue, an empty search, or the list. A silent panel used to
          be reachable whenever `data` was still undefined without the query
          reporting `isLoading` — which read as "Browse does nothing" (UI-36). */}
      {isError ? (
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
      ) : data === undefined ? (
        <p className="font-ui text-[0.82rem] text-text-muted">Loading models…</p>
      ) : data.length === 0 ? (
        <p className="rounded-control border border-dashed border-hairline px-body py-inline font-ui text-[0.8rem] text-text-faint">
          {provider.label} returned an empty catalogue.
        </p>
      ) : rows.length === 0 ? (
        <p className="rounded-control border border-dashed border-hairline px-body py-inline font-ui text-[0.8rem] text-text-faint">
          No matching models.
        </p>
      ) : null}
      {rows.length > 0 && (
        <ul className="overflow-hidden rounded-control border border-hairline bg-surface-1">
          {rows.slice(0, 100).map((m) => {
            const catalogueEntry = enabledByModel.get(m.model_id);
            const enabled = optimistic[m.model_id] ?? !!catalogueEntry;
            const asking = ctxAsk?.modelId === m.model_id;
            return (
              <ModelRow
                key={m.model_id}
                model={m}
                providerLabel={provider.label}
                enabled={enabled}
                pending={pending}
                contextWindow={catalogueEntry?.context_window ?? m.context_window}
                ctxAskValue={asking ? ctxAsk.value : undefined}
                onToggle={() => toggle(m)}
                onCtxAskChange={(value) => setCtxAsk({ modelId: m.model_id, value })}
                onCtxAskSubmit={(e) => {
                  e.preventDefault();
                  if (!asking) return;
                  const ctx = Number(ctxAsk.value);
                  if (Number.isFinite(ctx) && ctx >= 1024) confirmCtxEnable(m, ctx);
                }}
              />
            );
          })}
        </ul>
      )}
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
    </div>
  );
}
