import { DEFAULT_ASSIGNMENTS, MODEL_CATALOGUE } from "@/fixtures/models";
import { ApiError } from "./client";
import type {
  AssignmentsPatch,
  BuildKernelConfig,
  DataSourcesConfig,
  EncodersConfig,
  ImageGenConfig,
  LiveBrowserConfig,
  ModelAssignments,
  ModelInfo,
  ModelUpsert,
  OpenRouterKeyStatus,
  OpenRouterModel,
  ProviderCatalogueModel,
  ProviderCreate,
  ProviderEnableBody,
  ProviderInfo,
  ProviderMutationResult,
  ProviderPatch,
  ProviderPreset,
  RoleFallbackConfig,
  TtsConfig,
} from "@/types/models";
import type { SandboxConfig, SandboxHealth } from "@/types/sandbox";
import type { ProbeResult } from "@/types/probe";
import { agentGet, agentLive, agentSend, apiGet, apiSend, fixtureDelay, isLive } from "./client";

/**
 * Data-access layer for the model catalogue + assignments (the absolute, manual
 * model story). Components NEVER call this directly — only hooks (useModels,
 * useAssignments, useUpdateAssignments) do, per the data-flow discipline.
 *
 * Live (VITE_API_BASE set) → the app-server endpoints GET /api/models,
 * GET/PUT /api/models/assignments (api-endpoints.md). Otherwise → the in-repo
 * fixture (a session-scoped in-memory copy; no browser storage). The DTO shapes
 * are identical to the frontend types, so live JSON maps straight through.
 */

// In-memory, session-scoped state for the FIXTURE path (deep copies so mutations
// don't leak into the fixture constants).
let fixtureAssignments: ModelAssignments = {
  default_model: DEFAULT_ASSIGNMENTS.default_model,
  roles: { ...DEFAULT_ASSIGNMENTS.roles },
};
let fixtureCatalogue: ModelInfo[] = MODEL_CATALOGUE.map((m) => ({ ...m }));

