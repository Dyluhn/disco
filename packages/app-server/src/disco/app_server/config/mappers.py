"""Pure RouterConfig↔DTO mappers for the app-server settings surface.

Extracted from config_state.py (god-file decomposition, Wave 1). Pure helper
functions that derive the wire DTOs from the core `RouterConfig` (llm-router
contract §7) and build core entities back from upsert DTOs. No instance state,
no ConfigState coupling. Clean layering: this imports the leaf `dtos` module and
is itself imported by the stateful `config_state.ConfigState` orchestrator.
"""

from __future__ import annotations

from disco.core.llm import ModelRole, RouterConfig
from disco.core.llm.config import ModelEntry
from disco.core.llm.types import Requirement

from .dtos import (
    AssignmentsDTO,
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    ModelDTO,
    ModelUpsert,
    OpenRouterModelDTO,
    ProjectStorageConfigDTO,
    SandboxConfigDTO,
    TtsConfigDTO,
)


def _humanize(key: str) -> str:
    return key.replace("-", " ").title()


def _provider_view(entry: ModelEntry) -> str:
    # The frontend distinguishes only local (free) vs paid; derive it from price so
    # a user-added paid model reads correctly regardless of its endpoint key.
    return "openrouter" if (entry.price_in_per_m > 0 or entry.price_out_per_m > 0) else "local"


def _model_name(model_id: str) -> str:
    """The displayable model name: drop a path prefix ('anthropic/claude-x' ->
    'claude-x') and a '.gguf' suffix ('Qwen3.6-27B...gguf' -> 'Qwen3.6-27B...')."""
    return model_id.rsplit("/", 1)[-1].removesuffix(".gguf")


def _endpoint_host(base_url: str | None) -> str | None:
    """'http://192.168.1.231:18080/v1' -> '192.168.1.231:18080' (None if unset)."""
    if not base_url:
        return None
    return base_url.split("://", 1)[-1].split("/", 1)[0]


def _note(entry) -> str:
    """A quiet provenance caption from REAL config: context window, quant, endpoint
    — so the settings catalogue reflects what's actually deployed, not seed labels."""
    bits: list[str] = []
    ctx = entry.context_window
    bits.append(f"{ctx // 1000}K ctx" if ctx >= 1000 else f"{ctx} ctx")
    if entry.quantization:
        bits.append(entry.quantization)
    host = _endpoint_host(entry.base_url)
    if host:
        bits.append(host)
    return " · ".join(bits)


def _models_from(config: RouterConfig) -> list[ModelDTO]:
    out: list[ModelDTO] = []
    for key, entry in config.models.items():
        # Label carries the REAL model behind the role slot (e.g. "Driver Local —
        # Qwen3.6-27B-UD-Q5_K_XL") rather than just the humanized key, so the UI
        # never looks like seed data.
        out.append(
            ModelDTO(
                id=key,
                label=f"{_humanize(key)} — {_model_name(entry.model_id)}",
                provider=_provider_view(entry),
                price_in_per_m=entry.price_in_per_m,
                price_out_per_m=entry.price_out_per_m,
                capabilities=sorted(r.value for r in entry.capabilities),
                note=_note(entry),
                model_id=entry.model_id,
                base_url=entry.base_url,
                api_key_env=entry.api_key_env,
                context_window=entry.context_window,
                quantization=entry.quantization,
            )
        )
    return out


def normalize_openrouter(data: list[dict]) -> list[OpenRouterModelDTO]:
    """Map the raw OpenRouter /models payload to our DTO. Pricing is USD per token
    → ×1e6 for per-Mtok; capabilities come from supported_parameters + modalities."""
    out: list[OpenRouterModelDTO] = []
    for m in data:
        pricing = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        params = m.get("supported_parameters") or []
        ctx = int(m.get("context_length") or 0)
        caps: list[str] = []
        if "image" in (arch.get("input_modalities") or []):
            caps.append("vision")
        if "tools" in params:
            caps.append("tool_calling")
        if "response_format" in params:
            caps.append("json_mode")
        if ctx >= 32_000:
            caps.append("long_context")

        def _price(v: object) -> float:
            try:
                return float(str(v)) * 1_000_000  # OR prices are USD-per-token strings
            except (TypeError, ValueError):
                return 0.0

        out.append(
            OpenRouterModelDTO(
                id=str(m.get("id")),
                name=str(m.get("name") or m.get("id")),
                context_length=ctx,
                price_in_per_m=_price(pricing.get("prompt")),
                price_out_per_m=_price(pricing.get("completion")),
                capabilities=caps,
            )
        )
    return out


