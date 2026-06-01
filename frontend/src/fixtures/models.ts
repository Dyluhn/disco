import type { ModelAssignments, ModelInfo } from "@/types/models";

/**
 * The assignable model catalogue + the starting assignments — placeholder values
 * mirroring the backend's `default_config()` (llm-router contract §7). The UI is
 * built against these first; swap for the live `/api/models` + router config when
 * wired. Local models are free; the one OpenRouter model is the paid overflow,
 * present in the catalogue as an *assignable* choice (nothing routes to it
 * automatically — the operator assigns it).
 */
export const MODEL_CATALOGUE: ModelInfo[] = [
  {
    id: "driver-local",
    label: "Local Driver — Qwen 35B-A3B",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: ["tool_calling", "json_mode", "long_context"],
    note: "Q4_K_M · the default lead",
  },
  {
    id: "driver-overflow",
    label: "Frontier — Claude (OpenRouter)",
    provider: "openrouter",
    price_in_per_m: 3,
    price_out_per_m: 15,
    capabilities: ["tool_calling", "json_mode", "long_context", "vision"],
    note: "paid · assign deliberately",
  },
  {
    id: "rag-local",
    label: "Local RAG — Llama 3.1 8B",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: ["json_mode"],
  },
  {
    id: "rewriter-local",
    label: "Local Rewriter — Llama 3.2 3B",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: [],
  },
  {
    id: "summarizer-local",
    label: "Local Summarizer — Qwen 1.7B",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: [],
  },
  {
    id: "nli-local",
    label: "Local Cross-Encoder — DeBERTa",
    provider: "local",
    price_in_per_m: 0,
    price_out_per_m: 0,
    capabilities: [],
    note: "off the LLM path",
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
