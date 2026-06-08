"""Config + scaffold state for the app-server (settings surface, BoD §5.2).

The model catalogue + assignments are derived from the core `RouterConfig`
(llm-router contract §7) — the one source of truth for the deterministic, manual
model story. Skills and MCP are in-memory scaffolds (wiring-pending) mirroring the
frontend fixtures until their subsystems land. All state is session-scoped
(process memory); swap for a persisted config store when wiring beyond demo.

The DTO shapes are the wire contract the frontend's data layer consumes — they
mirror the frontend's `src/types/models.ts` + `src/types/config.ts`.
"""

from __future__ import annotations

from typing import Literal

from perpleximanus.core import SkillStore
from perpleximanus.core.llm import ConfigStore, ModelRole, RouterConfig, SecretStore
from perpleximanus.core.llm.config import ModelEntry
from perpleximanus.core.llm.types import Requirement
from pydantic import BaseModel

# ---- wire DTOs (mirror the frontend types) ----------------------------------


class ModelDTO(BaseModel):
    id: str
    label: str
    provider: str  # derived view: "local" (free) | "openrouter" (paid)
    price_in_per_m: float
    price_out_per_m: float
    capabilities: list[str]
    note: str | None = None
    # raw editable fields (so the edit form prefills the real config, not a view):
    model_id: str
    base_url: str | None = None
    api_key_env: str | None = None
    context_window: int
    quantization: str | None = None


class ModelUpsert(BaseModel):
    """Create/edit a catalogue model. `id` is the catalogue key (immutable on
    edit). The endpoint key is derived (= id for new models), so the user only
    thinks in terms of a model + its endpoint, never an internal provider key."""

    id: str
    model_id: str
    base_url: str | None = None
    api_key_env: str | None = None
    context_window: int = 8192
    quantization: str | None = None
    capabilities: list[str] = []
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0


class OpenRouterModelDTO(BaseModel):
    """One model from the live OpenRouter catalogue, normalized to the fields the
    Add flow needs (the slug `id` becomes the model's model_id; prices are /Mtok)."""

    id: str  # slug, e.g. "anthropic/claude-3.5-sonnet"
    name: str
    context_length: int
    price_in_per_m: float
    price_out_per_m: float
    capabilities: list[str]


class OpenRouterKeyStatus(BaseModel):
    configured: bool  # an encrypted key is stored
    locked: bool  # stored but not decryptable (PMX_SECRET_KEY missing/wrong)
    can_store: bool  # PMX_SECRET_KEY present, so a new key can be encrypted + saved


class OpenRouterKeyBody(BaseModel):
    key: str


class AssignmentsDTO(BaseModel):
    default_model: str
    roles: dict[str, str]  # {rag_answerer, query_rewriter, summarizer, nli_verifier} -> model id


class AssignmentsPatch(BaseModel):
    default_model: str | None = None
    roles: dict[str, str] | None = None


class SandboxConfigDTO(BaseModel):
    """The active sandbox backend + its (non-secret) connection — the wire mirror of
    core's SandboxSettings. Persisted in the shared ConfigStore; the agent-server maps
    it to the live backend. No secrets (remote connections are keyless Tailscale SSH)."""

    backend: str  # "process" | "gvisor" | "local" | "podman"
    docker_socket: str
    podman_url: str
    runtime: str
    image: str
    workspace_root: str


class EncodersConfigDTO(BaseModel):
    """Where the non-generative encoders (embeddings / rerank / NLI) run. `remote=False`
    (default) = BUNDLED in-process (ONNX/CPU); `remote=True` = the external LAN
    endpoints. The three endpoint URLs are UI-editable when Remote is selected and
    persisted; an empty one falls back to the PMX_*_URL env default. The wire mirror
    of core's EncodersSettings — the agent-server honors it on the next research run.
    NOT an LLM-router role assignment."""

    remote: bool
    reranker_url: str = ""
    embedder_url: str = ""
    nli_url: str = ""


class DataSourcesConfigDTO(BaseModel):
    """The universal web-data providers (§B). Each slot has three tiers; the bundled
    defaults (`ddgs` / `local`) need no key. `api_key_env` is the NAME of the env var
    holding a paid key (never the key itself). The wire mirror of core's
    SearchSettings + ExtractionSettings."""

    search_provider: Literal["ddgs", "searxng", "tavily", "brave"] = "ddgs"
    search_base_url: str = ""
    search_api_key_env: str = ""
    extraction_provider: Literal["local", "crawl4ai", "firecrawl"] = "local"
    extraction_base_url: str = ""
    extraction_api_key_env: str = ""


class ProjectStorageConfigDTO(BaseModel):
    """Where Build projects persist on the app host — the user-chosen directory.

    Wire mirror of core's `ProjectStorageSettings`. The `status` field is derived
    on the GET path so the UI knows immediately whether the saved path is valid
    (ok / unset / not_found / not_a_directory / not_writable); on PUT, the path
    is validated server-side and a 400 with a typed reason is returned for any
    non-OK status."""

    projects_root: str = ""
    status: str = "unset"  # informational; populated by the GET path


