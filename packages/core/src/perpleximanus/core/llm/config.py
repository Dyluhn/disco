"""Router configuration — llm-router-contract.md §7.

All `[VERIFY]` specifics (model ids, context sizes, prices, thresholds, family
tags) live HERE — one config surface (BoD §19), editable without touching any
caller. This is the single file you revise when models or prices change (R10).

The starting role assignments (§7) are encoded as a `default_config()` factory
with **placeholder** model ids — deliberately NOT the operator's real homelab
model ids or OpenRouter strings, which are [VERIFY] and must be filled in at
wiring time. The *shape* is fixed; the values are placeholders.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .types import ModelRole, Requirement


class ModelEntry(BaseModel):
    model_id: str  # provider's id string [VERIFY]
    provider: str  # "ollama"|"llamacpp"|"openrouter"
    context_window: int  # [VERIFY]
    capabilities: frozenset[Requirement] = frozenset()
    quantization: str | None = None  # e.g. "Q4_K_M"; informational provenance
    # per-million-token prices for cost accounting; 0 for local. [VERIFY]
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    # [EXTENSION] §8 requires a model family for prompt selection but §7's
    # ModelEntry omitted the field. Optional here: if None, the family is
    # derived from model_id (prompts.derive_family). Set it to pin a [VERIFY]
    # family tag explicitly.
    family: str | None = None


class RoleRouting(BaseModel):
    primary: str  # ModelEntry key for the local default
    overflow: str | None = None  # ModelEntry key for the OpenRouter path
    overflow_eligible: bool = False  # may this role overflow at all? (§5.1)
    overflow_ladder: list[str] = Field(default_factory=list)  # stronger models (§5.3)


class RouterConfig(BaseModel):
    models: dict[str, ModelEntry]  # key -> entry
    roles: dict[ModelRole, RoleRouting]
    # overflow policy thresholds (§5.1) — all tunable, none hardcoded in code
    local_retry_before_overflow: int = 1
    errors_before_overflow: int = 2
    confidence_floor: float = 0.5
    # cost governance (§5.2)
    soft_budget_usd_per_conversation: float | None = None
    hard_budget_usd_per_conversation: float | None = None
    soft_budget_usd_global_daily: float | None = None
    hard_budget_usd_global_daily: float | None = None

    def entry_for(self, key: str) -> ModelEntry:
        return self.models[key]


def default_config() -> RouterConfig:
    """The §7 starting role assignments with PLACEHOLDER ids ([VERIFY] at build).

    Replace `model_id`/`provider`/prices/context windows with the operator's
    real values when wiring the live adapters; nothing else changes (R10).
    """
    models = {
        # AGENT_DRIVER: local 70B/35B-class + a frontier overflow.
        "driver-local": ModelEntry(
            model_id="PLACEHOLDER-local-driver",
            provider="ollama",
            context_window=65_536,
            capabilities=frozenset(
                {Requirement.TOOL_CALLING, Requirement.JSON_MODE, Requirement.LONG_CONTEXT}
            ),
            quantization="Q4_K_M",
            family="qwen",
        ),
        "driver-overflow": ModelEntry(
            model_id="PLACEHOLDER-frontier-driver",
            provider="openrouter",
            context_window=200_000,
            capabilities=frozenset(
                {
                    Requirement.TOOL_CALLING,
                    Requirement.JSON_MODE,
                    Requirement.LONG_CONTEXT,
                    Requirement.VISION,
                }
            ),
            family="anthropic",
            price_in_per_m=3.0,
            price_out_per_m=15.0,
        ),
        "rag-local": ModelEntry(
            model_id="PLACEHOLDER-local-rag",
            provider="ollama",
            context_window=32_768,
            capabilities=frozenset({Requirement.JSON_MODE}),
            family="llama",
        ),
        "rewriter-local": ModelEntry(
            model_id="PLACEHOLDER-local-rewriter",
            provider="ollama",
            context_window=8_192,
            family="llama",
        ),
        "summarizer-local": ModelEntry(
            model_id="PLACEHOLDER-local-summarizer",
            provider="ollama",
            context_window=16_384,
            family="qwen",
        ),
        "nli-local": ModelEntry(
            model_id="PLACEHOLDER-local-cross-encoder",
            provider="local",
            context_window=512,
            family="deberta",
        ),
    }
    roles = {
        ModelRole.AGENT_DRIVER: RoleRouting(
            primary="driver-local", overflow="driver-overflow", overflow_eligible=True
        ),
        ModelRole.RAG_ANSWERER: RoleRouting(primary="rag-local", overflow_eligible=False),
        ModelRole.QUERY_REWRITER: RoleRouting(primary="rewriter-local", overflow_eligible=False),
        ModelRole.SUMMARIZER: RoleRouting(primary="summarizer-local", overflow_eligible=False),
        ModelRole.NLI_VERIFIER: RoleRouting(primary="nli-local", overflow_eligible=False),
    }
    return RouterConfig(models=models, roles=roles)
