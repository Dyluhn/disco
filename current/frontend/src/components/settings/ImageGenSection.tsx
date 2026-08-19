/**
 * Settings → Image generation. Controls the provider the `image_generate` tool uses
 * when an agent or build task produces an image (slide art, website backgrounds, etc.).
 *
 * Real, configured backends only (W-50: the old bundled `procedural` Pillow tier was
 * removed — it was a false affordance that always "succeeded" with abstract patterns).
 * Each choice persists to the app-server (the agent-server honors it on the NEXT
 * image-gen call — no restart). Until a tier is fully configured, image-gen is NOT
 * configured and the tool fails loudly (slides degrade to text-only).
 *  - Self-hosted (ComfyUI): your ComfyUI graph API (`base_url`; REQUIRED), keyless.
 *    Posts a workflow to /prompt and polls /history.
 *  - Paid API (OpenAI-compatible): a vendor like OpenAI gpt-image-1 / DALL·E via
 *    /v1/images/generations. Set the base URL and the stored key name.
 *  - OpenRouter: image models via chat-completions using the shared OpenRouter key.
 */

import { useEffect, useState } from "react";
import { agentIsLive } from "@/api/liveness";
import { testImageGen } from "@/api/models";
import {
  useImageGenConfig,
  useOpenRouterKey,
  useOpenRouterModels,
  useUpdateImageGenConfig,
} from "@/hooks/useModels";
import { ContextualFieldsPanel } from "./imageGenSectionParts/ContextualFieldsPanel";
import { computeImageGenFieldsDirty } from "./imageGenSectionParts/fieldsDirty";
import { getFallbackWarning } from "./imageGenSectionParts/fallbackWarning";
import {
  OPTIONS,
  type Provider,
} from "./imageGenSectionParts/providerOptions";
import { ProviderOptionList } from "./imageGenSectionParts/ProviderOptionList";
import { getWorkflowJsonError } from "./imageGenSectionParts/workflowJsonValidator";
import { ProbeButton } from "./ProbeButton";

export function ImageGenSection() {
  const { data, isLoading } = useImageGenConfig();
  const save = useUpdateImageGenConfig();
  // Live OpenRouter catalogue, fetched only while the OpenRouter tier is active —
  // filtered to image-OUTPUT models so the model field becomes a priced picker.
  const orModels = useOpenRouterModels(data?.provider === "openrouter", {
    allModalities: true,
  });
  // For the OpenRouter tier the required config is a decryptable OpenRouter key
  // (stored under Providers) — otherwise image-gen silently falls back to
  // procedural, which we must warn about rather than imply it's working.
  const orKey = useOpenRouterKey();

  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [model, setModel] = useState("");
  const [workflowJson, setWorkflowJson] = useState("");
  useEffect(() => {
    if (data) {
      setBaseUrl(data.base_url ?? "");
      setApiKeyEnv(data.api_key_env ?? "");
      setModel(data.model ?? "");
      setWorkflowJson(data.workflow_json ?? "");
    }
  }, [data]);

  const active = data?.provider;
  const activeOption = OPTIONS.find((option) => option.provider === active);

  // Switching provider preserves the current field drafts so edits aren't lost.
  const selectProvider = (provider: Provider) => {
    if (!data || provider === active) return;
    save.mutate({
      provider,
      base_url: baseUrl,
      api_key_env: apiKeyEnv,
      model,
      workflow_json: workflowJson,
    });
  };

  const fieldsDirty = computeImageGenFieldsDirty(
    data,
    baseUrl,
    apiKeyEnv,
    model,
    workflowJson,
  );

  const saveFields = () => {
    if (!data) return;
    save.mutate({
      provider: data.provider,
      base_url: baseUrl,
      api_key_env: apiKeyEnv,
      model,
      workflow_json: workflowJson,
    });
  };

  const workflowJsonError = getWorkflowJsonError(workflowJson);
  const fallbackWarning = getFallbackWarning(data, orKey.data);

  return (
    <section className="flex flex-col gap-inline">
      <header>
        <h3 className="font-ui text-[0.95rem] font-semibold text-text">
          Image generation
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          The provider used for slide art, website backgrounds, and other
          generated images. Point it at ComfyUI (self-host) or a paid API
          (OpenAI / OpenRouter) — until one is configured, image generation is
          unavailable and slides render text-only.
        </p>
      </header>

      {isLoading || !data ? (
        <p className="font-ui text-[0.86rem] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-col gap-inline">
          <div
            data-imagegen-current={active}
            className="flex items-start gap-inline rounded-control border border-hairline bg-surface-1/40 px-body py-inline"
          >
            {activeOption && (
              <activeOption.Icon
                className="mt-px size-4 shrink-0 text-accent"
                aria-hidden
              />
            )}
            <span className="flex min-w-0 flex-col gap-hair">
              <span className="font-ui text-[0.86rem] font-medium text-text">
                Current: {activeOption?.label}
              </span>
              <span className="font-ui text-[0.78rem] leading-snug text-text-faint">
                {activeOption?.help}
              </span>
            </span>
          </div>

          {fallbackWarning && (
            <div
              className="rounded-control border border-warn/35 bg-warn/5 px-body py-inline font-ui text-[0.8rem] leading-snug text-warn"
              role="status"
              data-disco-flag="imagegen-procedural-fallback"
            >
              {fallbackWarning}
            </div>
          )}

          <details className="rounded-control border border-hairline px-body py-inline">
            <summary className="cursor-pointer py-3 font-ui text-[0.84rem] font-medium text-text lg:py-0">
              Configure image generation
            </summary>
            <div className="mt-inline flex flex-col gap-inline">
              <ProviderOptionList
                active={active}
                pending={save.isPending}
                onSelect={selectProvider}
              />

              <ProbeButton
                control="settings.imagegen-test"
                idleLabel="Test image"
                run={testImageGen}
                disabled={!agentIsLive() || fieldsDirty}
                disabledHint={
                  fieldsDirty
                    ? "unsaved changes — click Save first, then Test"
                    : "agent server offline — start it to test"
                }
              />

              <ContextualFieldsPanel
                provider={data.provider}
                baseUrl={baseUrl}
                setBaseUrl={setBaseUrl}
                apiKeyEnv={apiKeyEnv}
                setApiKeyEnv={setApiKeyEnv}
                model={model}
                setModel={setModel}
                workflowJson={workflowJson}
                setWorkflowJson={setWorkflowJson}
                workflowJsonError={workflowJsonError}
                orModelsData={orModels.data}
                orModelsLoading={orModels.isLoading}
                fieldsDirty={fieldsDirty}
                savePending={save.isPending}
                onSave={saveFields}
              />
              {save.error && (
                <p className="font-ui text-[0.8rem] text-warn">
                  Couldn't save: {(save.error as Error).message}
                </p>
              )}
            </div>
          </details>
        </div>
      )}
    </section>
  );
}