def _capabilities(values: list[str]) -> frozenset[Requirement]:
    """Map capability strings to the typed Requirement set, ignoring unknowns
    (advisory metadata — a bad value shouldn't reject the model)."""
    out: set[Requirement] = set()
    for v in values:
        try:
            out.add(Requirement(v))
        except ValueError:
            continue
    return frozenset(out)


def _entry_from(upsert: ModelUpsert, *, provider: str) -> ModelEntry:
    """Build a ModelEntry from an upsert. `provider` is the endpoint key — the
    model's own id for a new model, or the existing entry's key on edit (so the
    endpoint grouping of seeded models is preserved)."""
    return ModelEntry(
        model_id=upsert.model_id,
        provider=provider,
        context_window=upsert.context_window,
        capabilities=_capabilities(upsert.capabilities),
        quantization=upsert.quantization,
        price_in_per_m=upsert.price_in_per_m,
        price_out_per_m=upsert.price_out_per_m,
        base_url=upsert.base_url,
        api_key_env=upsert.api_key_env,
    )


# Roles NOT assignable to a catalogue LLM in the Settings matrix:
#  - AGENT_DRIVER follows `default_model` (+ the per-conversation pick), shown separately.
#  - NLI_VERIFIER is an ENCODER (cross-encoder), bundled in-process / remote via the
#    Encoders setting — it does NOT route through the LLM model assignments, so
#    surfacing it as an assignable LLM role would be a false affordance.
_NON_ASSIGNABLE_ROLES = frozenset({ModelRole.AGENT_DRIVER, ModelRole.NLI_VERIFIER})


def _assignments_from(config: RouterConfig) -> AssignmentsDTO:
    roles = {
        role.value: config.assignments.get(role, config.default_model)
        for role in ModelRole
        if role not in _NON_ASSIGNABLE_ROLES
    }
    return AssignmentsDTO(default_model=config.default_model, roles=roles)


def _encoders_from(config: RouterConfig) -> EncodersConfigDTO:
    e = config.encoders
    return EncodersConfigDTO(
        remote=e.remote,
        reranker_url=e.reranker_url,
        embedder_url=e.embedder_url,
        nli_url=e.nli_url,
    )


def _tts_from(config: RouterConfig) -> TtsConfigDTO:
    t = config.tts
    return TtsConfigDTO(
        enabled=t.enabled,
        provider=t.provider,
        base_url=t.base_url,
        api_key_env=t.api_key_env,
        model=t.model,
        voice_a=t.voice_a,
        voice_b=t.voice_b,
    )


def _data_sources_from(config: RouterConfig) -> DataSourcesConfigDTO:
    s, x = config.search, config.extraction
    return DataSourcesConfigDTO(
        search_provider=s.provider,
        search_base_url=s.base_url,
        search_api_key_env=s.api_key_env,
        extraction_provider=x.provider,
        extraction_base_url=x.base_url,
        extraction_api_key_env=x.api_key_env,
    )


def _sandbox_from(config: RouterConfig) -> SandboxConfigDTO:
    s = config.sandbox
    return SandboxConfigDTO(
        backend=s.backend,
        docker_socket=s.docker_socket,
        podman_url=s.podman_url,
        runtime=s.runtime,
        image=s.image,
        workspace_root=s.workspace_root,
    )


def _projects_from(config: RouterConfig) -> ProjectStorageConfigDTO:
    """Wire DTO for the Build-project storage path. The `status` derives from
    the live `validate_root` check so the UI sees the truth at GET time
    (a path saved yesterday could be missing today if the user deleted it).

    When `projects_root` is empty (fresh install / unset), the effective path
    is computed and auto-created by :func:`resolve_projects_root`; the DTO then
    carries that resolved path on `effective_root` so the UI can show where
    data will live even with the input left blank, while `projects_root` stays
    as "" to preserve the "use the auto default" semantics. `status` is ``ok``
    rather than ``unset`` when the auto-default resolves successfully."""
    from disco.tools.projects import resolve_projects_root, validate_root

    p = config.projects
    effective = resolve_projects_root(p.projects_root)
    return ProjectStorageConfigDTO(
        projects_root=p.projects_root,  # preserve what the user explicitly saved
        status=validate_root(effective).value,
        effective_root=effective,  # the real directory in use (auto-created if "")
    )


def _mcp_live_status(approval: dict | None) -> str:
    """Derive the live status for a server. In the app-server projection:
    'connected' = approved exists; 'disconnected' = no approval yet.
    The agent-server is what actually dials and may surface 'error'."""
    return "connected" if approval else "disconnected"
