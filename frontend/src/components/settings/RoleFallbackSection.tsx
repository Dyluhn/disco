import { useEffect, useState } from "react";
import {
  useRoleFallbackConfig,
  useUpdateRoleFallbackConfig,
} from "@/hooks/useModels";
import {
  canSaveRoleFallback,
  isRoleFallbackDirty,
  isRoleFallbackIncomplete,
} from "./roleFallbackSectionParts/deriveRoleFallbackState";
import { FallbackToggle } from "./roleFallbackSectionParts/FallbackToggle";
import { FallbackFields } from "./roleFallbackSectionParts/FallbackFields";
import { FallbackIncompleteNotice } from "./roleFallbackSectionParts/FallbackIncompleteNotice";
import { FallbackSaveRow } from "./roleFallbackSectionParts/FallbackSaveRow";

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

  const dirty = isRoleFallbackDirty(data, enabled, baseUrl, model, apiKeyEnv);
  const incomplete = isRoleFallbackIncomplete(enabled, baseUrl, model);
  const canSave = canSaveRoleFallback(dirty, incomplete, save.isPending);

  const saveDraft = () => {
    if (!canSave) return;
    save.mutate({
      enabled,
      base_url: baseUrl.trim(),
      model: model.trim(),
      api_key_env: apiKeyEnv.trim(),
    });
  };

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
          <FallbackToggle
            enabled={enabled}
            pending={save.isPending}
            onChange={setEnabled}
          />

          <FallbackFields
            enabled={enabled}
            baseUrl={baseUrl}
            model={model}
            apiKeyEnv={apiKeyEnv}
            onBaseUrlChange={setBaseUrl}
            onModelChange={setModel}
            onApiKeyEnvChange={setApiKeyEnv}
          />

          <FallbackIncompleteNotice incomplete={incomplete} />

          <FallbackSaveRow
            canSave={canSave}
            pending={save.isPending}
            dirty={dirty}
            onSave={saveDraft}
            error={save.error}
          />
        </div>
      )}
    </section>
  );
}
