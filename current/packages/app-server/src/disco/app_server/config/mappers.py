"""Pure RouterConfig↔DTO mappers for the app-server settings surface.

Extracted from config_state.py (god-file decomposition, Wave 1). Pure helper
functions that derive the wire DTOs from the core `RouterConfig` (llm-router
contract §7) and build core entities back from upsert DTOs. No instance state,
no ConfigState coupling. Clean layering: this imports the leaf `dtos` module and
is itself imported by the stateful `config_state.ConfigState` orchestrator.
"""

from __future__ import annotations

from typing import Literal

from disco.core.llm import ModelRole, RouterConfig
from disco.core.llm.config import ModelEntry
from disco.core.llm.types import Requirement
from disco.core.llm.vision_table import resolve_vision_status

from .dtos import (
    AssignmentsDTO,
    BuildKernelConfigDTO,
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    ImageGenConfigDTO,
    LiveBrowserConfigDTO,
    ModelDTO,
    ModelUpsert,
    OpenRouterModelDTO,
    ProjectStorageConfigDTO,
    RoleFallbackConfigDTO,
    SandboxConfigDTO,
    SandboxConnectionDTO,
    TtsConfigDTO,
)


def _humanize(key: str) -> str:
    return key.replace("-", " ").title()


def _display_key(key: str) -> str:
    """Humanize the catalogue key for the model LABEL, dropping the `or-` OpenRouter
    prefix FIRST (W-04). Stripping before humanizing keeps `.title()` from turning the
    bare `or` into a bogus 'Or ' word — so `or-gpt-4-turbo` reads 'Gpt 4 Turbo …', not
    'Or Gpt 4 Turbo …'. Non-`or-` keys (e.g. 'driver-local') are unaffected."""
    return _humanize(key.removeprefix("or-"))


def _display_label_key(key: str, entry: ModelEntry) -> str:
    """Provider-enabled entries carry ids like `prov-<provider>-<model>` for
    uniqueness; showing that raw ("Prov Opencode Go Deepseek V4 Flash") leaks
    plumbing into the label. Strip the provider scaffolding — the provider is
    already visible via the host chip — and humanize just the model part."""
    if entry.api_key_env and entry.api_key_env.startswith("provider_"):
        slug = entry.api_key_env.removeprefix("provider_")
        return _humanize(key.removeprefix("prov-").removeprefix(f"{slug}-"))
    return _display_key(key)


def _pricing_mode(entry: ModelEntry) -> Literal["metered", "subscription", "free", "unknown"]:
    """Resolve the effective pay model for the cost surfaces (W-05). An explicit
    `entry.pricing_mode` wins; otherwise derive for back-compat: any price → metered,
    else free. Always a concrete value on the wire so the frontend never has to guess
    'subscription' (which is NOT derivable from price — it has a 0 per-token price)."""
    if entry.pricing_mode is not None:
        return entry.pricing_mode
    return "metered" if (entry.price_in_per_m > 0 or entry.price_out_per_m > 0) else "free"


def _provider_view(entry: ModelEntry) -> str:
    # The frontend distinguishes only local (free) vs paid; the paid bucket is keyed
    # as "openrouter" (the grouped "Overflow — paid" header). A SUBSCRIPTION tier (W-05)
    # is a PAID flat plan whose per-token price is 0, so deriving purely from price would
    # wrongly collapse it into the "local/free" group — group it as paid. Otherwise fall
    # back to price so a user-added paid model reads correctly regardless of its key.
    if entry.pricing_mode == "subscription":
        return "openrouter"
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


def _positive_int(value: object) -> int | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _note(entry) -> str:
    """A quiet provenance caption from REAL config: context window, quant, endpoint
    — so the settings catalogue reflects what's actually deployed, not seed labels."""
    if not entry.base_url:
        return "not configured - add endpoint base URL"
    bits: list[str] = []
    ctx = entry.context_window
    bits.append(f"{ctx // 1000}K ctx" if ctx >= 1000 else f"{ctx} ctx")
    if entry.quantization:
        bits.append(entry.quantization)
    host = _endpoint_host(entry.base_url)
    if host:
        bits.append(host)
    return " · ".join(bits)