/** Derive the display fields for a fixture-path upsert, mirroring the backend. */
function toModelInfo(u: ModelUpsert): ModelInfo {
  // W-04: drop the `or-` OpenRouter prefix BEFORE humanizing so the label doesn't
  // gain a bogus "Or " word (mirrors the backend mapper's _display_key).
  const humanized = u.id
    .replace(/^or-/, "")
    .replace(/-/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  const name = u.model_id.split("/").pop()!.replace(/\.gguf$/, "");
  const paid = u.price_in_per_m > 0 || u.price_out_per_m > 0;
  const ctx = u.context_window >= 1000 ? `${Math.floor(u.context_window / 1000)}K ctx` : `${u.context_window} ctx`;
  const host = u.base_url ? u.base_url.replace(/^https?:\/\//, "").split("/")[0] : null;
  const note = [ctx, u.quantization, host].filter(Boolean).join(" · ");
  return {
    id: u.id,
    label: `${humanized} — ${name}`,
    provider: paid ? "openrouter" : "local",
    price_in_per_m: u.price_in_per_m,
    price_out_per_m: u.price_out_per_m,
    // W-05: derive when unset (subscription must be explicit — it has a 0 per-token
    // price, so it can't be inferred); carry an explicit mode through verbatim.
    pricing_mode: u.pricing_mode ?? (paid ? "metered" : "free"),
    capabilities: [...u.capabilities],
    note,
    model_id: u.model_id,
    base_url: u.base_url ?? null,
    api_key_env: u.api_key_env ?? null,
    context_window: u.context_window,
    quantization: u.quantization ?? null,
  };
}

export async function listModels(): Promise<ModelInfo[]> {
  if (isLive()) return apiGet<ModelInfo[]>("/api/models");
  await fixtureDelay();
  return fixtureCatalogue.map((m) => ({ ...m }));
}

/** Add a model to the catalogue. */
export async function createModel(upsert: ModelUpsert): Promise<ModelInfo[]> {
  if (isLive()) return apiSend<ModelInfo[]>("POST", "/api/models", upsert);
  await fixtureDelay();
  if (fixtureCatalogue.some((m) => m.id === upsert.id)) {
    throw new ApiError(`model ${upsert.id} already exists`, 400);
  }
  fixtureCatalogue = [...fixtureCatalogue, toModelInfo(upsert)];
  return fixtureCatalogue.map((m) => ({ ...m }));
}

/** Edit an existing catalogue model (id is immutable). */
export async function updateModel(id: string, upsert: ModelUpsert): Promise<ModelInfo[]> {
  if (isLive()) return apiSend<ModelInfo[]>("PUT", `/api/models/${encodeURIComponent(id)}`, upsert);
  await fixtureDelay();
  if (!fixtureCatalogue.some((m) => m.id === id)) throw new ApiError(`unknown model ${id}`, 400);
  fixtureCatalogue = fixtureCatalogue.map((m) => (m.id === id ? toModelInfo(upsert) : m));
  return fixtureCatalogue.map((m) => ({ ...m }));
}

/** Remove a model — refused if it's the default or assigned to a role. */
export async function deleteModel(id: string): Promise<ModelInfo[]> {
  if (isLive()) return apiSend<ModelInfo[]>("DELETE", `/api/models/${encodeURIComponent(id)}`);
  await fixtureDelay();
  if (id === fixtureAssignments.default_model) {
    throw new ApiError(`${id} is the default model — reassign the default first`, 400);
  }
  const role = Object.entries(fixtureAssignments.roles).find(([, k]) => k === id)?.[0];
  if (role) throw new ApiError(`${id} is assigned to ${role} — reassign first`, 400);
  fixtureCatalogue = fixtureCatalogue.filter((m) => m.id !== id);
  return fixtureCatalogue.map((m) => ({ ...m }));
}

export async function getAssignments(): Promise<ModelAssignments> {
  if (isLive()) return apiGet<ModelAssignments>("/api/models/assignments");
  await fixtureDelay();
  return {
    default_model: fixtureAssignments.default_model,
    roles: { ...fixtureAssignments.roles },
  };
}

/**
 * Apply a partial assignment change. Assignments are ABSOLUTE — the system uses
 * exactly what is set; there is no validation/prediction here (capabilities are
 * advisory + fail-loud at runtime, not blocked in the UI).
 */
export async function updateAssignments(patch: AssignmentsPatch): Promise<ModelAssignments> {
  if (isLive()) return apiSend<ModelAssignments>("PUT", "/api/models/assignments", patch);
  await fixtureDelay();
  fixtureAssignments = {
    default_model: patch.default_model ?? fixtureAssignments.default_model,
    roles: { ...fixtureAssignments.roles, ...(patch.roles ?? {}) },
  };
  return {
    default_model: fixtureAssignments.default_model,
    roles: { ...fixtureAssignments.roles },
  };
}

// ---- sandbox backend -------------------------------------------------------

let fixtureSandbox: SandboxConfig = {
  backend: "local",
  docker_socket: "unix:///var/run/docker.sock",
  podman_url: "unix:///run/user/1000/podman/podman.sock",
  runtime: "runc",
  image: "disco-sandbox:base",
  workspace_root: "/var/lib/disco/workspaces",
};

export async function getSandboxConfig(): Promise<SandboxConfig> {
  if (isLive()) return apiGet<SandboxConfig>("/api/sandbox/config");
  await fixtureDelay();
  return { ...fixtureSandbox };
}

export async function updateSandboxConfig(cfg: SandboxConfig): Promise<SandboxConfig> {
  if (isLive()) return apiSend<SandboxConfig>("PUT", "/api/sandbox/config", cfg);
  await fixtureDelay();
  fixtureSandbox = { ...cfg };
  return { ...fixtureSandbox };
}

/** W-48 — connectivity preflight for a sandbox backend. Probed on the AGENT-server,
 * which owns the sandbox environment (the app-server has no container socket in a
 * split-container deploy, so its probe would falsely fail). Real, bounded probe →
 * a typed host-naming verdict (never throws for an expected failure). The fixture
 * path returns a synthetic "reachable". */
export async function testSandbox(cfg: SandboxConfig): Promise<ProbeResult> {
  if (agentLive()) return agentSend<ProbeResult>("POST", "/api/sandbox/test", cfg);
  await fixtureDelay();
  return { ok: true, status: "ok", detail: `${cfg.backend} sandbox is reachable.` };
}

/** Reachability of the ACTIVE (persisted) sandbox backend — the cheap signal the app
 * shell polls to surface an unreachable sandbox BEFORE a doomed run. Probed on the
 * AGENT-server (which owns the sandbox environment), against the SAME service the run
 * path builds, so the banner and a real run agree. The fixture path returns a synthetic
 * "reachable" (dev = process backend). */
export async function getSandboxHealth(): Promise<SandboxHealth> {
  if (agentLive()) return agentGet<SandboxHealth>("/api/sandbox/health");
  await fixtureDelay();
  return { reachable: true, backend: fixtureSandbox.backend, detail: "" };
}

// ---- encoders (bundled-local vs remote) ------------------------------------

let fixtureEncoders: EncodersConfig = { remote: false };

export async function getEncodersConfig(): Promise<EncodersConfig> {
  if (isLive()) return apiGet<EncodersConfig>("/api/encoders/config");
  await fixtureDelay();
  return { ...fixtureEncoders };
}

export async function updateEncodersConfig(cfg: EncodersConfig): Promise<EncodersConfig> {
  if (isLive()) return apiSend<EncodersConfig>("PUT", "/api/encoders/config", cfg);
  await fixtureDelay();
  fixtureEncoders = { ...cfg };
  return { ...fixtureEncoders };
}

// ---- TTS (audio-overview toggle / bundled-vs-remote / voices) --------------

let fixtureTts: TtsConfig = {
  enabled: true,
  provider: "bundled",
  base_url: "",
  api_key_env: "",
  model: "",
  voice_a: "af_heart",
  voice_b: "af_bella",
};

export async function getTtsConfig(): Promise<TtsConfig> {
  if (isLive()) return apiGet<TtsConfig>("/api/tts/config");
  await fixtureDelay();
  return { ...fixtureTts };
}

export async function updateTtsConfig(cfg: TtsConfig): Promise<TtsConfig> {
  if (isLive()) return apiSend<TtsConfig>("PUT", "/api/tts/config", cfg);
  await fixtureDelay();
  fixtureTts = { ...cfg };
  return { ...fixtureTts };
}

// ---- image generation (comfyui / openai / openrouter provider tiers) -------

let fixtureImageGen: ImageGenConfig = {
  provider: "openrouter",
  base_url: "",
  api_key_env: "",
  model: "",
};

export async function getImageGenConfig(): Promise<ImageGenConfig> {
  if (isLive()) return apiGet<ImageGenConfig>("/api/image-gen/config");
  await fixtureDelay();
  return { ...fixtureImageGen };
}

export async function updateImageGenConfig(cfg: ImageGenConfig): Promise<ImageGenConfig> {
  if (isLive()) return apiSend<ImageGenConfig>("PUT", "/api/image-gen/config", cfg);
  await fixtureDelay();
  fixtureImageGen = { ...cfg };
  return { ...fixtureImageGen };
}

// ---- data sources (search + extraction provider tiers) ---------------------

let fixtureDataSources: DataSourcesConfig = {
  search_provider: "ddgs",
  search_base_url: "",
  search_api_key_env: "",
  extraction_provider: "local",
  extraction_base_url: "",
  extraction_api_key_env: "",
  configured_sources: [],
};

export async function getDataSourcesConfig(): Promise<DataSourcesConfig> {
  if (isLive()) return apiGet<DataSourcesConfig>("/api/data-sources/config");
  await fixtureDelay();
  return { ...fixtureDataSources };
}

export async function updateDataSourcesConfig(cfg: DataSourcesConfig): Promise<DataSourcesConfig> {
  if (isLive()) return apiSend<DataSourcesConfig>("PUT", "/api/data-sources/config", cfg);
  await fixtureDelay();
  fixtureDataSources = { ...cfg };
  return { ...fixtureDataSources };
}

// ---- role fallback (auxiliary role resilience) ----------------------------

let fixtureRoleFallback: RoleFallbackConfig = {
  enabled: false,
  base_url: "",
  model: "",
  api_key_env: "",
};

export async function getRoleFallbackConfig(): Promise<RoleFallbackConfig> {
  if (isLive()) return apiGet<RoleFallbackConfig>("/api/role-fallback/config");
  await fixtureDelay();
  return { ...fixtureRoleFallback };
}

export async function updateRoleFallbackConfig(
  cfg: RoleFallbackConfig,
): Promise<RoleFallbackConfig> {
  if (isLive()) return apiSend<RoleFallbackConfig>("PUT", "/api/role-fallback/config", cfg);
  await fixtureDelay();
  if (cfg.enabled && (!cfg.base_url.trim() || !cfg.model.trim())) {
    throw new ApiError("Role fallback needs both a base URL and model when enabled.", 400);
  }
  fixtureRoleFallback = { ...cfg };
  return { ...fixtureRoleFallback };
}

// ---- T4.2/T4.3/T4.4 live provider probes -----------------------------------

/** T4.2 — reachability of the configured search/extraction endpoint (app-server). */
export async function testDataSource(kind: "search" | "extraction"): Promise<ProbeResult> {
  return apiSend<ProbeResult>("POST", `/api/data-sources/${kind}/test`);
}

/** T4.3 — synthesize a one-word clip via the configured TTS tier (agent-server). */
export async function testTts(): Promise<ProbeResult> {
  return agentSend<ProbeResult>("POST", "/api/tts/test");
}

/** T4.4 — one tiny generation via the configured image tier; reports NOT-CONFIGURED
 * honestly when no real backend is set up (W-50) (agent-server). */
export async function testImageGen(): Promise<ProbeResult> {
  return agentSend<ProbeResult>("POST", "/api/image-gen/test");
}

// ---- OpenRouter ------------------------------------------------------------

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
    price_in_per_m: m.price_in_per_m,
    price_out_per_m: m.price_out_per_m,
  };
}

