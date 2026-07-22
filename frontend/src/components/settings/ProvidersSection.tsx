import {
  AlertTriangle,
  Check,
  KeyRound,
  Plus,
  Search,
  Trash2,
} from "lucide-react";
import { useMemo, useState } from "react";
import { ApiError } from "@/api/client";
import { cn } from "@/lib/cn";
import {
  useCreateProvider,
  useDeleteProvider,
  useDisableProviderModel,
  useEnableProviderModel,
  useModels,
  useProviderModels,
  useProviderPresets,
  useProviders,
} from "@/hooks/useModels";
import type {
  ModelInfo,
  ProviderCatalogueModel,
  ProviderInfo,
  ProviderMutationResult,
  ProviderPreset,
} from "@/types/models";
import { OpenRouterSection } from "./OpenRouterSection";
import { ProviderKeysSection } from "./ProviderKeysSection";

const field =
  "w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong";

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.message) as { detail?: string };
      return parsed.detail || error.message;
    } catch {
      return error.message;
    }
  }
  return "Request failed.";
}

function hostLabel(baseUrl: string): string {
  try {
    return new URL(baseUrl).host;
  } catch {
    return baseUrl.replace(/^https?:\/\//, "").split("/")[0] || baseUrl;
  }
}

function contextLabel(value: number | null | undefined): string {
  if (!value) return "context unknown";
  return value >= 1000 ? `${Math.floor(value / 1000)}K ctx` : `${value} ctx`;
}

function priceLabel(m: ProviderCatalogueModel): string {
  if (m.price_in_per_m == null || m.price_out_per_m == null) {
    return "pricing unknown";
  }
  if (m.price_in_per_m === 0 && m.price_out_per_m === 0) return "Free";
  return `$${m.price_in_per_m.toFixed(2)} / $${m.price_out_per_m.toFixed(2)} / Mtok`;
}

function AddProviderForm({
  onCreated,
}: {
  onCreated: (result: ProviderMutationResult) => void;
}) {
  const { data: presets } = useProviderPresets();
  const create = useCreateProvider();
  const [presetId, setPresetId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [customBaseUrl, setCustomBaseUrl] = useState("");

  const selected: ProviderPreset | undefined =
    presets?.find((p) => p.id === (presetId || presets[0]?.id)) ?? presets?.[0];
  const baseUrl = selected?.requires_base_url
    ? customBaseUrl.trim()
    : (selected?.base_url ?? "");
  const label = selected?.requires_base_url
    ? hostLabel(customBaseUrl.trim()) || "Custom provider"
    : (selected?.label ?? "");
  const canSave = !!selected && !!apiKey.trim() && !!baseUrl.trim();

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!selected || !canSave) return;
        create.mutate(
          {
            label,
            base_url: baseUrl,
            kind: selected.kind,
            api_key: apiKey.trim(),
          },
          {
            onSuccess: (result) => {
              setApiKey("");
              setCustomBaseUrl("");
              onCreated(result);
            },
          },
        );
      }}
      className="flex flex-col gap-hair rounded-card border border-hairline bg-surface-1/50 p-body"
    >
      <div className="grid gap-inline md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)_auto]">
        <select
          value={selected?.id ?? ""}
          onChange={(e) => setPresetId(e.target.value)}
          aria-label="Provider preset"
          className={field}
        >
          {(presets ?? []).map((preset) => (
            <option key={preset.id} value={preset.id}>
              {preset.label}
            </option>
          ))}
        </select>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="API key"
          aria-label="Provider API key"
          className={field}
        />
        <button
          type="submit"
          data-disco-control="settings.provider-add"
          disabled={!canSave || create.isPending}
          className="flex shrink-0 items-center justify-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
        >
          <Plus className="size-3.5" aria-hidden />
          {create.isPending ? "Adding..." : "Add provider"}
        </button>
      </div>
      {selected?.requires_base_url && (
        <input
          value={customBaseUrl}
          onChange={(e) => setCustomBaseUrl(e.target.value)}
          placeholder="https://provider.example/v1"
          aria-label="Custom provider base URL"
          className={cn(field, "font-mono")}
        />
      )}
      {create.error && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {errorText(create.error)}
        </p>
      )}
    </form>
  );
}

