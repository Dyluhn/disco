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

from perpleximanus.core.llm import ConfigStore, ModelRole, RouterConfig
from pydantic import BaseModel

# ---- wire DTOs (mirror the frontend types) ----------------------------------


class ModelDTO(BaseModel):
    id: str
    label: str
    provider: str  # "local" | "openrouter"
    price_in_per_m: float
    price_out_per_m: float
    capabilities: list[str]
    note: str | None = None


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


def _provider_of(raw: str) -> str:
    # The frontend distinguishes only local (free) vs openrouter (paid overflow).
    return "openrouter" if raw == "openrouter" else "local"


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
                provider=_provider_of(entry.provider),
                price_in_per_m=entry.price_in_per_m,
                price_out_per_m=entry.price_out_per_m,
                capabilities=sorted(r.value for r in entry.capabilities),
                note=_note(entry),
            )
        )
    return out


def _assignments_from(config: RouterConfig) -> AssignmentsDTO:
    roles = {
        role.value: config.assignments.get(role, config.default_model)
        for role in ModelRole
        if role != ModelRole.AGENT_DRIVER
    }
    return AssignmentsDTO(default_model=config.default_model, roles=roles)


# ---- the session-scoped config state ----------------------------------------


class ConfigState:
    """Holds the live config the settings surface reads/writes. Model catalogue
    is read-only (the assignable models); assignments are mutable (the absolute,
    manual model story) and PERSISTED via a shared ConfigStore so the agent-server
    runtime actually honors them. Skills/MCP are wiring-pending scaffolds.

    `store` is the shared assignment store (defaults to PMX_CONFIG). `config` only
    seeds the read-only catalogue when no store is supplied (test convenience)."""

    def __init__(
        self, config: RouterConfig | None = None, *, store: ConfigStore | None = None
    ) -> None:
        if store is not None:
            self._store = store
        elif config is not None:
            self._store = ConfigStore(base_factory=lambda: config)
        else:
            self._store = ConfigStore()
        self._models = _models_from(self._store.load())
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
        return self._models

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