// ---- generic providers -----------------------------------------------------

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
  if (!fixtureCatalogue.some((m) => m.id === base)) return base;
  let i = 2;
  while (fixtureCatalogue.some((m) => m.id === `${base}-${i}`)) i += 1;
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
  const refs = fixtureCatalogue.filter((m) => m.api_key_env === provider.secret_name);
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
  const existing = fixtureCatalogue.find(
    (m) => m.api_key_env === provider.secret_name && m.model_id === modelId,
  );
  if (existing) return fixtureCatalogue.map((m) => ({ ...m }));
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
    price_in_per_m: live?.price_in_per_m ?? 0,
    price_out_per_m: live?.price_out_per_m ?? 0,
  };
  fixtureCatalogue = [...fixtureCatalogue, toModelInfo(upsert)];
  return fixtureCatalogue.map((m) => ({ ...m }));
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
  const model = fixtureCatalogue.find((m) => m.id === catalogueId);
  if (model?.api_key_env !== provider.secret_name) {
    throw new ApiError(`${catalogueId} does not belong to provider ${providerId}`, 400);
  }
  return deleteModel(catalogueId);
}

// ---- live browser (noVNC toggle) -------------------------------------------

let fixtureLiveBrowser: LiveBrowserConfig = { enabled: false };

export async function getLiveBrowserConfig(): Promise<LiveBrowserConfig> {
  if (isLive()) return apiGet<LiveBrowserConfig>("/api/live-browser/config");
  await fixtureDelay();
  return { ...fixtureLiveBrowser };
}

export async function updateLiveBrowserConfig(cfg: LiveBrowserConfig): Promise<LiveBrowserConfig> {
  if (isLive()) return apiSend<LiveBrowserConfig>("PUT", "/api/live-browser/config", cfg);
  await fixtureDelay();
  fixtureLiveBrowser = { ...cfg };
  return { ...fixtureLiveBrowser };
}

// ---- vestigial build kernel config -----------------------------------------

let fixtureBuildKernel: BuildKernelConfig = { kind: "disco" };

export async function getBuildKernelConfig(): Promise<BuildKernelConfig> {
  if (isLive()) return apiGet<BuildKernelConfig>("/api/build-kernel/config");
  await fixtureDelay();
  return { ...fixtureBuildKernel };
}

export async function updateBuildKernelConfig(
  cfg: Pick<BuildKernelConfig, "kind">,
): Promise<BuildKernelConfig> {
  if (isLive()) return apiSend<BuildKernelConfig>("PUT", "/api/build-kernel/config", cfg);
  await fixtureDelay();
  fixtureBuildKernel = { ...fixtureBuildKernel, kind: cfg.kind };
  return { ...fixtureBuildKernel };
}