class SkillDTO(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool
    body: str = ""  # the markdown instructions handed to the agent


class SkillPatch(BaseModel):
    """Partial update for an existing skill. Any field omitted is left unchanged."""

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    body: str | None = None


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    body: str = ""
    enabled: bool = True


class McpConnectionDTO(BaseModel):
    id: str
    name: str
    url: str
    status: str  # "connected" | "disconnected" | "error"


# ---- derivation from the core RouterConfig ----------------------------------


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
    (a path saved yesterday could be missing today if the user deleted it)."""
    from perpleximanus.tools.projects import validate_root

    p = config.projects
    return ProjectStorageConfigDTO(
        projects_root=p.projects_root,
        status=validate_root(p.projects_root).value,
    )


class ConfigValidationError(Exception):
    """Raised by ConfigState.update_projects_config when the candidate path
    fails validation. The endpoint maps this to a 400 with a typed `reason`."""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# ---- the session-scoped config state ----------------------------------------


class ConfigState:
    """Holds the live config the settings surface reads/writes. The catalogue
    (add/edit/remove a model) AND the per-role assignments are mutable and PERSISTED
    via a shared ConfigStore so the agent-server runtime actually honors them.
    Skills/MCP are wiring-pending scaffolds.

    `store` is the shared config store (defaults to PMX_CONFIG). `config` only seeds
    the catalogue when no store is supplied (test convenience)."""

    def __init__(
        self,
        config: RouterConfig | None = None,
        *,
        store: ConfigStore | None = None,
        secrets: SecretStore | None = None,
        skills: SkillStore | None = None,
    ) -> None:
        if store is not None:
            self._store = store
        elif config is not None:
            self._store = ConfigStore(base_factory=lambda: config)
        else:
            self._store = ConfigStore()
        self._secrets = secrets or SecretStore()
        # Real, persistent skills (.md files under PMX_SKILLS_DIR). Replaces the
        # old fixture list — skills now survive restarts and feed the agent.
        self._skills_store = skills or SkillStore()
        self._mcp: list[McpConnectionDTO] = [
            McpConnectionDTO(
                id="fs", name="Filesystem", url="stdio://mcp-server-filesystem", status="connected"
            ),
            McpConnectionDTO(
                id="gh", name="GitHub", url="https://mcp.github.local", status="disconnected"
            ),
        ]

    # models + assignments (the absolute manual model story) ------------------

    def models(self) -> list[ModelDTO]:
        return _models_from(self._store.load())

    def add_model(self, upsert: ModelUpsert) -> list[ModelDTO]:
        """Add a model to the catalogue. New models are their own endpoint (the
        endpoint key = the catalogue id). Raises ValueError on a duplicate id."""
        entry = _entry_from(upsert, provider=upsert.id)
        return _models_from(self._store.add_model(upsert.id, entry))

    def update_model(self, model_id: str, upsert: ModelUpsert) -> list[ModelDTO]:
        """Edit an existing model. Preserves its endpoint key so a seeded model
        sharing a backend isn't silently split off. Raises ValueError if missing."""
        existing = self._store.load().models.get(model_id)
        provider = existing.provider if existing is not None else model_id
        entry = _entry_from(upsert, provider=provider)
        return _models_from(self._store.update_model(model_id, entry))

    def remove_model(self, model_id: str) -> list[ModelDTO]:
        """Remove a model. Raises ValueError if it's the default or assigned to a
        role (the user must reassign first — we never silently break routing)."""
        return _models_from(self._store.remove_model(model_id))

    # openrouter key (encrypted at rest) -------------------------------------

    def openrouter_key_status(self) -> OpenRouterKeyStatus:
        return OpenRouterKeyStatus(
            configured=self._secrets.has_openrouter_key(),
            locked=self._secrets.locked,
            can_store=self._secrets.can_store,
        )

    def set_openrouter_key(self, key: str) -> OpenRouterKeyStatus:
        """Encrypt + persist. Raises ValueError if PMX_SECRET_KEY isn't set (no app
        secret to encrypt with) or the key is blank — mapped to 400 by the wire."""
        if not key.strip():
            raise ValueError("key is empty")
        try:
            self._secrets.set_openrouter_key(key.strip())
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        return self.openrouter_key_status()

    def clear_openrouter_key(self) -> OpenRouterKeyStatus:
        self._secrets.clear_openrouter_key()
        return self.openrouter_key_status()

    def assignments(self) -> AssignmentsDTO:
        return _assignments_from(self._store.load())

    def update_assignments(self, patch: AssignmentsPatch) -> AssignmentsDTO:
        """Persist the patch over the current assignments. Raises ValueError on an
        unknown model key (the wire layer maps that to 400)."""
        cfg = self._store.load()
        default_model = patch.default_model or cfg.default_model
        assignments = dict(cfg.assignments)
        if patch.roles:
            for role_str, key in patch.roles.items():
                assignments[ModelRole(role_str)] = key  # ValueError on a bad role
        new_cfg = self._store.save_assignments(default_model, assignments)
        return _assignments_from(new_cfg)

    # sandbox backend (persisted; the agent-server maps it to the live backend) ----

    def sandbox_config(self) -> SandboxConfigDTO:
        return _sandbox_from(self._store.load())

    def update_sandbox_config(self, dto: SandboxConfigDTO) -> SandboxConfigDTO:
        """Persist the chosen backend + connection. The agent-server reloads the shared
        config per request, so a new selection drives the NEXT conversation's sandbox."""
        from perpleximanus.core.llm import SandboxSettings

        self._store.save_sandbox(
            SandboxSettings(
                backend=dto.backend,
                docker_socket=dto.docker_socket,
                podman_url=dto.podman_url,
                runtime=dto.runtime,
                image=dto.image,
                workspace_root=dto.workspace_root,
            )
        )
        return _sandbox_from(self._store.load())

    # encoders: bundled-local vs remote (persisted; agent-server honors per request) -

    def encoders_config(self) -> EncodersConfigDTO:
        return _encoders_from(self._store.load())

    def update_encoders_config(self, dto: EncodersConfigDTO) -> EncodersConfigDTO:
        """Persist the encoder mode. The agent-server rebuilds its research providers
        when this changes, so the toggle takes effect on the NEXT research run."""
        from perpleximanus.core.llm import EncodersSettings

        self._store.save_encoders(
            EncodersSettings(
                remote=dto.remote,
                reranker_url=dto.reranker_url.strip(),
                embedder_url=dto.embedder_url.strip(),
                nli_url=dto.nli_url.strip(),
            )
        )
        return _encoders_from(self._store.load())

    # data sources: web search + extraction provider tiers (persisted) ----------

    def data_sources_config(self) -> DataSourcesConfigDTO:
        return _data_sources_from(self._store.load())

    def update_data_sources_config(self, dto: DataSourcesConfigDTO) -> DataSourcesConfigDTO:
        """Persist the search + extraction provider choices. The agent-server rebuilds
        its research providers when these change → effective on the NEXT research run."""
        from perpleximanus.core.llm import ExtractionSettings, SearchSettings

        self._store.save_search(
            SearchSettings(
                provider=dto.search_provider,
                base_url=dto.search_base_url.strip(),
                api_key_env=dto.search_api_key_env.strip(),
            )
        )
        self._store.save_extraction(
            ExtractionSettings(
                provider=dto.extraction_provider,
                base_url=dto.extraction_base_url.strip(),
                api_key_env=dto.extraction_api_key_env.strip(),
            )
        )
        return _data_sources_from(self._store.load())

    # Build-project storage path (persisted; agent-server reads it per request) ---

    def projects_config(self) -> ProjectStorageConfigDTO:
        return _projects_from(self._store.load())

    def update_projects_config(
        self, dto: ProjectStorageConfigDTO
    ) -> ProjectStorageConfigDTO:
        """Persist the chosen projects_root after validation. An empty path
        unsets it (allowed). A non-empty path must be an existing, writable
        directory or this raises ConfigValidationError; the endpoint maps that
        to a 400 with a typed reason so the UI can show a specific error."""
        from perpleximanus.core.llm import ProjectStorageSettings
        from perpleximanus.tools.projects import StorageStatus, validate_root

        raw = dto.projects_root.strip()
        if raw:
            status = validate_root(raw)
            if status != StorageStatus.OK:
                raise ConfigValidationError(
                    reason=status.value,
                    detail=f"projects_root {raw!r}: {status.value}",
                )
        self._store.save_projects(ProjectStorageSettings(projects_root=raw))
        return _projects_from(self._store.load())

    # skills — real, persistent .md files (PMX_SKILLS_DIR) -------------------

    @staticmethod
    def _skill_dto(s) -> SkillDTO:
        return SkillDTO(
            id=s.id, name=s.name, description=s.description, enabled=s.enabled, body=s.body
        )

    def skills(self) -> list[SkillDTO]:
        return [self._skill_dto(s) for s in self._skills_store.list()]

    def create_skill(self, create: SkillCreate) -> SkillDTO:
        s = self._skills_store.create(
            name=create.name,
            description=create.description,
            body=create.body,
            enabled=create.enabled,
        )
        return self._skill_dto(s)

    def update_skill(self, skill_id: str, patch: SkillPatch) -> SkillDTO | None:
        existing = self._skills_store.get(skill_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                k: v
                for k, v in {
                    "name": patch.name,
                    "description": patch.description,
                    "enabled": patch.enabled,
                    "body": patch.body,
                }.items()
                if v is not None
            }
        )
        self._skills_store.save(updated)
        return self._skill_dto(updated)

    def delete_skill(self, skill_id: str) -> bool:
        return self._skills_store.delete(skill_id)

    # mcp (wiring-pending) ----------------------------------------------------

    def mcp_connections(self) -> list[McpConnectionDTO]:
        return [c.model_copy() for c in self._mcp]
