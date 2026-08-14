/**
 * Generic (BYO-key) provider management: presets, provider CRUD, per-provider
 * model catalogue browsing, and enable/disable of a provider's model into the
 * shared model catalogue.
 *
 * Extracted from api/models.ts (module-size decomposition, TS-0003);
 * re-exported (as thin delegators) from api/models so the public import path
 * and every signature stay unchanged.
 *
 * Enable/disable reach into the SAME fixture catalogue `./crud` owns, via its
 * exported `fixtureCatalogueSnapshot`/`appendFixtureCatalogueEntry`/
 * `toModelInfo` — the catalogue array itself stays private to `./crud`, which
 * remains its sole writer. `disableProviderModel` delegates its removal to
 * `./crud`'s `deleteModel` directly, unchanged from the original single-file
 * behavior.
 */

import { ApiError } from "../client";
import type {
  ModelInfo,
  ModelUpsert,
  ProviderCatalogueModel,
  ProviderCreate,
  ProviderEnableBody,
  ProviderInfo,
  ProviderMutationResult,
  ProviderPatch,
  ProviderPreset,
} from "@/types/models";
import { apiGet, apiSend, fixtureDelay, isLive } from "../client";
import { appendFixtureCatalogueEntry, deleteModel, fixtureCatalogueSnapshot, toModelInfo } from "./crud";

