import type { ImageGenConfig, OpenRouterKeyStatus } from "@/types/models";

/** What is still missing before image generation can run, or null when it can.
 *
 * Image generation is optional: a stock install has none configured and nothing
 * is wrong with that. It used to read as a red error the user had caused (UI-2),
 * so the wording is now a state plus the next step, and the caller renders it in
 * a neutral tone. Real failures — a configured backend that refuses — stay red.
 */
export function getSetupNote(
  data: ImageGenConfig | undefined,
  orKeyStatus: OpenRouterKeyStatus | undefined,
): string | null {
  if (!data) return null;
  const orKeyReady = Boolean(orKeyStatus?.configured && !orKeyStatus?.locked);
  const lead = "Optional: image generation is not set up.";
  if (data.provider === "comfyui" && !(data.base_url ?? "").trim())
    return `${lead} Set a ComfyUI base URL below to turn it on. Until then, slides render text-only.`;
  if (data.provider === "openai" && !(data.api_key_env ?? "").trim())
    return `${lead} Name the stored key below, and store that key under Providers, to turn it on.`;
  if (data.provider === "openrouter" && !orKeyReady)
    return `${lead} Store an OpenRouter key under Providers to turn it on.`;
  if (data.provider === "openrouter" && !(data.model ?? "").trim())
    return `${lead} Pick an image model below to turn it on.`;
  return null;
}
