import { Cloud, Cpu, Server } from "lucide-react";

export type Provider = "comfyui" | "openai" | "openrouter";

export const OPTIONS: {
  provider: Provider;
  Icon: typeof Cpu;
  label: string;
  help: string;
}[] = [
  {
    provider: "comfyui",
    Icon: Server,
    label: "Self-hosted (ComfyUI)",
    help: "Your ComfyUI graph API. Keyless, but the base URL is REQUIRED (without it, image-gen is unavailable). Ships a built-in SDXL/SD workflow — set Checkpoint to a model file that exists on your ComfyUI. For FLUX / SD3 / custom graphs, paste a 'Save (API Format)' workflow below.",
  },
  {
    provider: "openai",
    Icon: Cloud,
    label: "Paid API (OpenAI-compatible)",
    help: "A vendor like OpenAI gpt-image-1 / DALL·E via /v1/images/generations (or ImageRouter's full /v1/openai/... URL). Set the base URL and choose the stored key name.",
  },
  {
    provider: "openrouter",
    Icon: Cloud,
    label: "OpenRouter (image models)",
    help: "Image models via OpenRouter chat-completions (modalities:[image,text]) using the OpenRouter key in Providers. Paid — every OpenRouter image model costs credits. Set Model to an image model id; cheapest is google/gemini-2.5-flash-image.",
  },
];

/** Honest price label for an OpenRouter IMAGE model, from real OpenRouter data:
 *  - image_price_per_m (the /endpoints `image_output` rate ×1e6) is the actual
 *    image-generation cost → "$X /M img-tok". Shown first for every image model,
 *    incl. the dedicated generators (FLUX/Recraft/Seedream) the catalogue zeroes out.
 *  - else the text token price (some dual chat+image models) → "$in/$out /Mtok".
 *  - else (genuinely no price reported) "pricing on openrouter.ai" — never "Free",
 *    which would be a false tag on a paid model. (Per-IMAGE $ isn't shown: OpenRouter
 *    bills per image-token and tokens-per-image varies, so a per-image figure would be
 *    an invented number.) */
export const orPrice = (m: {
  price_in_per_m: number;
  price_out_per_m: number;
  image_price_per_m?: number;
}): string => {
  if (m.image_price_per_m && m.image_price_per_m > 0)
    return `$${m.image_price_per_m.toFixed(2)} /M img-tok`;
  if (m.price_in_per_m > 0 || m.price_out_per_m > 0)
    return `$${m.price_in_per_m.toFixed(2)} in / $${m.price_out_per_m.toFixed(2)} out /Mtok`;
  return "pricing on openrouter.ai";
};
