import type { OpenRouterModel } from "@/types/models";
import { orPrice } from "./providerOptions";
import { FIELD_CLASS } from "./styles";

/** The OpenRouter tier's priced model picker — filtered to image-output models,
 * like the LLM model browser. */
export function PricedModelSelect({
  model,
  setModel,
  imageModels,
}: {
  model: string;
  setModel: (value: string) => void;
  imageModels: OpenRouterModel[];
}) {
  return (
    <select
      value={model}
      onChange={(e) => setModel(e.target.value)}
      className={FIELD_CLASS}
    >
      <option value="">Select an image model…</option>
      {/* Keep a currently-saved id selectable even if it's not in the live
          catalogue (a custom/renamed/removed model) — never silently hide it. */}
      {model && !imageModels.some((m) => m.id === model) && (
        <option value={model}>{model} · (current)</option>
      )}
      {imageModels.map((m) => (
        <option key={m.id} value={m.id}>
          {m.id} · {orPrice(m)}
        </option>
      ))}
    </select>
  );
}
