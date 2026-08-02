import type { ImageGenConfig, OpenRouterKeyStatus } from "@/types/models";

/** Honesty (W-50): when a tier is selected without its required config, image-gen is
 * NOT CONFIGURED — the tool fails loudly (no procedural placeholder) and slides render
 * text-only. Warn so the UI never claims a provider is active when nothing will run. */
export function getFallbackWarning(
  data: ImageGenConfig | undefined,
  orKeyStatus: OpenRouterKeyStatus | undefined,
): string | null {
  if (!data) return null;
  const orKeyReady = Boolean(orKeyStatus?.configured && !orKeyStatus?.locked);
  if (data.provider === "comfyui" && !(data.base_url ?? "").trim())
    return "Set a base URL below — until then, image generation is unavailable (not configured) and slides render text-only.";
  if (data.provider === "openai" && !(data.api_key_env ?? "").trim())
    return "Set the stored key name below and store that key in Providers — until then, image generation is unavailable (not configured).";
  if (data.provider === "openrouter" && !orKeyReady)
    return "Store a decryptable OpenRouter key in Providers — until then, image generation is unavailable (not configured).";
  if (data.provider === "openrouter" && !(data.model ?? "").trim())
    return "Set an image model id below — until then, image generation is unavailable (not configured).";
  return null;
}
