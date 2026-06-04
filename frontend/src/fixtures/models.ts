import type { ModelAssignments, ModelInfo } from "@/types/models";

/**
 * The assignable model catalogue + starting assignments — mirrors the backend's
 * `default_config()` (llm-router contract §7) so the OFFLINE/test catalogue looks
 * like the real deployment, not placeholder seed data. When VITE_API_BASE is set,
 * the live `/api/models` (read from the persisted config) replaces this entirely.
 */
const QWEN = "http://192.168.1.231:18080/v1";
const GEMMA = "http://192.168.1.81:8087/v1";

export const MODEL_CATALOGUE: ModelInfo[] = [
  {
    id: "driver-local",
    label: "Driver Local — Qwen3.6-27B-UD-Q5_K_XL",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: ["tool_calling", "json_mode", "long_context"],
    note: "131K ctx · Q5_K_XL · 192.168.1.231:18080",
    model_id: "Qwen3.6-27B-UD-Q5_K_XL.gguf",
    base_url: QWEN,
    context_window: 131072,
    quantization: "Q5_K_XL",
  },
  {
    id: "rag-local",
    label: "Rag Local — Qwen3.6-27B-UD-Q5_K_XL",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: ["json_mode", "long_context"],
    note: "131K ctx · Q5_K_XL · 192.168.1.231:18080",
    model_id: "Qwen3.6-27B-UD-Q5_K_XL.gguf",
    base_url: QWEN,
    context_window: 131072,
    quantization: "Q5_K_XL",
  },
  {
    id: "rewriter-local",
    label: "Rewriter Local — gemma-4-e2b-mtp",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: ["json_mode"],
    note: "32K ctx · 192.168.1.81:8087",
    model_id: "gemma-4-e2b-mtp",
    base_url: GEMMA,
    api_key_env: "PMX_GEMMA_API_KEY",
    context_window: 32768,
  },
  {
    id: "summarizer-local",
    label: "Summarizer Local — gemma-4-e2b-mtp",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: [],
    note: "32K ctx · 192.168.1.81:8087",
    model_id: "gemma-4-e2b-mtp",
    base_url: GEMMA,
    api_key_env: "PMX_GEMMA_API_KEY",
    context_window: 32768,
  },
  {
    id: "nli-local",
    label: "Nli Local — bge-reranker-v2-m3",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: [],
    note: "512 ctx · off the LLM path",
    model_id: "bge-reranker-v2-m3",
    context_window: 512,
  },
  {
    id: "driver-overflow",
    label: "Driver Overflow — claude-3.5-sonnet",
    provider: "openrouter",
    price_in_per_m: 3,
    price_out_per_m: 15,
    capabilities: ["tool_calling", "json_mode", "long_context", "vision"],
    note: "200K ctx · openrouter.ai · paid, assign deliberately",
    model_id: "anthropic/claude-3.5-sonnet",
    base_url: "https://openrouter.ai/api/v1",
    api_key_env: "PMX_OPENROUTER_API_KEY",
    context_window: 200000,
  },
];

export const DEFAULT_ASSIGNMENTS: ModelAssignments = {
  default_model: "driver-local",
  roles: {
    rag_answerer: "rag-local",
    query_rewriter: "rewriter-local",
    summarizer: "summarizer-local",
    nli_verifier: "nli-local",
  },
};
