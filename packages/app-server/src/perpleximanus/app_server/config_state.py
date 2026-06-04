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


class SkillDTO(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool


class SkillPatch(BaseModel):
    enabled: bool


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


def _assignments_from(config: RouterConfig) -> AssignmentsDTO:
    roles = {
        role.value: config.assignments.get(role, config.default_model)
        for role in ModelRole
        if role != ModelRole.AGENT_DRIVER
    }
    return AssignmentsDTO(default_model=config.default_model, roles=roles)


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
    ) -> None:
        if store is not None:
            self._store = store
        elif config is not None:
            self._store = ConfigStore(base_factory=lambda: config)
        else:
            self._store = ConfigStore()
        self._secrets = secrets or SecretStore()
        self._skills: list[SkillDTO] = [
            SkillDTO(
                id="web-research",
                name="Web research",
                description="Search, extract, and ground answers against live sources.",
                enabled=True,
            ),
            SkillDTO(
                id="code-exec",
                name="Code execution",
                description="Run code in the sandbox to compute and verify.",
                enabled=True,
            ),
            SkillDTO(
                id="doc-analysis",
                name="Document analysis",
                description="Read and reason over uploaded documents.",
                enabled=False,
            ),
        ]
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

    # skills (wiring-pending) -------------------------------------------------

    def skills(self) -> list[SkillDTO]:
        return [s.model_copy() for s in self._skills]

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> list[SkillDTO]:
        self._skills = [
            s.model_copy(update={"enabled": enabled}) if s.id == skill_id else s
            for s in self._skills
        ]
        return self.skills()

    # mcp (wiring-pending) ----------------------------------------------------

    def mcp_connections(self) -> list[McpConnectionDTO]:
        return [c.model_copy() for c in self._mcp]