function ToggleSwitch({
  checked,
  label,
  busy,
  onClick,
}: {
  checked: boolean;
  label: string;
  busy: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={busy}
      onClick={onClick}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:opacity-60",
        checked
          ? "border-accent/50 bg-accent/30"
          : "border-hairline bg-surface-2",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "absolute top-1/2 size-3.5 -translate-y-1/2 rounded-full transition-all",
          checked ? "left-[1.15rem] bg-accent" : "left-[0.15rem] bg-text-faint",
        )}
      />
    </button>
  );
}

function BrowseProvider({
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
          className={field}
        />
      </div>
      {isLoading && (
        <p className="font-ui text-[0.82rem] text-text-muted">Loading models...</p>
      )}
      {isError && (
        <div
          role="alert"
          className="flex flex-col gap-inline rounded-control border border-warn/50 bg-warn/5 px-body py-inline"
        >
          <div className="flex items-start gap-hair">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
            <p className="font-ui text-[0.8rem] text-text">
              {provider.label} did not return a usable /models catalogue:{" "}
              <span className="text-text-muted">{errorText(error)}</span>
            </p>
          </div>
          <form onSubmit={manualAdd} className="flex gap-inline">
            <input
              value={manualId}
              onChange={(e) => setManualId(e.target.value)}
              aria-label={`Manual model ID for ${provider.label}`}
              placeholder="model ID"
              className={cn(field, "font-mono")}
            />
            <input
              value={manualCtx}
              onChange={(e) => setManualCtx(e.target.value)}
              inputMode="numeric"
              aria-label={`Context window for manual model on ${provider.label}`}
              placeholder="context window"
              className={cn(field, "w-36 font-mono")}
            />
            <button
              type="submit"
              data-disco-control="settings.provider-manual-add"
              disabled={!manualId.trim() || !(Number(manualCtx) >= 1024) || enable.isPending}
              className="shrink-0 rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text disabled:opacity-50"
            >
              Add model ID
            </button>
          </form>
        </div>
      )}
      {!isLoading && !isError && rows.length === 0 && (
        <p className="rounded-control border border-dashed border-hairline px-body py-inline font-ui text-[0.8rem] text-text-faint">
          No matching models.
        </p>
      )}
      {rows.length > 0 && (
        <ul className="overflow-hidden rounded-control border border-hairline bg-surface-1">
          {rows.slice(0, 100).map((m) => {
            const catalogueEntry = enabledByModel.get(m.model_id);
            const enabled = optimistic[m.model_id] ?? !!catalogueEntry;
            return (
              <li
                key={m.model_id}
                className="flex items-start justify-between gap-inline border-b border-hairline px-body py-inline last:border-b-0"
              >
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-hair">
                    {enabled && <Check className="size-3.5 shrink-0 text-supported" aria-hidden />}
                    <span className="truncate font-ui text-[0.84rem] text-text">
                      {m.label}
                    </span>
                  </div>
                  <p className="break-all font-mono text-[0.7rem] text-text-faint">
                    {m.model_id} ·{" "}
                    {contextLabel(catalogueEntry?.context_window ?? m.context_window)} ·{" "}
                    {priceLabel(m)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-hair">
                  <ToggleSwitch
                    checked={enabled}
                    busy={pending}
                    label={`${enabled ? "Disable" : "Enable"} ${m.model_id}`}
                    onClick={() => toggle(m)}
                  />
                  {ctxAsk?.modelId === m.model_id && (
                    <form
                      className="flex items-center gap-hair"
                      onSubmit={(e) => {
                        e.preventDefault();
                        const ctx = Number(ctxAsk.value);
                        if (Number.isFinite(ctx) && ctx >= 1024) confirmCtxEnable(m, ctx);
                      }}
                    >
                      <input
                        autoFocus
                        inputMode="numeric"
                        value={ctxAsk.value}
                        onChange={(e) => setCtxAsk({ modelId: m.model_id, value: e.target.value })}
                        aria-label={`Context window for ${m.model_id}`}
                        placeholder="context window (tokens)"
                        className={cn(field, "w-44 font-mono text-[0.74rem]")}
                      />
                      <button
                        type="submit"
                        data-disco-control="settings.provider-ctx-confirm"
                        disabled={!(Number(ctxAsk.value) >= 1024)}
                        className="shrink-0 rounded-control border border-hairline px-inline py-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text disabled:opacity-50"
                      >
                        Enable
                      </button>
                    </form>
                  )}
                  {ctxAsk?.modelId === m.model_id && (
                    <p className="max-w-56 text-right font-ui text-[0.7rem] text-text-faint">
                      {provider.label} doesn't report context windows — enter this
                      model's real limit.
                    </p>
                  )}
                </div>
              </li>
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

function GenericProviders() {
  const { data: providers } = useProviders();
  const { data: models } = useModels();
  const remove = useDeleteProvider();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [createNotice, setCreateNotice] = useState<ProviderMutationResult | null>(
    null,
  );
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const enabledBySecret = useMemo(() => {
    const map = new Map<string, ModelInfo[]>();
    for (const model of models ?? []) {
      if (!model.api_key_env) continue;
      const list = map.get(model.api_key_env) ?? [];
      list.push(model);
      map.set(model.api_key_env, list);
    }
    return map;
  }, [models]);

  return (
    <div className="flex flex-col gap-inline">
      <AddProviderForm
        onCreated={(result) => {
          setCreateNotice(result);
          setActiveId(result.provider.id);
        }}
      />
      {createNotice && !createNotice.catalogue_ok && (
        <div
          role="alert"
          className="rounded-control border border-warn/50 bg-warn/5 px-body py-inline"
        >
          <p className="font-ui text-[0.8rem] text-text">
            {createNotice.provider.label} was saved, but /models did not answer:{" "}
            <span className="text-text-muted">
              {createNotice.catalogue_error ?? "probe failed"}
            </span>
          </p>
        </div>
      )}
      {deleteError && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {deleteError}
        </p>
      )}
      {(providers ?? []).length === 0 ? (
        <p className="rounded-control border border-dashed border-hairline px-body py-inline font-ui text-[0.8rem] text-text-faint">
          No generic providers added yet.
        </p>
      ) : (
        <ul className="overflow-hidden rounded-card border border-hairline bg-surface-1/30">
          {(providers ?? []).map((provider) => {
            const enabledModels = enabledBySecret.get(provider.secret_name) ?? [];
            const active = activeId === provider.id;
            return (
              <li key={provider.id} className="border-b border-hairline last:border-b-0">
                <div className="flex flex-col gap-inline px-body py-inline md:flex-row md:items-center md:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-hair">
                      <span className="font-ui text-[0.9rem] font-semibold text-text">
                        {provider.label}
                      </span>
                      <span
                        className={cn(
                          "flex items-center gap-hair font-ui text-[0.74rem]",
                          provider.has_key ? "text-supported" : "text-unsupported",
                        )}
                      >
                        <KeyRound className="size-3" aria-hidden />
                        {provider.has_key ? "Key ready" : "No usable key"}
                      </span>
                    </div>
                    <p className="truncate font-mono text-[0.72rem] text-text-faint">
                      {hostLabel(provider.base_url)} · {enabledModels.length} enabled
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-inline">
                    <button
                      type="button"
                      data-disco-control="settings.provider-browse"
                      onClick={() => setActiveId(active ? null : provider.id)}
                      className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text"
                    >
                      {active ? "Close" : "Browse"}
                    </button>
                    <button
                      type="button"
                      aria-label={`Delete ${provider.label}`}
                      data-disco-control="settings.provider-delete"
                      onClick={() => {
                        setDeleteError(null);
                        remove.mutate(provider.id, {
                          onError: (err) => setDeleteError(errorText(err)),
                          onSuccess: () =>
                            setActiveId((current) =>
                              current === provider.id ? null : current,
                            ),
                        });
                      }}
                      className="rounded-control border border-hairline px-inline py-hair text-text-muted transition-colors hover:border-unsupported/50 hover:text-unsupported"
                    >
                      <Trash2 className="size-3.5" aria-hidden />
                    </button>
                  </div>
                </div>
                {active && (
                  <BrowseProvider provider={provider} enabledModels={enabledModels} />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function ProvidersSection() {
  return (
    <section
      id="providers"
      aria-labelledby="providers-heading"
      className="flex flex-col gap-inline"
    >
      <header>
        <h3
          id="providers-heading"
          className="font-ui text-[0.95rem] font-semibold text-text"
        >
          Providers
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Add a provider key, browse the models that key can see, and toggle
          models into the shared catalogue.
        </p>
      </header>

      <GenericProviders />

      <OpenRouterSection />

      <details className="rounded-card border border-hairline bg-surface-1/30 px-body py-inline">
        <summary className="cursor-pointer font-ui text-[0.84rem] font-medium text-text">
          Advanced provider keys
        </summary>
        <div className="mt-inline">
          <ProviderKeysSection />
        </div>
      </details>
    </section>
  );
}
