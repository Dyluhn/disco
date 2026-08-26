/**
 * OpenRouter browse/key-management API: the offline fixture catalogue, key
 * status CRUD, and the upsert mapper that turns a browsed OpenRouter model
 * into a catalogue `ModelUpsert`.
 *
 * Extracted from api/models.ts (module-size decomposition, TS-0003);
 * re-exported (as thin delegators) from api/models so the public import path
 * and every signature stay unchanged.
 */

import { ApiError } from "../client";
import type { ModelUpsert, OpenRouterKeyStatus, OpenRouterModel } from "@/types/models";
import { apiGet, apiSend, fixtureDelay, isLive } from "../client";

// A tiny offline catalogue so the browse UI works without a backend (tests).
const FIXTURE_OPENROUTER: OpenRouterModel[] = [
  { id: "anthropic/claude-3.5-sonnet", name: "Anthropic: Claude 3.5 Sonnet", context_length: 200000, price_in_per_m: 3, price_out_per_m: 15, capabilities: ["tool_calling", "json_mode", "long_context", "vision"] },
  { id: "openai/gpt-4o", name: "OpenAI: GPT-4o", context_length: 128000, price_in_per_m: 2.5, price_out_per_m: 10, capabilities: ["tool_calling", "json_mode", "long_context", "vision"] },
  { id: "meta-llama/llama-3.3-70b-instruct", name: "Meta: Llama 3.3 70B Instruct", context_length: 131072, price_in_per_m: 0.12, price_out_per_m: 0.3, capabilities: ["tool_calling", "long_context"] },
  { id: "google/gemini-2.5-flash-image", name: "Google: Gemini 2.5 Flash Image", context_length: 32768, price_in_per_m: 0.3, price_out_per_m: 2.5, capabilities: ["vision"], image_output: true, image_price_per_m: 30 },
  { id: "openai/gpt-5-image-mini", name: "OpenAI: GPT-5 Image Mini", context_length: 128000, price_in_per_m: 2.5, price_out_per_m: 2, capabilities: ["vision"], image_output: true, image_price_per_m: 8 },
  // A dedicated per-image generator: OpenRouter's /models reports 0/0 token price; the
  // real image cost is enriched from /endpoints (image_output ×1e6) → "$X /M img-tok".
  { id: "black-forest-labs/flux.2-flex", name: "Black Forest Labs: FLUX.2 Flex", context_length: 0, price_in_per_m: 0, price_out_per_m: 0, capabilities: [], image_output: true, image_price_per_m: 14.65 },
];
let fixtureOrKey: OpenRouterKeyStatus = { configured: false, locked: false, can_store: true };

/** The live OpenRouter catalogue (hundreds of models), proxied by the app-server. */
export async function listOpenRouterModels(
  opts?: { allModalities?: boolean },
): Promise<OpenRouterModel[]> {
  // allModalities → include image-output generators (FLUX/Recraft/…); OpenRouter's
  // /models defaults to text-output only, so the image-gen picker needs this.
  if (isLive())
    return apiGet<OpenRouterModel[]>(
      opts?.allModalities ? "/api/models/openrouter?modalities=all" : "/api/models/openrouter",
    );
  await fixtureDelay();
  return FIXTURE_OPENROUTER;
}

export async function getOpenRouterKeyStatus(): Promise<OpenRouterKeyStatus> {
  if (isLive()) return apiGet<OpenRouterKeyStatus>("/api/openrouter/key");
  await fixtureDelay();
  return { ...fixtureOrKey };
}

export async function setOpenRouterKey(key: string): Promise<OpenRouterKeyStatus> {
  if (isLive()) return apiSend<OpenRouterKeyStatus>("PUT", "/api/openrouter/key", { key });
  await fixtureDelay();
  if (!key.trim()) throw new ApiError("key is empty", 400);
  fixtureOrKey = { configured: true, locked: false, can_store: true };
  return { ...fixtureOrKey };
}

export async function clearOpenRouterKey(): Promise<OpenRouterKeyStatus> {
  if (isLive()) return apiSend<OpenRouterKeyStatus>("DELETE", "/api/openrouter/key");
  await fixtureDelay();
  fixtureOrKey = { configured: false, locked: false, can_store: true };
  return { ...fixtureOrKey };
}

/** Turn an OpenRouter model into a catalogue upsert (shared key + endpoint). */
export function openRouterUpsert(m: OpenRouterModel): ModelUpsert {
  const id = "or-" + m.id.replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase();
  return {
    id,
    model_id: m.id,
    base_url: "https://openrouter.ai/api/v1",
    // The runtime injects the decrypted OpenRouter key under the canonical
    // post-rename name DISCO_OPENROUTER_API_KEY (the legacy PMX_ name is NOT
    // auto-resolved the other direction), so UI-added models must reference
    // this env var to actually authenticate.
    api_key_env: "DISCO_OPENROUTER_API_KEY",
    context_window: m.context_length,
    max_output_tokens: m.max_output_tokens ?? null,
    quantization: null,
    capabilities: [...m.capabilities],
    vision_declared:
      m.vision_status === "vision"
        ? true
        : m.vision_status === "text-only"
          ? false
          : null,
    price_in_per_m: m.price_in_per_m,
    price_out_per_m: m.price_out_per_m,
  };
}
