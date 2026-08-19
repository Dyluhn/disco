import { KeyRound, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { useDeleteProvider, useModels, useProviders } from "@/hooks/useModels";
import type { ModelInfo, ProviderMutationResult } from "@/types/models";
import { AddProviderForm } from "./AddProviderForm";
import { BrowseProvider } from "./BrowseProvider";
import { errorText, hostLabel } from "./helpers";

export function GenericProviders() {
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
                          provider.has_key || provider.requires_api_key === false
                            ? "text-supported"
                            : "text-unsupported",
                        )}
                      >
                        <KeyRound className="size-3" aria-hidden />
                        {provider.requires_api_key === false
                          ? "No key needed"
                          : provider.has_key
                            ? "Key ready"
                            : "No usable key"}
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
                      className="min-h-11 rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
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
                      className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-control border border-hairline px-inline py-hair text-text-muted transition-colors hover:border-unsupported/50 hover:text-unsupported lg:min-h-0 lg:min-w-0"
                    >
                      <Trash2 className="size-3.5" aria-hidden />
                    </button>
                  </div>
                </div>
                {active && (
                  <BrowseProvider
                    provider={provider}
                    enabledModels={enabledModels}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