def _vision_status(entry: ModelEntry) -> Literal["vision", "text-only", "unknown"]:
    """Present only states the persisted settings authority can prove."""
    return resolve_vision_status(
        model_id=entry.model_id,
        family=entry.family,
        explicit=entry.vision,
        live_probe=entry.vision_probe,
        provider_declared=(
            entry.vision_declared
            if entry.vision_declared is not None
            else (Requirement.VISION in entry.capabilities or None)
        ),
    )


def _models_from(config: RouterConfig) -> list[ModelDTO]:
    out: list[ModelDTO] = []
    for key, entry in config.models.items():
        # Label carries the REAL model behind the role slot (e.g. "Driver Local —
        # Qwen3.6-27B-UD-Q5_K_XL") rather than just the humanized key, so the UI
        # never looks like seed data.
        out.append(
            ModelDTO(
                id=key,
                label=f"{_display_label_key(key, entry)} — {_model_name(entry.model_id)}",
                provider=_provider_view(entry),
                price_in_per_m=entry.price_in_per_m,
                price_out_per_m=entry.price_out_per_m,
                pricing_mode=_pricing_mode(entry),
                capabilities=sorted(r.value for r in entry.capabilities),
                vision=entry.vision,
                vision_status=_vision_status(entry),
                note=_note(entry),
                model_id=entry.model_id,
                base_url=entry.base_url,
                api_key_env=entry.api_key_env,
                requires_api_key=entry.requires_api_key,
                context_window=entry.context_window,
                max_output_tokens=entry.max_output_tokens,
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
        top_provider = m.get("top_provider") or {}
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
                max_output_tokens=_positive_int(
                    top_provider.get("max_completion_tokens")
                    if isinstance(top_provider, dict)
                    else None
                ),
                price_in_per_m=_price(pricing.get("prompt")),
                price_out_per_m=_price(pricing.get("completion")),
                capabilities=caps,
                vision_status=(
                    "vision"
                    if "image" in (arch.get("input_modalities") or [])
                    else (
                        "text-only"
                        if arch.get("input_modalities")
                        else "unknown"
                    )
                ),
                image_output="image" in (arch.get("output_modalities") or []),
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


def _entry_from(
    upsert: ModelUpsert,
    *,
    provider: str,
    existing: ModelEntry | None = None,
) -> ModelEntry:
    """Build a ModelEntry from an upsert. `provider` is the endpoint key — the
    model's own id for a new model, or the existing entry's key on edit (so the
    endpoint grouping of seeded models is preserved)."""
    vision = upsert.vision
    if "vision" not in upsert.model_fields_set and existing is not None:
        vision = existing.vision
    capabilities = _capabilities(upsert.capabilities)
    if vision is True:
        capabilities = capabilities | {Requirement.VISION}
    elif vision is False:
        capabilities = capabilities - {Requirement.VISION}
    return ModelEntry(
        model_id=upsert.model_id,
        provider=provider,
        context_window=upsert.context_window,
        max_output_tokens=upsert.max_output_tokens,
        capabilities=capabilities,
        quantization=upsert.quantization,
        price_in_per_m=upsert.price_in_per_m,
        price_out_per_m=upsert.price_out_per_m,
        pricing_mode=upsert.pricing_mode,  # W-05: carry the pay model through edits
        base_url=upsert.base_url,
        api_key_env=upsert.api_key_env,
        requires_api_key=(
            upsert.requires_api_key if existing is None else existing.requires_api_key
        ),
        vision=vision,
        vision_declared=(
            upsert.vision_declared
            if "vision_declared" in upsert.model_fields_set
            else (existing.vision_declared if existing is not None else None)
        ),
        # These are not editable on this surface; retain their architectural
        # meaning instead of erasing them on an unrelated catalogue edit.
        family=existing.family if existing is not None else None,
        tier=existing.tier if existing is not None else None,
    )


# Roles NOT assignable to a catalogue LLM in the Settings matrix:
#  - AGENT_DRIVER follows `default_model` (+ the per-conversation pick), shown separately.
#  - NLI_VERIFIER is an ENCODER (cross-encoder), bundled in-process / remote via the
#    Encoders setting — it does NOT route through the LLM model assignments, so
#    surfacing it as an assignable LLM role would be a false affordance.
#  - VERIFIER is a bounded artifact judge wired by the finish gate. It has a
#    config assignment, but is not part of the ordinary generation matrix.
_NON_ASSIGNABLE_ROLES = frozenset(
    {ModelRole.AGENT_DRIVER, ModelRole.NLI_VERIFIER, ModelRole.VERIFIER}
)


def _assignments_from(config: RouterConfig) -> AssignmentsDTO:
    roles = {
        role.value: config.assignments.get(role, config.default_model)
        for role in ModelRole
        if role not in _NON_ASSIGNABLE_ROLES
    }
    return AssignmentsDTO(
        default_model=config.default_model,
        roles=roles,
        vision_model=config.vision_escalation_model,
    )


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


def _image_gen_from(config: RouterConfig) -> ImageGenConfigDTO:
    i = config.image_gen
    return ImageGenConfigDTO(
        provider=i.provider,
        base_url=i.base_url,
        api_key_env=i.api_key_env,
        model=i.model,
        workflow_json=i.workflow_json,
    )


def _data_sources_from(
    config: RouterConfig, *, configured_sources: list[str] | None = None
) -> DataSourcesConfigDTO:
    s, x = config.search, config.extraction
    return DataSourcesConfigDTO(
        search_provider=s.provider,
        search_base_url=s.base_url,
        search_api_key_env=s.api_key_env,
        extraction_provider=x.provider,
        extraction_base_url=x.base_url,
        extraction_api_key_env=x.api_key_env,
        configured_sources=configured_sources or [],
    )


def _role_fallback_from(config: RouterConfig) -> RoleFallbackConfigDTO:
    fallback = config.role_fallback
    return RoleFallbackConfigDTO(
        enabled=fallback.enabled,
        base_url=fallback.base_url,
        model=fallback.model,
        api_key_env=fallback.api_key_env,
    )


def _sandbox_from(config: RouterConfig) -> SandboxConfigDTO:
    from disco.core.llm import default_connection_for

    s = config.sandbox

    def _conn_dto(c) -> SandboxConnectionDTO:
        return SandboxConnectionDTO(
            docker_socket=c.docker_socket,
            podman_url=c.podman_url,
            runtime=c.runtime,
            image=c.image,
            workspace_root=c.workspace_root,
        )

    # Expose EVERY backend's connection block so the UI restores a backend's OWN setup on
    # switch, never the previous backend's socket (P1 bleed fix). Priority:
    #   1. SAVED per-backend blocks (authoritative).
    #   2. The ACTIVE backend ALWAYS reflects the live flat fields — this migrates an OLD
    #      flat config (no `connections` map) by mapping the flat block to connections[
    #      active] ONLY (it is NOT replicated to other backends).
    #   3. CLEAN backend-appropriate defaults for every other known backend with no saved
    #      block — so switching to Local/podman/process gets a clean slate, not a gvisor
    #      ssh:// socket carried over.
    connections = {bid: _conn_dto(c) for bid, c in s.connections.items()}
    connections[s.backend] = _conn_dto(s.active_connection())  # migrate flat → active only
    for bid in ("gvisor", "local", "podman", "process"):
        connections.setdefault(bid, _conn_dto(default_connection_for(bid)))
    return SandboxConfigDTO(
        backend=s.backend,
        docker_socket=s.docker_socket,
        podman_url=s.podman_url,
        runtime=s.runtime,
        image=s.image,
        workspace_root=s.workspace_root,
        connections=connections,
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


def _live_browser_from(config: RouterConfig) -> LiveBrowserConfigDTO:
    """Wire DTO for the live-browser toggle. Mirrors LiveBrowserSettings."""
    lb = config.live_browser
    return LiveBrowserConfigDTO(enabled=lb.enabled)


def _build_kernel_from(config: RouterConfig) -> BuildKernelConfigDTO:
    """Wire DTO for the vestigial Build kernel setting."""
    return BuildKernelConfigDTO(kind=config.build_kernel)


def _mcp_live_status(approval: dict | None) -> str:
    """Derive the live status for a server. In the app-server projection:
    'connected' = approved exists; 'disconnected' = no approval yet.
    The agent-server is what actually dials and may surface 'error'."""
    return "connected" if approval else "disconnected"
