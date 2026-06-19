/**
 * UI model types — mirror the backend's deterministic model story (llm-router
 * contract v1.3): a catalogue of assignable models + absolute per-role
 * assignments. Capabilities are ADVISORY metadata shown to inform assignment;
 * cost is surfaced everywhere a model is chosen. There is NO automatic routing.
 */

export type ModelProvider = "local" | "openrouter";

export type Capability = "vision" | "long_context" | "tool_calling" | "json_mode";

export const CAPABILITY_LABEL: Record<Capability, string> = {
  vision: "Vision",
  long_context: "Long context",
  tool_calling: "Tool calling",
  json_mode: "JSON mode",
};

export interface ModelInfo {
  id: string; // catalogue key, e.g. "driver-local"
  label: string; // human name shown in the UI
  provider: ModelProvider; // local (free) vs openrouter (paid overflow)
  /** per-million-token prices; both 0 for local. */
  price_in_per_m: number;
  price_out_per_m: number;
  capabilities: Capability[];
  /** optional provenance note (e.g. quantization) shown as a quiet caption. */
  note?: string;
  // raw editable fields (mirror the backend ModelDTO) so an edit form prefills the
  // real config, not the display view.
  model_id: string;
  base_url?: string | null;
  api_key_env?: string | null;
  context_window: number;
  quantization?: string | null;
}

/** [CONTRACT] provider-neutral usage metrics (llm-router contract §7). */
export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  cached_tokens?: number;
  cost_usd?: number;
}

/** One model from the live OpenRouter catalogue (mirrors the backend DTO). */
export interface OpenRouterModel {
  id: string; // slug, becomes the model's model_id
  name: string;
  context_length: number;
  price_in_per_m: number;
  price_out_per_m: number;
  capabilities: Capability[];
}

/** Status of the encrypted-at-rest OpenRouter API key. */
export interface OpenRouterKeyStatus {
  configured: boolean; // an encrypted key is stored
  locked: boolean; // stored but not decryptable (PMX_SECRET_KEY missing/wrong)
  can_store: boolean; // PMX_SECRET_KEY present, so a key can be saved
}

/** Create/edit payload for a catalogue model (mirrors the backend ModelUpsert). */
export interface ModelUpsert {
  id: string;
  model_id: string;
  base_url?: string | null;
  api_key_env?: string | null;
  context_window: number;
  quantization?: string | null;
  capabilities: Capability[];
  price_in_per_m: number;
  price_out_per_m: number;
}

/** True for free local models — drives the "free" vs "$/Mtok" cost legibility. */
export function isFree(m: ModelInfo): boolean {
  return m.provider === "local" || (m.price_in_per_m === 0 && m.price_out_per_m === 0);
}

/** The five roles the router assigns (llm-router contract §7). */
export type ModelRole =
  | "agent_driver"
  | "rag_answerer"
  | "query_rewriter"
  | "summarizer"
  | "nli_verifier";

/** Roles that get explicit per-role LLM selectors in Settings. AGENT_DRIVER
 * follows the default + per-conversation pick; NLI_VERIFIER is an ENCODER
 * (bundled in-process / remote via the Encoders setting), NOT an LLM-router role,
 * so it isn't assignable here. */
export type AssignableRole = Exclude<ModelRole, "agent_driver" | "nli_verifier">;

export interface RoleMeta {
  id: AssignableRole;
  label: string;
  description: string;
}

/** Where the non-generative encoders (embeddings / rerank / NLI) run — the wire
 * mirror of the app-server's EncodersConfigDTO. `remote=false` = bundled
 * in-process (ONNX/CPU); `remote=true` = external LAN endpoints. The endpoint URLs
 * are editable when Remote is selected; an empty one falls back to the server's
 * env default. */
export interface EncodersConfig {
  remote: boolean;
  reranker_url?: string;
  embedder_url?: string;
  nli_url?: string;
}

/** Audio-overview TTS (RP-09) — the wire mirror of the app-server's TtsConfigDTO.
 * The universal three-tier provider pattern: `provider` = "bundled" (in-process
 * Kokoro, keyless default) | "speaches" (self-hosted OpenAI-compatible
 * /v1/audio/speech via `base_url`, keyless) | "openai" (paid OpenAI-compatible via
 * `base_url` + `api_key_env` naming the secret + `model`). `enabled=false` turns the
 * feature off AND frees the model's RAM. Voices default to af_heart/af_bella. */
export interface TtsConfig {
  enabled: boolean;
  provider: "bundled" | "speaches" | "openai";
  base_url?: string;
  api_key_env?: string;
  model?: string;
  voice_a?: string;
  voice_b?: string;
}

/** Image generation provider — the wire mirror of the app-server's ImageGenConfigDTO
 * (the universal three-tier pattern). `procedural` (bundled Pillow, keyless default) |
 * `comfyui` (self-hosted graph API via `base_url`, keyless) | `openai` (paid
 * OpenAI-compatible /v1/images/generations via `base_url` + `api_key_env` naming the
 * secret, never the key itself). */
export interface ImageGenConfig {
  provider: "procedural" | "comfyui" | "openai";
  base_url?: string;
  api_key_env?: string;
  /** openai: image model id (e.g. "gpt-image-1"); comfyui: checkpoint filename. */
  model?: string;
}

/** Universal web-data providers (§B). Each slot has three tiers; the bundled
 * defaults (ddgs / local) need no key. `*_api_key_env` is the NAME of an env var
 * holding a paid key — never the key itself. Mirror of DataSourcesConfigDTO. */
export type SearchProvider = "ddgs" | "searxng" | "tavily" | "brave";
export type ExtractionProvider = "local" | "crawl4ai" | "firecrawl";
export interface DataSourcesConfig {
  search_provider: SearchProvider;
  search_base_url: string;
  search_api_key_env: string;
  extraction_provider: ExtractionProvider;
  extraction_base_url: string;
  extraction_api_key_env: string;
}

/**
 * The non-driver roles get explicit per-role selectors in Settings; AGENT_DRIVER
 * is the "default primary" (and the main-screen leader pill overrides it per
 * conversation). Ordered for display.
 */
export const ROLES: RoleMeta[] = [
  {
    id: "rag_answerer",
    label: "RAG answerer",
    description: "Synthesizes the grounded answer from retrieved passages.",
  },
  {
    id: "query_rewriter",
    label: "Query rewriter",
    description: "Expands and decomposes the question for retrieval.",
  },
  {
    id: "summarizer",
    label: "Summarizer",
    description: "Condenses context on a long run.",
  },
];

/**
 * The absolute assignment set. `default_model` leads AGENT_DRIVER (and is the
 * fallback); `roles` pins every other function. Mirrors RouterConfig.
 */
export interface ModelAssignments {
  default_model: string; // AGENT_DRIVER's model id
  roles: Record<AssignableRole, string>;
}

/** A partial change to the assignments (one slot at a time). */
export interface AssignmentsPatch {
  default_model?: string;
  roles?: Partial<Record<AssignableRole, string>>;
}

/** Live browser (noVNC) toggle — wire mirror of LiveBrowserSettings. Off by default.
 * When enabled, a "Live" toggle appears on the Agent canvas browser pane. */
export interface LiveBrowserConfig {
  enabled: boolean;
}
