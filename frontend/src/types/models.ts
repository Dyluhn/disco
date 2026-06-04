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

/** The non-driver roles that get explicit per-role selectors in Settings. */
export type AssignableRole = Exclude<ModelRole, "agent_driver">;

export interface RoleMeta {
  id: AssignableRole;
  label: string;
  description: string;
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
    description: "Condenses context — a cheap, separate model.",
  },
  {
    id: "nli_verifier",
    label: "NLI verifier",
    description: "Checks each claim's entailment against its cited passage.",
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
