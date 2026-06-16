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

from disco.core import SkillStore
from disco.core.llm import ConfigStore, ModelRole, RouterConfig, SecretStore
from disco.core.llm.config import McpSettings, ModelEntry
from disco.core.llm.types import Requirement

from .config.dtos import (
    AssignmentsDTO,
    AssignmentsPatch,
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    McpConnectionDTO,
    McpServerApproveDTO,
    McpServerConfigDTO,
    ModelDTO,
    ModelUpsert,
    OpenRouterKeyStatus,
    OpenRouterModelDTO,
    ProjectStorageConfigDTO,
    SandboxConfigDTO,
    SkillCreate,
    SkillDTO,
    SkillPatch,
    TtsConfigDTO,
)

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
        db_conn: object = None,
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
        # DB connection for mcp_approvals table (shared with agent-server).
        # When None (tests without a DB), MCP config persists to ConfigStore only.
        self._db_conn = db_conn

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
        from disco.core.llm import SandboxSettings

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
        from disco.core.llm import EncodersSettings

        self._store.save_encoders(
            EncodersSettings(
                remote=dto.remote,
                reranker_url=dto.reranker_url.strip(),
                embedder_url=dto.embedder_url.strip(),
                nli_url=dto.nli_url.strip(),
            )
        )
        return _encoders_from(self._store.load())

    # TTS: audio-overview toggle / bundled-vs-remote / voices (persisted) --------

    def tts_config(self) -> TtsConfigDTO:
        return _tts_from(self._store.load())

    def update_tts_config(self, dto: TtsConfigDTO) -> TtsConfigDTO:
        """Persist the audio-overview TTS settings. The agent-server reloads the config
        per request, so a toggle takes effect on the NEXT overview; disabling it also
        lets the agent-server unload the Kokoro model to free RAM."""
        from disco.core.llm import TtsSettings

        self._store.save_tts(
            TtsSettings(
                enabled=dto.enabled,
                provider=dto.provider,
                base_url=dto.base_url.strip(),
                api_key_env=dto.api_key_env.strip(),
                model=dto.model.strip(),
                voice_a=dto.voice_a.strip() or "af_heart",
                voice_b=dto.voice_b.strip() or "af_bella",
            )
        )
        return _tts_from(self._store.load())

    # data sources: web search + extraction provider tiers (persisted) ----------

    def data_sources_config(self) -> DataSourcesConfigDTO:
        return _data_sources_from(self._store.load())

    def update_data_sources_config(self, dto: DataSourcesConfigDTO) -> DataSourcesConfigDTO:
        """Persist the search + extraction provider choices. The agent-server rebuilds
        its research providers when these change → effective on the NEXT research run."""
        from disco.core.llm import ExtractionSettings, SearchSettings

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
        from disco.core.llm import ProjectStorageSettings
        from disco.tools.projects import (
            StorageStatus,
            resolve_projects_root,
            validate_root,
        )

        raw = dto.projects_root.strip()
        if raw:
            # E4: a configured-but-missing path that's CREATABLE is mkdir -p'd and
            # accepted ("works every time"); only a genuinely unreachable path
            # (mkdir fails) stays NOT_FOUND and is rejected with a typed reason.
            status = validate_root(resolve_projects_root(raw))
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
            id=s.id,
            name=s.name,
            description=s.description,
            enabled=s.enabled,
            body=s.body,
            surfaces=s.surfaces,
        )

    def skills(self) -> list[SkillDTO]:
        return [self._skill_dto(s) for s in self._skills_store.list()]

    def create_skill(self, create: SkillCreate) -> SkillDTO:
        s = self._skills_store.create(
            name=create.name,
            description=create.description,
            body=create.body,
            enabled=create.enabled,
            surfaces=create.surfaces,
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
                    "surfaces": patch.surfaces,
                }.items()
                if v is not None
            }
        )
        self._skills_store.save(updated)
        return self._skill_dto(updated)

    def delete_skill(self, skill_id: str) -> bool:
        return self._skills_store.delete(skill_id)

    # mcp (live, persistent CRUD) — rung B ----------------------------------

    def _mcp_config(self) -> McpSettings:
        return self._store.load().mcp

    def _mcp_approvals(self) -> dict[str, dict]:
        """Read all approval rows keyed by server name."""
        if self._db_conn is None:
            return {}
        try:
            from disco.tools.mcp.migrations import list_mcp_approvals

            rows = list_mcp_approvals(self._db_conn)
            return {r["server"]: r for r in rows}
        except Exception:
            return {}

    def _mcp_approval_pending(self) -> dict[str, dict]:
        """E6 (#10): read the per-server DRIFT rows the agent-server wrote.

        Each row is the AUTHORITATIVE new_hash the live agent-server pool
        computed at startup when it detected a description_hash mismatch
        against `mcp_approvals`. The app-server surfaces this on
        `McpConnectionDTO.new_description_hash` so the ApprovalDiff renders
        the REAL fingerprint of the changed tool set — not a stub of the
        stored hash. The agent-server clears the row in the same transaction
        as `create_mcp_approval` (the operator accepted the new tools), so
        `new_description_hash` is `None` once a server is in sync.
        """
        if self._db_conn is None:
            return {}
        try:
            from disco.tools.mcp.migrations import list_mcp_approval_pending

            rows = list_mcp_approval_pending(self._db_conn)
            return {r["server"]: r for r in rows}
        except Exception:
            return {}

    def mcp_connections(self) -> list[McpConnectionDTO]:
        """Live pool projection: every configured server + its approval status.

        E6 (#10): also projects `new_description_hash` from the
        `mcp_approval_pending` drift table. When the live agent-server pool
        just computed a fresh hash that differs from the stored approval,
        `new_description_hash` carries that AUTHORITATIVE value and the
        frontend ApprovalDiff uses it for the new-hash side of the diff
        (and as the body of the re-approve POST). When the server is in
        sync (no drift row), `new_description_hash` is `None` and the UI
        does not show a diff banner.
        """
        cfg = self._mcp_config()
        approvals = self._mcp_approvals()
        pending = self._mcp_approval_pending()
        out: list[McpConnectionDTO] = []
        for name, srv in cfg.servers.items():
            url = (
                srv.get("url", "") or srv.get("command", [""])[0]
                if srv.get("command")
                else srv.get("url", "")
            )
            ap = approvals.get(name)
            pd = pending.get(name)
            out.append(
                McpConnectionDTO(
                    id=name,
                    name=name,
                    url=url,
                    status=_mcp_live_status(ap),
                    transport=srv.get("transport"),
                    risk_tier=srv.get("risk_tier"),
                    description_hash=ap["description_hash"] if ap else None,
                    # E6: pass the AUTHORITATIVE new_hash through verbatim —
                    # NEVER recompute here, the backend is the source of
                    # truth. None when the server is in sync.
                    new_description_hash=pd["new_hash"] if pd else None,
                    approved_at=ap["approved_at"] if ap else None,
                    enabled=srv.get("enabled", True),
                )
            )
        return out

    def create_mcp_server(self, body: McpServerConfigDTO) -> McpConnectionDTO:
        cfg = self._mcp_config()
        if body.name in cfg.servers:
            raise ValueError(f"server {body.name!r} already exists")
        srv: dict = {
            "transport": body.transport,
            "url": body.url,
            "enabled": body.enabled,
            "allowed_tools": body.allowed_tools,
            "risk_tier": body.risk_tier,
        }
        if body.transport == "stdio":
            srv["command"] = [body.url]
        servers = {**cfg.servers, body.name: srv}
        new_cfg = cfg.model_copy(update={"servers": servers})
        self._store.save(
            self._store.load().model_copy(update={"mcp": new_cfg})
        )
        return McpConnectionDTO(
            id=body.name,
            name=body.name,
            url=body.url,
            status="disconnected",
            transport=body.transport,
            risk_tier=body.risk_tier,
            enabled=body.enabled,
        )

    def update_mcp_server(self, name: str, patch: McpServerConfigDTO) -> McpConnectionDTO | None:
        cfg = self._mcp_config()
        if name not in cfg.servers:
            return None
        existing = cfg.servers[name]
        updated = {**existing}
        if patch.transport:
            updated["transport"] = patch.transport
        if patch.url:
            updated["url"] = patch.url
        if patch.enabled is not None:
            updated["enabled"] = patch.enabled
        if patch.allowed_tools is not None:
            updated["allowed_tools"] = patch.allowed_tools
        if patch.risk_tier:
            updated["risk_tier"] = patch.risk_tier
        servers = {**cfg.servers, name: updated}
        new_cfg = cfg.model_copy(update={"servers": servers})
        self._store.save(
            self._store.load().model_copy(update={"mcp": new_cfg})
        )
        approvals = self._mcp_approvals()
        ap = approvals.get(name)
        return McpConnectionDTO(
            id=name,
            name=name,
            url=updated.get("url", ""),
            status=_mcp_live_status(ap),
            transport=updated.get("transport"),
            risk_tier=updated.get("risk_tier"),
            description_hash=ap["description_hash"] if ap else None,
            approved_at=ap["approved_at"] if ap else None,
            enabled=updated.get("enabled", True),
        )

    def delete_mcp_server(self, name: str) -> bool:
        cfg = self._mcp_config()
        if name not in cfg.servers:
            return False
        servers = {k: v for k, v in cfg.servers.items() if k != name}
        new_cfg = cfg.model_copy(update={"servers": servers})
        self._store.save(
            self._store.load().model_copy(update={"mcp": new_cfg})
        )
        # Also remove the approval row.
        if self._db_conn is not None:
            try:
                from disco.tools.mcp.migrations import delete_mcp_approval

                delete_mcp_approval(self._db_conn, name)
            except Exception:
                pass
        return True

    def approve_mcp_server(self, name: str, body: McpServerApproveDTO) -> McpConnectionDTO:
        """Approve or re-approve — mutates the single row, never inserts a second."""
        cfg = self._mcp_config()
        if name not in cfg.servers:
            raise KeyError(f"unknown server {name!r}")
        if self._db_conn is None:
            raise RuntimeError("no DB connection for approval persistence")
        from disco.tools.mcp.migrations import create_mcp_approval, get_mcp_approval

        create_mcp_approval(self._db_conn, name, body.description_hash)
        ap = get_mcp_approval(self._db_conn, name)
        srv = cfg.servers[name]
        return McpConnectionDTO(
            id=name,
            name=name,
            url=srv.get("url", ""),
            status=_mcp_live_status(ap),
            transport=srv.get("transport"),
            risk_tier=srv.get("risk_tier"),
            description_hash=ap["description_hash"] if ap else None,
            approved_at=ap["approved_at"] if ap else None,
            enabled=srv.get("enabled", True),
        )

    def mcp_approval_diff(self, name: str, new_hash: str) -> dict | None:
        """Return old-vs-new hash diff for the approve UI. None = no diff or no
        stored approval."""
        if self._db_conn is None:
            return None
        from disco.tools.mcp.migrations import get_mcp_approval

        old = get_mcp_approval(self._db_conn, name)
        if old is None:
            return None
        old_hash = old["description_hash"]
        if old_hash == new_hash:
            return None
        return {
            "server": name,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "approved_at": old["approved_at"],
            "approved_by": old["approved_by"],
        }


def _mcp_live_status(approval: dict | None) -> str:
    """Derive the live status for a server. In the app-server projection:
    'connected' = approved exists; 'disconnected' = no approval yet.
    The agent-server is what actually dials and may surface 'error'."""
    return "connected" if approval else "disconnected"
