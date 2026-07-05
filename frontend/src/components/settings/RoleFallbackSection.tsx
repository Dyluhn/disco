import { useEffect, useState } from "react";
import { Check, Loader2, RotateCcw, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  useRoleFallbackConfig,
  useUpdateRoleFallbackConfig,
} from "@/hooks/useModels";

export function RoleFallbackSection() {
  const { data, isLoading } = useRoleFallbackConfig();
  const save = useUpdateRoleFallbackConfig();

  const [enabled, setEnabled] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");

  useEffect(() => {
    if (!data) return;
    setEnabled(data.enabled);
    setBaseUrl(data.base_url);
    setModel(data.model);
    setApiKeyEnv(data.api_key_env);
  }, [data]);

  const dirty =
    !!data &&
    (enabled !== data.enabled ||
      baseUrl !== data.base_url ||
      model !== data.model ||
      apiKeyEnv !== data.api_key_env);
  const incomplete = enabled && (!baseUrl.trim() || !model.trim());
  const canSave = dirty && !incomplete && !save.isPending;

  const saveDraft = () => {
    if (!canSave) return;
    save.mutate({
      enabled,
      base_url: baseUrl.trim(),
      model: model.trim(),
      api_key_env: apiKeyEnv.trim(),
    });
  };

  const fieldClass =
    "rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60";

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Model resilience
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Retry transient primary-model failures for auxiliary model roles
          against a local OpenAI-compatible endpoint.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading...</p>
      ) : (
        <div className="flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
          <label className="flex cursor-pointer items-start gap-inline">
            <input
              type="checkbox"
              checked={enabled}
              disabled={save.isPending}
              onChange={(event) => setEnabled(event.target.checked)}
              className="peer sr-only"
              data-disco-control="settings.role-fallback-toggle"
            />
            <span
              className={cn(
                "mt-px flex h-5 w-9 shrink-0 items-center rounded-full border p-[2px] transition-colors",
                enabled
                  ? "border-accent/60 bg-accent/70"
                  : "border-hairline-strong bg-surface-2",
                save.isPending && "opacity-60",
              )}
              aria-hidden
            >
              <span
                className={cn(
                  "size-3.5 rounded-full bg-bg shadow-sm transition-transform",
                  enabled && "translate-x-4",
                )}
              />
            </span>
            <span className="flex min-w-0 flex-col gap-hair">
              <span className="flex items-center gap-hair font-ui text-[0.9rem] font-medium text-text">
                <RotateCcw className="size-3.5 text-accent" aria-hidden />
                Fall back to a local model for auxiliary roles
              </span>
              <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">
                Only summarizer, query-rewriter and judge roles fall back. The
                agent driver and the grounded answer never do.
              </span>
            </span>
          </label>

          {enabled && (
            <div className="grid gap-inline sm:grid-cols-2">
              <label className="flex flex-col gap-hair sm:col-span-2">
                <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                  Endpoint base URL
                  <span className="font-ui text-[0.72rem] text-text-faint">
                    · OpenAI-compatible /v1
                  </span>
                </span>
                <input
                  type="url"
                  inputMode="url"
                  spellCheck={false}
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder="http://localhost:8080/v1"
                  className={fieldClass}
                />
              </label>
              <label className="flex flex-col gap-hair">
                <span className="font-ui text-[0.8rem] text-text">Model</span>
                <input
                  spellCheck={false}
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  placeholder="local-fallback-model"
                  className={fieldClass}
                />
              </label>
              <label className="flex flex-col gap-hair">
                <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
                  API key env
                  <span className="font-ui text-[0.72rem] text-text-faint">
                    · optional
                  </span>
                </span>
                <input
                  spellCheck={false}
                  value={apiKeyEnv}
                  onChange={(event) => setApiKeyEnv(event.target.value)}
                  placeholder="DISCO_FALLBACK_API_KEY"
                  className={fieldClass}
                />
              </label>
            </div>
          )}

          {incomplete && (
            <p className="font-ui text-[0.8rem] text-warn" role="status">
              Set both a base URL and model before saving an enabled fallback.
            </p>
          )}

          <div className="flex items-center gap-inline">
            <button
              type="button"
              data-disco-control="settings.role-fallback-save"
              disabled={!canSave}
              onClick={saveDraft}
              className={cn(
                "flex items-center gap-hair self-start rounded-control border px-inline py-hair font-ui text-[0.8rem] transition-colors",
                canSave
                  ? "border-accent/50 bg-accent/10 text-text hover:bg-accent/20"
                  : "border-hairline text-text-faint",
              )}
            >
              {save.isPending ? (
                <Loader2 className="size-3 animate-spin" aria-hidden />
              ) : (
                <Check className="size-3" aria-hidden />
              )}
              Save
            </button>
            {!dirty && !save.isPending && (
              <span className="flex items-center gap-hair font-ui text-[0.76rem] text-text-faint">
                <ShieldCheck className="size-3" aria-hidden />
                Saved
              </span>
            )}
          </div>
          {save.error && (
            <p className="font-ui text-[0.8rem] text-warn">
              Couldn't save: {(save.error as Error).message}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
