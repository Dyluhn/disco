import { KeyRound, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { cn } from "@/lib/cn";
import { useDeleteProvider, useModels, useProviders } from "@/hooks/useModels";
import type {
  ModelInfo,
  ProviderInfo,
  ProviderMutationResult,
} from "@/types/models";
import { AddProviderForm } from "./AddProviderForm";
import { BrowseProvider } from "./BrowseProvider";
import { errorText, hostLabel, keyBadge } from "./helpers";

/** What removing this provider actually costs the user, in its own words. The
 *  server refuses while catalogue models still point at the provider's key, so
 *  say that rather than promising a removal that will come back 409. */
function deleteDescription(
  provider: ProviderInfo,
  enabledModels: ModelInfo[],
): string {
  const host = hostLabel(provider.base_url);
  if (enabledModels.length === 0) {
    return `Removes ${host} and its stored key. No catalogue model uses it.`;
  }
  const models = enabledModels.map((model) => model.label).join(", ");
  const count = `${enabledModels.length} enabled model${enabledModels.length === 1 ? "" : "s"}`;
  return `${host} still has ${count} in the catalogue (${models}). Turn them off under Browse first — the server refuses to remove a provider its models still use.`;
}

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
            {createNotice.provider.label} was saved, but its key was not
            accepted:{" "}
            <span className="text-text-muted">
              {createNotice.catalogue_error ?? "the /models probe failed"}
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
            const badge = keyBadge(provider);
            return (
              <li key={provider.id} className="border-b border-hairline last:border-b-0">
                <div className="flex flex-col gap-inline px-body py-inline md:flex-row md:items-center md:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-hair">
                      <span className="font-ui text-[0.9rem] font-semibold text-text">
                        {provider.label}
                      </span>
                      <span
                        data-provider-key-state={badge.tone}
                        className={cn(
                          "flex items-center gap-hair font-ui text-[0.74rem]",
                          badge.tone === "ok"
                            ? "text-supported"
                            : badge.tone === "bad"
                              ? "text-unsupported"
                              : "text-text-muted",
                        )}
                      >
                        <KeyRound className="size-3" aria-hidden />
                        {badge.text}
                      </span>
                    </div>
                    <p className="truncate font-mono text-[0.72rem] text-text-faint">
                      {hostLabel(provider.base_url)} · {enabledModels.length} enabled
                    </p>
                    {badge.detail && (
                      <p className="font-ui text-[0.74rem] leading-snug text-unsupported">
                        {badge.detail}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-inline">
                    <button
                      type="button"
                      data-disco-control="settings.provider-browse"
                      aria-expanded={active}
                      onClick={() => setActiveId(active ? null : provider.id)}
                      className="min-h-11 rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
                    >
                      {active ? "Close" : "Browse"}
                    </button>
                    {/* Removing a provider is as destructive as removing a
                        model (which already asks), and it takes the stored key
                        with it — so it asks too, naming what goes. */}
                    <ConfirmDialog
                      confirmContext="provider-delete"
                      title={`Remove ${provider.label}?`}
                      description={deleteDescription(provider, enabledModels)}
                      confirmLabel="Remove provider"
                      onConfirm={() => {
                        setDeleteError(null);
                        remove.mutate(provider.id, {
                          onError: (err) => setDeleteError(errorText(err)),
                          onSuccess: () =>
                            setActiveId((current) =>
                              current === provider.id ? null : current,
                            ),
                        });
                      }}
                      trigger={
                        <button
                          type="button"
                          aria-label={`Delete ${provider.label}`}
                          data-disco-control="settings.provider-delete"
                          className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-control border border-hairline px-inline py-hair text-text-muted transition-colors hover:border-unsupported/50 hover:text-unsupported lg:min-h-0 lg:min-w-0"
                        >
                          <Trash2 className="size-3.5" aria-hidden />
                        </button>
                      }
                    />
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
