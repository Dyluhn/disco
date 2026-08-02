import type { OpenRouterModel } from "@/types/models";
import { ComfyWorkflowField } from "./ComfyWorkflowField";
import { ModelField } from "./ModelField";
import type { Provider } from "./providerOptions";
import { SaveRow } from "./SaveRow";
import { FIELD_CLASS } from "./styles";
import { StoredKeyField } from "./StoredKeyField";

/** Contextual fields — endpoint (self-host/paid) + model + key env (paid),
 * shown below the provider picker for whichever tier is currently active. */
export function ContextualFieldsPanel({
  provider,
  baseUrl,
  setBaseUrl,
  apiKeyEnv,
  setApiKeyEnv,
  model,
  setModel,
  workflowJson,
  setWorkflowJson,
  workflowJsonError,
  orModelsData,
  orModelsLoading,
  fieldsDirty,
  savePending,
  onSave,
}: {
  provider: Provider;
  baseUrl: string;
  setBaseUrl: (value: string) => void;
  apiKeyEnv: string;
  setApiKeyEnv: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
  workflowJson: string;
  setWorkflowJson: (value: string) => void;
  workflowJsonError: string | null;
  orModelsData: OpenRouterModel[] | undefined;
  orModelsLoading: boolean;
  fieldsDirty: boolean;
  savePending: boolean;
  onSave: () => void;
}) {
  const showOpenRouter = provider === "openrouter";
  const showPaid = provider === "openai";
  const showComfy = provider === "comfyui";
  const showUrl = showComfy || showPaid;

  return (
    <div className="mt-hair flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/40 px-body py-inline">
      {showOpenRouter && (
        <p className="font-ui text-[0.78rem] leading-relaxed text-text-faint">
          Uses your{" "}
          <strong className="text-text">OpenRouter key</strong> from{" "}
          <em>Providers</em> — store it there if you haven't, or image
          generation is unavailable (not configured). Every OpenRouter
          image model is <strong className="text-text">paid</strong>;
          add credits at openrouter.ai/settings/credits.
        </p>
      )}
      {showUrl && (
        <label className="flex flex-col gap-hair">
          <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
            Endpoint base URL
            <span className="font-ui text-[0.72rem] text-text-faint">
              {showPaid
                ? "· origin (we append /v1/images/generations) OR a full endpoint URL"
                : "· ComfyUI host"}
            </span>
          </span>
          <input
            type="url"
            inputMode="url"
            spellCheck={false}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={
              showPaid
                ? "https://api.openai.com  — or ImageRouter's full URL https://api.imagerouter.io/v1/openai/images/generations"
                : "http://host:8188  (required for ComfyUI)"
            }
            className={FIELD_CLASS}
          />
        </label>
      )}
      <ModelField
        showComfy={showComfy}
        showOpenRouter={showOpenRouter}
        model={model}
        setModel={setModel}
        orModelsData={orModelsData}
        orModelsLoading={orModelsLoading}
      />
      {showComfy && (
        <ComfyWorkflowField
          workflowJson={workflowJson}
          setWorkflowJson={setWorkflowJson}
          workflowJsonError={workflowJsonError}
        />
      )}
      {showPaid && (
        <StoredKeyField apiKeyEnv={apiKeyEnv} setApiKeyEnv={setApiKeyEnv} />
      )}
      <SaveRow fieldsDirty={fieldsDirty} savePending={savePending} onSave={onSave} />
    </div>
  );
}