const PROVIDER_PRESETS_FIXTURE: ProviderPreset[] = [
  { id: "openrouter", label: "OpenRouter", base_url: "https://openrouter.ai/api/v1", kind: "openai-compat" },
  { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", kind: "openai-compat" },
  { id: "anthropic", label: "Anthropic", base_url: "https://api.anthropic.com/v1", kind: "anthropic" },
  { id: "groq", label: "Groq", base_url: "https://api.groq.com/openai/v1", kind: "openai-compat" },
  { id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com", kind: "openai-compat" },
  { id: "together", label: "Together", base_url: "https://api.together.xyz/v1", kind: "openai-compat" },
  { id: "fireworks", label: "Fireworks", base_url: "https://api.fireworks.ai/inference/v1", kind: "openai-compat" },
  { id: "mistral", label: "Mistral", base_url: "https://api.mistral.ai/v1", kind: "openai-compat" },
  { id: "xai", label: "xAI", base_url: "https://api.x.ai/v1", kind: "openai-compat" },
  { id: "opencode-go", label: "OpenCode Go", base_url: "https://opencode.ai/zen/go/v1", kind: "openai-compat" },
  { id: "custom-openai-compatible", label: "Custom (OpenAI-compatible)", base_url: "", kind: "openai-compat", requires_base_url: true },
];

let fixtureProviders: ProviderInfo[] = [];

const FIXTURE_PROVIDER_CATALOGUE: Record<string, ProviderCatalogueModel[]> = {
  "openai-compat": [
    {
      model_id: "gpt-4o-mini",
      label: "GPT-4o mini",
      context_window: 128000,
      price_in_per_m: null,
      price_out_per_m: null,
      capabilities: ["vision", "tool_calling", "json_mode", "long_context"],
    },
    {
      model_id: "gpt-4o",
      label: "GPT-4o",
      context_window: 128000,
      price_in_per_m: 2.5,
      price_out_per_m: 10,
      capabilities: ["vision", "tool_calling", "json_mode", "long_context"],
    },
  ],
  anthropic: [
    {
      model_id: "claude-3-5-sonnet-20241022",
      label: "Claude 3.5 Sonnet",
      context_window: 200000,
      price_in_per_m: null,
      price_out_per_m: null,
      capabilities: ["vision", "long_context"],
    },
  ],
  gemini: [
    {
      model_id: "gemini-1.5-pro",
      label: "Gemini 1.5 Pro",
      context_window: 1048576,
      price_in_per_m: null,
      price_out_per_m: null,
      capabilities: ["long_context"],
    },
  ],
};

function providerSlug(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "provider";
}

function uniqueProviderId(label: string): string {
  const base = providerSlug(label);
  if (!fixtureProviders.some((p) => p.id === base)) return base;
  let i = 2;
  while (fixtureProviders.some((p) => p.id === `${base}-${i}`)) i += 1;
  return `${base}-${i}`;
}

function uniqueProviderModelId(providerId: string, modelId: string, label?: string | null): string {
  const stem = providerSlug(label?.trim() || modelId);
  const base = `prov-${providerId}-${stem}`;
  if (!fixtureCatalogueSnapshot().some((m) => m.id === base)) return base;
  let i = 2;
  while (fixtureCatalogueSnapshot().some((m) => m.id === `${base}-${i}`)) i += 1;
  return `${base}-${i}`;
}

export async function listProviderPresets(): Promise<ProviderPreset[]> {
  if (isLive()) return apiGet<ProviderPreset[]>("/api/providers/presets");
  await fixtureDelay();
  return PROVIDER_PRESETS_FIXTURE.map((p) => ({ ...p }));
}

export async function listProviders(): Promise<ProviderInfo[]> {
  if (isLive()) return apiGet<ProviderInfo[]>("/api/providers");
  await fixtureDelay();
  return fixtureProviders.map((p) => ({ ...p }));
}

export async function createProvider(body: ProviderCreate): Promise<ProviderMutationResult> {
  if (isLive()) return apiSend<ProviderMutationResult>("POST", "/api/providers", body);
  await fixtureDelay();
  if (!body.label.trim()) throw new ApiError("label is empty", 400);
  if (!body.base_url.trim()) throw new ApiError("base_url is empty", 400);
  if (!body.api_key.trim()) throw new ApiError("api_key is empty", 400);
  const id = uniqueProviderId(body.label);
  const provider: ProviderInfo = {
    id,
    label: body.label.trim(),
    base_url: body.base_url.trim().replace(/\/+$/, ""),
    kind: body.kind,
    secret_name: `provider_${id}`,
    has_key: true,
  };
  fixtureProviders = [...fixtureProviders, provider];
  return { provider: { ...provider }, catalogue_ok: true, catalogue_error: null };
}

export async function updateProvider(
  id: string,
  patch: ProviderPatch,
): Promise<ProviderMutationResult> {
  if (isLive())
    return apiSend<ProviderMutationResult>("PUT", `/api/providers/${encodeURIComponent(id)}`, patch);
  await fixtureDelay();
  const existing = fixtureProviders.find((p) => p.id === id);
  if (!existing) throw new ApiError(`unknown provider ${id}`, 404);
  const next: ProviderInfo = {
    ...existing,
    label: patch.label?.trim() || existing.label,
    base_url: patch.base_url?.trim().replace(/\/+$/, "") || existing.base_url,
    kind: patch.kind ?? existing.kind,
    has_key: patch.api_key !== undefined ? !!patch.api_key.trim() : existing.has_key,
  };
  fixtureProviders = fixtureProviders.map((p) => (p.id === id ? next : p));
  return { provider: { ...next }, catalogue_ok: true, catalogue_error: null };
}

export async function deleteProvider(id: string): Promise<void> {
  if (isLive()) return apiSend<void>("DELETE", `/api/providers/${encodeURIComponent(id)}`);
  await fixtureDelay();
  const provider = fixtureProviders.find((p) => p.id === id);
  if (!provider) throw new ApiError(`unknown provider ${id}`, 404);
  const refs = fixtureCatalogueSnapshot().filter((m) => m.api_key_env === provider.secret_name);
  if (refs.length > 0) {
    throw new ApiError(
      `Provider is still used by catalogue models: ${refs.map((m) => m.id).join(", ")}`,
      409,
    );
  }
  fixtureProviders = fixtureProviders.filter((p) => p.id !== id);
}

export async function listProviderModels(providerId: string): Promise<ProviderCatalogueModel[]> {
  if (isLive())
    return apiGet<ProviderCatalogueModel[]>(
      `/api/providers/${encodeURIComponent(providerId)}/models`,
    );
  await fixtureDelay();
  const provider = fixtureProviders.find((p) => p.id === providerId);
  if (!provider) throw new ApiError(`unknown provider ${providerId}`, 404);
  return (FIXTURE_PROVIDER_CATALOGUE[provider.kind] ?? []).map((m) => ({
    ...m,
    capabilities: [...m.capabilities],
  }));
}

export async function enableProviderModel(
  providerId: string,
  body: ProviderEnableBody,
): Promise<ModelInfo[]> {
  if (isLive())
    return apiSend<ModelInfo[]>(
      "POST",
      `/api/providers/${encodeURIComponent(providerId)}/enable`,
      body,
    );
  await fixtureDelay();
  const provider = fixtureProviders.find((p) => p.id === providerId);
  if (!provider) throw new ApiError(`unknown provider ${providerId}`, 404);
  const modelId = body.model_id.trim();
  if (!modelId) throw new ApiError("model_id is empty", 400);
  const existing = fixtureCatalogueSnapshot().find(
    (m) => m.api_key_env === provider.secret_name && m.model_id === modelId,
  );
  if (existing) return fixtureCatalogueSnapshot().map((m) => ({ ...m }));
  const catalogue = FIXTURE_PROVIDER_CATALOGUE[provider.kind] ?? [];
  const live = catalogue.find((m) => m.model_id === modelId);
  const upsert: ModelUpsert = {
    id: uniqueProviderModelId(provider.id, modelId, body.label ?? live?.label),
    model_id: modelId,
    base_url: provider.base_url,
    api_key_env: provider.secret_name,
    context_window: live?.context_window ?? 8192,
    quantization: null,
    capabilities: live?.capabilities ?? [],
    // Provider metadata is advisory; only the explicit Settings control creates
    // a manual pin that can override later runtime detection.
    vision: null,
    price_in_per_m: live?.price_in_per_m ?? 0,
    price_out_per_m: live?.price_out_per_m ?? 0,
  };
  return appendFixtureCatalogueEntry(toModelInfo(upsert));
}

export async function disableProviderModel(
  providerId: string,
  catalogueId: string,
): Promise<ModelInfo[]> {
  if (isLive())
    return apiSend<ModelInfo[]>(
      "DELETE",
      `/api/providers/${encodeURIComponent(providerId)}/enable/${encodeURIComponent(catalogueId)}`,
    );
  const provider = fixtureProviders.find((p) => p.id === providerId);
  if (!provider) throw new ApiError(`unknown provider ${providerId}`, 404);
  const model = fixtureCatalogueSnapshot().find((m) => m.id === catalogueId);
  if (model?.api_key_env !== provider.secret_name) {
    throw new ApiError(`${catalogueId} does not belong to provider ${providerId}`, 400);
  }
  return deleteModel(catalogueId);
}
