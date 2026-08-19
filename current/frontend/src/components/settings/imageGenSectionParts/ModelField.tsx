import { cn } from "@/lib/cn";
import { TAP_TARGET } from "@/lib/tapTarget";
import type { OpenRouterModel } from "@/types/models";
import { FIELD_CLASS } from "./styles";
import { PricedModelSelect } from "./PricedModelSelect";

export function ModelField({
  showComfy,
  showOpenRouter,
  model,
  setModel,
  orModelsData,
  orModelsLoading,
}: {
  showComfy: boolean;
  showOpenRouter: boolean;
  model: string;
  setModel: (value: string) => void;
  orModelsData: OpenRouterModel[] | undefined;
  orModelsLoading: boolean;
}) {
  // For the OpenRouter tier the model field becomes a priced picker, filtered to
  // image-OUTPUT models — like the LLM model browser.
  const imageModels = (orModelsData ?? []).filter((m) => m.image_output);

  return (
    <label className="flex flex-col gap-hair">
      <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
        {showComfy ? "Checkpoint" : "Model"}
        <span className="font-ui text-[0.72rem] text-text-faint">
          {showComfy
            ? "· required; must exist on your ComfyUI"
            : showOpenRouter
              ? "· $/M image-tokens (the real image-gen cost, from OpenRouter)"
              : "· empty = provider default"}
        </span>
      </span>
      {showOpenRouter && imageModels.length > 0 ? (
        // Priced picker — filtered to OpenRouter image-output models, like the
        // LLM model browser. Falls back to the text input below if the live
        // catalogue is unavailable (offline / 502), so config still works.
        <PricedModelSelect model={model} setModel={setModel} imageModels={imageModels} />
      ) : (
        <input
          spellCheck={false}
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder={
            showComfy
              ? "sd_xl_base_1.0.safetensors"
              : showOpenRouter
                ? "google/gemini-2.5-flash-image"
                : "gpt-image-1"
          }
          className={cn(FIELD_CLASS, TAP_TARGET)}
        />
      )}
      {showOpenRouter && orModelsLoading && (
        <span className="font-ui text-[0.72rem] text-text-faint">
          Loading the OpenRouter image catalogue…
        </span>
      )}
      {showOpenRouter && !orModelsLoading && imageModels.length === 0 && (
        <span className="font-ui text-[0.72rem] text-text-faint">
          Couldn't load the live catalogue — type an image model id
          (e.g. google/gemini-2.5-flash-image).
        </span>
      )}
    </label>
  );
}
