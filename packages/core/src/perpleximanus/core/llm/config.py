"""Router configuration — llm-router-contract.md §7 (v1.2 deterministic).

All `[VERIFY]` specifics (model ids, context sizes, prices, family tags) live
HERE — one config surface (BoD §19), editable without touching any caller. This
is the single file you revise when models or prices change (R10).

v1.2 LOBOTOMY: model selection is now ABSOLUTE and config-driven. A role resolves
to exactly one model by direct lookup — `default_model` for the driver, an
explicit `assignments[role]` for every other role, optionally overridden per
conversation by the model pill. There is no overflow policy, no difficulty
assessment, no capability-based escalation: the mapping IS the source of truth
and the router obeys it without deviation (see `model_for`). The intelligent-
routing scaffolding (`RoleRouting`, threshold fields) is retained DORMANT for the
documented revival path.

The starting assignments are encoded as a `default_config()` factory with
**placeholder** model ids — deliberately NOT the operator's real homelab model
ids or OpenRouter strings, which are [VERIFY] and filled in at wiring time.
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
    """[DORMANT — v1.2] The intelligent-routing per-role bundle. No longer read
    by the deterministic router (a role now maps to ONE model via
    `RouterConfig.assignments`/`default_model`). Retained as the revival
    scaffolding referenced by the dormant policy and the v1.2 contract appendix."""

    primary: str  # ModelEntry key for the local default
    overflow: str | None = None  # ModelEntry key for the OpenRouter path
    overflow_eligible: bool = False  # may this role overflow at all? (§5.1)
    overflow_ladder: list[str] = Field(default_factory=list)  # stronger models (§5.3)


class RouterConfig(BaseModel):
    models: dict[str, ModelEntry]  # key -> entry (the assignable catalogue)
    # v1.2 deterministic assignment — the source of truth (R10):
    default_model: str  # AGENT_DRIVER's model + fallback for any unassigned role
    assignments: dict[ModelRole, str] = Field(default_factory=dict)  # explicit per-role
    # cost governance (§5.2) — PASSIVE spend backstop in v1.2: it observes spend
    # and refuses to exceed a HARD cap, but it no longer influences which model is
    # chosen (selection is `assignments`-driven, full stop).
    soft_budget_usd_per_conversation: float | None = None
    hard_budget_usd_per_conversation: float | None = None
    soft_budget_usd_global_daily: float | None = None
    hard_budget_usd_global_daily: float | None = None

    # === INTELLIGENT ROUTING (DORMANT) — see policy.py / routing.py ============
    # The intelligent router consumed per-role `RoleRouting` (primary + overflow +
    # eligibility + ladder) PLUS the thresholds below to decide local-vs-frontier
    # escalation. v1.2 lobotomized selection to deterministic assignment, so these
    # inputs are no longer read. Kept commented for the documented revival path
    # (llm-router-contract.md v1.2 appendix). To revive: restore these fields,
    # re-enable the policy in policy.py, and switch routing.py's `_resolve` back to
    # the intelligent body preserved there.
    #     roles: dict[ModelRole, RoleRouting]
    #     local_retry_before_overflow: int = 1
    #     errors_before_overflow: int = 2
    #     confidence_floor: float = 0.5
    # === END DORMANT ==========================================================

    def model_for(self, role: ModelRole, *, override: str | None = None) -> str:
        """Deterministic role -> model-key resolution (v1.2). Precedence:
        per-conversation `override` (the main-screen model pill) > explicit
        settings `assignments[role]` > `default_model`. No policy, no capability
        escalation — the mapping is obeyed without deviation. Returns a KEY into
        `models`; `entry_for` resolves it (and fails loud if it is unknown)."""
        if override is not None:
            return override
        return self.assignments.get(role, self.default_model)

    def entry_for(self, key: str) -> ModelEntry:
        return self.models[key]


def default_config() -> RouterConfig:
    """The v1.2 starting ASSIGNMENTS with PLACEHOLDER ids ([VERIFY] at build).

    Every role maps to exactly one model. `default_model` ("driver-local") leads
    the show for AGENT_DRIVER (and is the fallback for any unassigned role); the
    other roles are pinned explicitly. "driver-overflow" stays in the catalogue
    as an *assignable* frontier model — the operator may select it per role in
    Settings or per conversation via the model pill — but nothing routes to it
    automatically. Replace `model_id`/`provider`/prices/context windows with the
    operator's real values when wiring the live adapters; nothing else changes.
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
    assignments = {
        # AGENT_DRIVER intentionally omitted: it resolves to `default_model`.
        ModelRole.RAG_ANSWERER: "rag-local",
        ModelRole.QUERY_REWRITER: "rewriter-local",
        ModelRole.SUMMARIZER: "summarizer-local",
        ModelRole.NLI_VERIFIER: "nli-local",
    }
    return RouterConfig(
        models=models,
        default_model="driver-local",
        assignments=assignments,
    )
