import { DEFAULT_ASSIGNMENTS, MODEL_CATALOGUE } from "@/fixtures/models";
import { ApiError } from "./client";
import type {
  AssignmentsPatch,
  DataSourcesConfig,
  EncodersConfig,
  ModelAssignments,
  ModelInfo,
  ModelUpsert,
  OpenRouterKeyStatus,
  OpenRouterModel,
  TtsConfig,
} from "@/types/models";
import type { SandboxConfig } from "@/types/sandbox";
import { apiGet, apiSend, fixtureDelay, isLive } from "./client";

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
  const humanized = u.id.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
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
  podman_url: "http+ssh://sandbox@100.73.110.47/run/user/1000/podman/podman.sock",
  runtime: "runc",
  image: "pmx-sandbox:base",
  workspace_root: "/opt/sandbox/workspaces",
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

// ---- data sources (search + extraction provider tiers) ---------------------

let fixtureDataSources: DataSourcesConfig = {
  search_provider: "ddgs",
  search_base_url: "",
  search_api_key_env: "",
  extraction_provider: "local",
  extraction_base_url: "",
  extraction_api_key_env: "",
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

// ---- OpenRouter ------------------------------------------------------------

// A tiny offline catalogue so the browse UI works without a backend (tests).
const FIXTURE_OPENROUTER: OpenRouterModel[] = [
  { id: "anthropic/claude-3.5-sonnet", name: "Anthropic: Claude 3.5 Sonnet", context_length: 200000, price_in_per_m: 3, price_out_per_m: 15, capabilities: ["tool_calling", "json_mode", "long_context", "vision"] },
  { id: "openai/gpt-4o", name: "OpenAI: GPT-4o", context_length: 128000, price_in_per_m: 2.5, price_out_per_m: 10, capabilities: ["tool_calling", "json_mode", "long_context", "vision"] },
  { id: "meta-llama/llama-3.3-70b-instruct", name: "Meta: Llama 3.3 70B Instruct", context_length: 131072, price_in_per_m: 0.12, price_out_per_m: 0.3, capabilities: ["tool_calling", "long_context"] },
];
let fixtureOrKey: OpenRouterKeyStatus = { configured: false, locked: false, can_store: true };

/** The live OpenRouter catalogue (hundreds of models), proxied by the app-server. */
export async function listOpenRouterModels(): Promise<OpenRouterModel[]> {
  if (isLive()) return apiGet<OpenRouterModel[]>("/api/models/openrouter");
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
    api_key_env: "PMX_OPENROUTER_API_KEY",
    context_window: m.context_length,
    quantization: null,
    capabilities: [...m.capabilities],
    price_in_per_m: m.price_in_per_m,
    price_out_per_m: m.price_out_per_m,
  };
}
