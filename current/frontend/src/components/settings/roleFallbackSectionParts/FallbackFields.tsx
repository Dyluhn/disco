/**
 * The endpoint / model / API-key-env fields for RoleFallbackSection, shown
 * only while the fallback is enabled. Pulled out of the component purely to
 * shed cyclomatic complexity (TS-0034).
 */

import { StoredCredentialField } from "../StoredCredentialField";

const fieldClass =
  "rounded-control border border-hairline bg-bg px-inline py-hair font-mono text-[0.78rem] text-text outline-none transition-colors placeholder:text-text-faint focus:border-accent/60";

export function FallbackFields({
  enabled,
  baseUrl,
  model,
  apiKeyEnv,
  onBaseUrlChange,
  onModelChange,
  onApiKeyEnvChange,
}: {
  enabled: boolean;
  baseUrl: string;
  model: string;
  apiKeyEnv: string;
  onBaseUrlChange: (value: string) => void;
  onModelChange: (value: string) => void;
  onApiKeyEnvChange: (value: string) => void;
}) {
  if (!enabled) return null;
  return (
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
          onChange={(event) => onBaseUrlChange(event.target.value)}
          placeholder="http://localhost:8080/v1"
          className={fieldClass}
        />
      </label>
      <label className="flex flex-col gap-hair">
        <span className="font-ui text-[0.8rem] text-text">Model</span>
        <input
          spellCheck={false}
          value={model}
          onChange={(event) => onModelChange(event.target.value)}
          placeholder="local-fallback-model"
          className={fieldClass}
        />
      </label>
      <StoredCredentialField
        label="Fallback credential"
        value={apiKeyEnv}
        onChange={onApiKeyEnvChange}
        placeholder="DISCO_FALLBACK_API_KEY"
        inputClassName={fieldClass}
      />
    </div>
  );
}
