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

from typing import TYPE_CHECKING, Any

from disco.core import SkillStore
from disco.core.llm import ConfigStore, ModelRole, RouterConfig, SecretStore
from disco.core.llm.config import McpSettings, ProviderSettings
from disco.core.quota import QuotaConfig, QuotaUsage, SqliteQuotaStore, StoredQuotaConfig
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    STRIPE_API_URL,
    STRIPE_SECRET_REF,
    StripeAppConfig,
    StripeAppConfigStore,
    configure_stripe_restricted_key,
    configure_stripe_webhook_secret,
    ensure_stripe_binding_secret,
)

if TYPE_CHECKING:
    # The shared mcp_approvals DB connection — a duck-typed sqlite3-like conn
    # (execute/commit). Type-only import keeps it off the runtime path.
    from disco.tools.mcp.migrations import _ApprovalConn

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import (
    AssignmentsDTO,
    AssignmentsPatch,
    BuildKernelConfigDTO,
    DataSourcesConfigDTO,
    EncodersConfigDTO,
    ImageGenConfigDTO,
    LiveBrowserConfigDTO,
    McpConnectionDTO,
    McpServerApproveDTO,
    McpServerConfigDTO,
    McpServerPatchDTO,
    ModelDTO,
    ModelUpsert,
    OpenRouterKeyStatus,
    ProbeResult,
    ProjectStorageConfigDTO,
    ProviderCatalogueModelDTO,
    ProviderCreate,
    ProviderDTO,
    ProviderEnableBody,
    ProviderPatch,
    RoleFallbackConfigDTO,
    SandboxConfigDTO,
    SandboxHealthDTO,
    SecretsListDTO,
    SecretStatus,
    SkillCreate,
    SkillDTO,
    SkillPatch,
    TtsConfigDTO,
)
from .config.mappers import (
    _assignments_from,
    _build_kernel_from,
    _data_sources_from,
    _encoders_from,
    _entry_from,
    _image_gen_from,
    _live_browser_from,
    _models_from,
    _projects_from,
    _role_fallback_from,
    _sandbox_from,
    _tts_from,
)
from .mcp_config_service import McpConfigService
from .provider_config_service import (
    ProviderConfigService,
)
from .provider_config_service import (
    ProviderInUseError as ProviderInUseError,
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
        db_conn: _ApprovalConn | None = None,
        stripe_configs: StripeAppConfigStore | None = None,
        quota_store: SqliteQuotaStore | None = None,
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
        self._stripe_configs = stripe_configs
        self._quota_store = quota_store
        self._provider_config = ProviderConfigService(
            self._store,
            self._secrets,
            add_model=lambda body: self.add_model(body),
            remove_model=lambda model_id: self.remove_model(model_id),
        )
        self._mcp_service = McpConfigService(self._store, self._secrets, db_conn)

    def _approve_origin(self, url: str, purpose: str, secret_ref: str | None = "") -> None:
        _origin_wiring.sign_origin(self._store, self._secrets, url, purpose, secret_ref)

    def approve_origin(self, url: str, purpose: str, secret_ref: str | None = "") -> None:
        _origin_wiring.approve_origin(self._store, self._secrets, url, purpose, secret_ref)

    def _approve_model_origin(self, entry: Any, model_id: str | None = None) -> None:
        _origin_wiring.approve_model_origin(self._store, self._secrets, entry, model_id=model_id)

    # models + assignments (the absolute manual model story) ------------------

    def models(self) -> list[ModelDTO]:
        return _models_from(self._store.load())

    def add_model(self, upsert: ModelUpsert) -> list[ModelDTO]:
        """Add a model to the catalogue. New models are their own endpoint (the
        endpoint key = the catalogue id). Raises ValueError on a duplicate id."""
        entry = _entry_from(upsert, provider=upsert.id)
        self._store.add_model(upsert.id, entry)
        self._approve_model_origin(entry, model_id=upsert.id)
        return _models_from(self._store.load())

    def update_model(self, model_id: str, upsert: ModelUpsert) -> list[ModelDTO]:
        """Edit an existing model. Preserves its endpoint key so a seeded model
        sharing a backend isn't silently split off. Raises ValueError if missing."""
        existing = self._store.load().models.get(model_id)
        provider = existing.provider if existing is not None else model_id
        entry = _entry_from(upsert, provider=provider)
        self._store.update_model(model_id, entry)
        self._approve_model_origin(entry, model_id=model_id)
        return _models_from(self._store.load())

    def remove_model(self, model_id: str) -> list[ModelDTO]:
        """Remove a model. Raises ValueError if it's the default or assigned to a
        role (the user must reassign first — we never silently break routing)."""
        return _models_from(self._store.remove_model(model_id))

    # openrouter key (encrypted at rest) -------------------------------------

    def openrouter_key_status(self) -> OpenRouterKeyStatus:
        # `locked` must reflect ACTUAL decryptability, not just "app secret missing".
        # A key encrypted under a DIFFERENT app secret (rotated/wrong key) is present
        # but undecryptable — SecretStore.locked misses that (it only checks box
        # availability, and it gates the env-secret import so it must stay lenient).
        # Compute the DISPLAY status here from a real decrypt attempt so the UI shows
        # "re-enter the key" instead of a false green over an unusable key.
        present = self._secrets.has_openrouter_key()
        usable = present and bool(self._secrets.get_openrouter_key())
        return OpenRouterKeyStatus(
            configured=present,
            locked=present and not usable,
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

    # provider objects (definition + encrypted key + catalogue model toggles) -

    def _provider_dto(self, provider: ProviderSettings) -> ProviderDTO:
        return self._provider_config._provider_dto(provider)

    def _save_providers(self, providers: dict[str, ProviderSettings]) -> None:
        self._provider_config._save_providers(providers)

    def providers(self) -> list[ProviderDTO]:
        return self._provider_config.providers()

    def provider_settings(self, provider_id: str) -> ProviderSettings:
        return self._provider_config.provider_settings(provider_id)

    def provider_origin_approved(self, provider: ProviderDTO) -> bool:
        return self._provider_config.provider_origin_approved(provider)

    def _unique_provider_id(self, label: str) -> str:
        return self._provider_config._unique_provider_id(label)

    def create_provider(self, body: ProviderCreate) -> ProviderDTO:
        return self._provider_config.create_provider(body)

    def update_provider(self, provider_id: str, patch: ProviderPatch) -> ProviderDTO:
        return self._provider_config.update_provider(provider_id, patch)

    def delete_provider(self, provider_id: str) -> None:
        self._provider_config.delete_provider(provider_id)

    def _unique_provider_catalogue_id(
        self, provider_id: str, model_id: str, label: str | None
    ) -> str:
        return self._provider_config._unique_provider_catalogue_id(provider_id, model_id, label)

    def enable_provider_model(
        self,
        provider_id: str,
        body: ProviderEnableBody,
        catalogue_model: ProviderCatalogueModelDTO | None = None,
    ) -> list[ModelDTO]:
        return self._provider_config.enable_provider_model(provider_id, body, catalogue_model)

    def disable_provider_model(self, provider_id: str, catalogue_id: str) -> list[ModelDTO]:
        return self._provider_config.disable_provider_model(provider_id, catalogue_id)

    # generic provider secrets (encrypted at rest, keyed by api_key_env name) ---
    # "openrouter" is reserved for the dedicated route/UI above; the generic
    # surface neither lists nor accepts it (avoids two UIs fighting over one slot).

    _RESERVED_SECRETS = frozenset({"openrouter", STRIPE_SECRET_REF})

    @classmethod
    def _reserved_secret(cls, name: str) -> bool:
        return name in cls._RESERVED_SECRETS or name.startswith(
            ("stripe.binding.", "stripe.webhook.")
        )

    def list_secrets(self) -> SecretsListDTO:
        names = [n for n in self._secrets.secret_names() if not self._reserved_secret(n)]
        # The SPECIFIC stored keys that can't be decrypted (named, so the UI can
        # say which to fix) — generic, not the OpenRouter-only `locked` property.
        locked_names = [
            n for n in self._secrets.undecryptable_names() if not self._reserved_secret(n)
        ]
        return SecretsListDTO(
            names=sorted(names),
            locked_names=sorted(locked_names),
            locked=bool(locked_names),
            can_store=self._secrets.can_store,
        )

    def secret_status(self, name: str) -> SecretStatus:
        return SecretStatus(
            name=name,
            configured=self._secrets.has_secret(name),
            locked=self._secrets.locked,
            can_store=self._secrets.can_store,
        )

    def set_secret(self, name: str, value: str) -> SecretStatus:
        """Encrypt + persist a provider key under its api_key_env NAME. Raises
        ValueError (→ 400) on a reserved/invalid name, a blank value, or no app
        secret to encrypt with."""
        name = name.strip()
        from disco.core.llm.secret_refs import is_control_secret_ref

        if self._reserved_secret(name):
            route = "/api/openrouter/key" if name == "openrouter" else "/api/stripe/config/{app}"
            raise ValueError(f"use the dedicated {route} route for the {name} credential")
        if is_control_secret_ref(name):
            raise ValueError(f"{name} is an internal control secret and cannot be a provider ref")
        if not value.strip():
            raise ValueError("value is empty")
        try:
            self._secrets.set_secret(name, value.strip())
        except RuntimeError as exc:  # no DISCO_SECRET_KEY → can't encrypt
            raise ValueError(str(exc)) from exc
        return self.secret_status(name)

    def clear_secret(self, name: str) -> SecretStatus:
        name = name.strip()
        from disco.core.llm.secret_refs import is_control_secret_ref

        if self._reserved_secret(name):
            route = "/api/openrouter/key" if name == "openrouter" else "/api/stripe/config/{app}"
            raise ValueError(f"use the dedicated {route} route for the {name} credential")
        if is_control_secret_ref(name):
            raise ValueError(f"{name} is an internal control secret and cannot be cleared")
        self._secrets.clear_secret(name)
        return self.secret_status(name)

    def configure_stripe_app(
        self,
        *,
        owner_id: str,
        audience: str,
        restricted_key: str,
        webhook_secret: str,
        plan_selector: str,
        stripe_price_id: str,
        allowed_return_origins: frozenset[str],
        enabled: bool,
    ) -> StripeAppConfig:
        """Owner-only settings seam; the key is write-only and separately encrypted."""
        if self._stripe_configs is None:
            raise RuntimeError("Stripe configuration store is not wired to shared host state")
        self._stripe_configs.validate_configuration(
            owner_id=owner_id,
            audience=audience,
            plan_selector=plan_selector,
            stripe_price_id=stripe_price_id,
            allowed_return_origins=allowed_return_origins,
            enabled=enabled,
        )
        configure_stripe_restricted_key(self._secrets, restricted_key)
        configure_stripe_webhook_secret(
            self._secrets,
            owner_id,
            audience,
            webhook_secret,
        )
        ensure_stripe_binding_secret(self._secrets, owner_id, audience)
        config = self._stripe_configs.configure(
            owner_id=owner_id,
            audience=audience,
            plan_selector=plan_selector,
            stripe_price_id=stripe_price_id,
            allowed_return_origins=allowed_return_origins,
            enabled=enabled,
            secret_store=self._secrets,
        )
        self._approve_origin(
            STRIPE_API_URL,
            PAYMENTS_CHECKOUT_SERVICE_NAME,
            STRIPE_SECRET_REF,
        )
        return config

    # per-app host-service quotas -------------------------------------------

    def configure_app_quota(
        self,
        *,
        owner_id: str,
        audience: str,
        limits: QuotaConfig,
        service: str | None = None,
    ) -> StoredQuotaConfig:
        if self._quota_store is None:
            raise RuntimeError("quota store is not wired to shared host state")
        return self._quota_store.configure(
            owner_id=owner_id,
            audience=audience,
            limits=limits,
            service=service,
        )

    def app_quota_config(
        self, owner_id: str, audience: str, *, service: str | None = None
    ) -> StoredQuotaConfig | None:
        if self._quota_store is None:
            raise RuntimeError("quota store is not wired to shared host state")
        return self._quota_store.get_config(owner_id, audience, service=service)

    def delete_app_quota(self, owner_id: str, audience: str, *, service: str | None = None) -> bool:
        if self._quota_store is None:
            raise RuntimeError("quota store is not wired to shared host state")
        return self._quota_store.delete_config(owner_id, audience, service=service)

    def app_quota_usage(
        self, owner_id: str, audience: str, *, service: str | None = None
    ) -> QuotaUsage:
        if self._quota_store is None:
            raise RuntimeError("quota store is not wired to shared host state")
        return self._quota_store.get_usage(
            owner_id=owner_id,
            audience=audience,
            service=service,
        )

    def default_app_quota(self) -> QuotaConfig:
        if self._quota_store is None:
            raise RuntimeError("quota store is not wired to shared host state")
        return self._quota_store.default_config

    def _resolve_secret_value(self, name: str) -> str | None:
        from disco.core.llm.secret_refs import resolve_provider_secret

        return resolve_provider_secret(name, self._secrets)

    async def test_secret(self, name: str) -> ProbeResult:
        """Probe T4.1: a cheap authenticated call against the OpenAI-compatible
        backend that USES this key, so a "green" means the provider really
        answered. The endpoint is resolved from the model catalogue (the
        ModelEntry whose ``api_key_env`` matches this name); the value is
        decrypted from the secret store. Honest failures (no endpoint, no value,
        bad key, host down) come back ok=False at HTTP 200."""
        cfg = self._store.load()
        # 1) Find an OpenAI-compatible model endpoint that consumes this key —
        # capture its model_id too, so the probe can do a real authenticated
        # completion (a key that only passes an UNauthenticated /models GET would
        # be a false green — OpenRouter serves /models without a key).
        base_url: str | None = None
        model_id: str = ""
        provider: str = ""
        for entry in cfg.models.values():
            if entry.api_key_env == name and entry.base_url:
                base_url = entry.base_url
                model_id = entry.model_id
                provider = entry.provider
                break
        if base_url is None:
            # Maybe a search/extraction key (no /models endpoint) — point the
            # user at the right probe rather than failing opaquely.
            ds_envs = {
                cfg.search.api_key_env,
                cfg.extraction.api_key_env,
            }
            if name in ds_envs and name:
                return ProbeResult(
                    ok=False,
                    status="misconfigured",
                    detail=(
                        f"{name} is used by a data source, which has no models "
                        "list to test. Use 'Test connection' under Data sources."
                    ),
                )
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=(
                    f"No configured model references {name}. Assign it as a "
                    "model's api_key_env (Models), then test it there."
                ),
            )
        if gate := _origin_wiring.probe_approval_gate(
            self._store,
            "model",
            base_url,
            provider=provider,
            secret_ref=name,
            secrets=self._secrets,
            require_secret_ref_allowed=True,
        ):
            return gate
        # 2) Decrypt the stored value (or fall back to env).
        value = self._resolve_secret_value(name)
        if not value:
            locked = self._secrets.locked
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=(
                    f"No decryptable value for {name} — "
                    + (
                        "the app secret can't decrypt it; re-enter the key."
                        if locked
                        else "store the key first."
                    )
                ),
            )
        # 3) The real, authenticated network call (a 1-token completion).
        from .probe_clients import probe_openai_auth

        ok, status, detail = await probe_openai_auth(base_url, value, model_id)
        return ProbeResult(ok=ok, status=status, detail=detail)

    async def test_data_source(self, kind: str) -> ProbeResult:
        """Probe T4.2: reachability of the configured search/extraction endpoint.
        Bundled (in-process) tiers have no network dependency — reported honestly
        as such, never as a remote "ok". Self-host tiers probe the configured base
        URL; paid tiers probe the vendor host (any HTTP answer = reachable, a
        401/403 = the key was rejected)."""
        from .probe_clients import probe_reachable

        cfg = self._store.load()
        # Vendor hosts for the paid tiers (no key-free models list; reachability only).
        _PAID_HOSTS = {
            "tavily": "https://api.tavily.com",
            "brave": "https://api.search.brave.com",
            "firecrawl": "https://api.firecrawl.dev",
        }
        if kind == "search":
            s = cfg.search
            provider, base_url, key_env = s.provider, s.base_url.strip(), s.api_key_env.strip()
            bundled = {"ddgs", "arxiv", "news", "semantic_scholar", "site_scoped"}
        elif kind == "extraction":
            e = cfg.extraction
            provider, base_url, key_env = e.provider, e.base_url.strip(), e.api_key_env.strip()
            bundled = {"local"}
        else:
            return ProbeResult(
                ok=False, status="error", detail=f"unknown data-source kind {kind!r}"
            )

        if provider in bundled:
            return ProbeResult(
                ok=True,
                status="bundled",
                detail=(
                    f"Bundled/keyless tier ({provider}) — requires no configured "
                    "service URL, so there is no self-hosted endpoint to reach."
                ),
                provider=provider,
            )
        # Self-host tiers (searxng / crawl4ai) need a configured base URL.
        if provider in ("searxng", "crawl4ai"):
            if not base_url:
                return ProbeResult(
                    ok=False,
                    status="misconfigured",
                    detail=f"No base URL set for {provider} — add the service URL above.",
                    provider=provider,
                )
            if gate := _origin_wiring.probe_approval_gate(
                self._store, kind, base_url, provider=provider, secrets=self._secrets
            ):
                return gate
            ok, status, detail = await probe_reachable(base_url)
            return ProbeResult(ok=ok, status=status, detail=detail, provider=provider)
        # Paid tiers (tavily / brave / firecrawl): probe the vendor host with the key.
        host = base_url or _PAID_HOSTS.get(provider)
        if not host:
            return ProbeResult(
                ok=False,
                status="misconfigured",
                detail=f"No endpoint known for provider {provider!r}.",
                provider=provider,
            )
        if gate := _origin_wiring.probe_approval_gate(
            self._store,
            kind,
            host,
            provider=provider,
            secret_ref=key_env,
            secrets=self._secrets,
            require_secret_ref_allowed=True,
        ):
            return gate
        key = self._resolve_secret_value(key_env) if key_env else None
        ok, status, detail = await probe_reachable(host, api_key=key)
        return ProbeResult(ok=ok, status=status, detail=detail, provider=provider)

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
        config per request, so a new selection drives the NEXT conversation's sandbox.

        W-48: the connectivity PREFLIGHT is a SEPARATE probe (`test_sandbox` /
        POST /api/sandbox/test) the Save flow runs after persisting, so a config is
        never LOST just because the host is momentarily down — the user saves, then
        sees the typed named reachability verdict and can fix the host. The
        agent-server ALSO re-probes on first use (first-kick pre-flight).

        STRUCTURAL validation (the outage guard): a `gvisor` backend with an empty /
        host-less `docker_socket` (or `podman` with an empty `podman_url`) is REJECTED
        with a typed ConfigValidationError → the endpoint maps it to 400, rather than
        silently persisting an unrunnable config that fails every later run. The
        per-backend `connections` map is preserved by the store, so the rejected save
        leaves the last-good block for that backend intact."""
        from disco.core.llm import SandboxConnection, SandboxSettings, sandbox_connection_error

        reason = sandbox_connection_error(
            dto.backend,
            SandboxConnection(
                docker_socket=dto.docker_socket,
                podman_url=dto.podman_url,
                runtime=dto.runtime,
                image=dto.image,
                workspace_root=dto.workspace_root,
            ),
        )
        if reason is not None:
            raise ConfigValidationError(
                "unrunnable_sandbox",
                detail=f"Can't save the {dto.backend} sandbox: {reason}.",
            )

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

    async def sandbox_health(self) -> SandboxHealthDTO:
        """Reachability of the ACTIVE (persisted) sandbox backend — the cheap,
        side-effect-free signal the app shell surfaces as a banner BEFORE a run is
        started. Reuses the SAME `test_sandbox` probe (→ the same `healthcheck()` the
        run path hits), so the banner and the real run can't disagree. Never raises:
        a probe failure is a RESULT (reachable=False with a host-naming detail)."""
        dto = self.sandbox_config()
        probe = await self.test_sandbox(dto)
        return SandboxHealthDTO(
            reachable=probe.ok,
            backend=dto.backend,
            detail=probe.detail,
        )

    # W-48: HARD wall-clock bound on the sandbox connectivity probe — a "test" must
    # feel instant and never hang Settings on a black-holed gVisor/Podman host. The
    # backends' own client_timeout_s bounds the socket, but the SSH transport is
    # outside that, so this caps the WHOLE probe; a timeout is itself "unreachable".
    _SANDBOX_PROBE_TIMEOUT_S = 12.0

    async def test_sandbox(self, dto: SandboxConfigDTO) -> ProbeResult:
        """W-48: connectivity PREFLIGHT for a sandbox backend. Build the SAME backend
        service the agent-server would (`service_from_config`) and run its
        `healthcheck()` — a REAL probe of the configured endpoint (Docker socket /
        ssh:// host / Podman socket / process workspace root). Classifies the outcome
        into the ProbeResult vocabulary with a typed, host-NAMING detail
        (e.g. "gvisor sandbox host ssh://sandbox@<host> unreachable: …") instead of a
        silent failure or a generic 500 later. Bounded by _SANDBOX_PROBE_TIMEOUT_S so a
        dead host fails fast. Never raises for an expected failure — it's a RESULT."""
        from disco.tools.sandbox import (
            SandboxConfig,
            probe_sandbox_reachability,
            sandbox_endpoint_label,
            service_from_config,
        )

        # NOTE: this co-located probe only reflects reality when the app-server SHARES the
        # sandbox host with the run path (dev / single-process). In a split-container deploy
        # the run path is the AGENT-server (which owns the container socket), so the live UI
        # probes THERE (routes/sandbox.py) — this path stays for the co-located case + tests.
        # The classifier is shared (probe_sandbox_reachability) so the two can never drift.
        cfg = SandboxConfig(
            backend=dto.backend,
            docker_socket=dto.docker_socket,
            podman_url=dto.podman_url,
            runtime=dto.runtime,
            image=dto.image,
            workspace_root=dto.workspace_root,
        )
        endpoint = sandbox_endpoint_label(dto.backend, dto.docker_socket, dto.podman_url)
        try:
            service = service_from_config(cfg)
        except Exception as exc:  # noqa: BLE001 — a construction failure is a RESULT
            return ProbeResult(
                ok=False, status="error", detail=f"{endpoint}: {exc}", provider=dto.backend
            )
        ok, status, detail = await probe_sandbox_reachability(
            service, endpoint, timeout_s=self._SANDBOX_PROBE_TIMEOUT_S
        )
        return ProbeResult(ok=ok, status=status, detail=detail, provider=dto.backend)

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
        _origin_wiring.approve_encoder_origins(self._store, self._secrets, dto)
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
        _origin_wiring.approve_tts_origin(self._store, self._secrets, dto)
        return _tts_from(self._store.load())

    # image generation: ComfyUI / OpenAI-compatible / OpenRouter (persisted) ------

    def image_gen_config(self) -> ImageGenConfigDTO:
        return _image_gen_from(self._store.load())

    def update_image_gen_config(self, dto: ImageGenConfigDTO) -> ImageGenConfigDTO:
        """Persist the image generation provider choice. The agent-server reloads the
        config per request, so a change takes effect on the NEXT image-gen call.
        `comfyui` is self-hosted (needs base_url); `openai` is paid and requires an
        api_key_env secret stored via /api/secrets; `openrouter` (default) is paid and
        uses the stored OpenRouter key. No tier is configured until its requirement is
        met — until then image generation fails NOT CONFIGURED (W-50, no placeholder)."""
        from disco.core.llm import ImageGenSettings

        self._store.save_image_gen(
            ImageGenSettings(
                provider=dto.provider,
                base_url=dto.base_url.strip(),
                api_key_env=dto.api_key_env.strip(),
                model=dto.model.strip(),
                # Only meaningful for comfyui; .strip() trims edge whitespace but
                # preserves the JSON body (internal newlines/indent untouched).
                workflow_json=dto.workflow_json.strip(),
            )
        )
        _origin_wiring.approve_image_gen_origin(self._store, self._secrets, dto)
        return _image_gen_from(self._store.load())

    # data sources: web search + extraction provider tiers (persisted) ----------

    def data_sources_config(self) -> DataSourcesConfigDTO:
        cfg = self._store.load()
        return _data_sources_from(cfg, configured_sources=self._configured_research_sources(cfg))

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
        _origin_wiring.approve_data_source_origins(self._store, self._secrets, dto)
        cfg = self._store.load()
        return _data_sources_from(cfg, configured_sources=self._configured_research_sources(cfg))

    def _configured_research_sources(self, cfg: RouterConfig) -> list[str]:
        configured: set[str] = set()
        search = cfg.search
        if search.base_url.strip():
            configured.add(search.provider)
        if search.api_key_env.strip() and self._resolve_secret_value(search.api_key_env.strip()):
            configured.add(search.provider)
        provider_envs = {
            "tavily": ("TAVILY_API_KEY", "DISCO_TAVILY_API_KEY"),
            "brave": (
                "BRAVE_SEARCH_API_KEY",
                "DISCO_BRAVE_SEARCH_API_KEY",
                "BRAVE_API_KEY",
            ),
            "semantic_scholar": (
                "SEMANTIC_SCHOLAR_API_KEY",
                "DISCO_SEMANTIC_SCHOLAR_API_KEY",
                "S2_API_KEY",
            ),
        }
        for provider, names in provider_envs.items():
            if any(self._resolve_secret_value(name) for name in names):
                configured.add(provider)
        return sorted(configured)

    def _approve_data_source_origins(self, dto: DataSourcesConfigDTO) -> None:
        _origin_wiring.approve_data_source_origins(self._store, self._secrets, dto)

    # auxiliary-role local fallback (persisted; agent-server reads per request) ---

    def role_fallback_config(self) -> RoleFallbackConfigDTO:
        return _role_fallback_from(self._store.load())

    def update_role_fallback_config(self, dto: RoleFallbackConfigDTO) -> RoleFallbackConfigDTO:
        """Persist auxiliary-role fallback settings. An enabled fallback must carry
        both base_url and model; otherwise the router would build a provider that
        cannot make a valid completion request."""
        from disco.core.llm import RoleFallbackSettings

        base_url = dto.base_url.strip()
        model = dto.model.strip()
        api_key_env = dto.api_key_env.strip()
        if dto.enabled and (not base_url or not model):
            raise ConfigValidationError(
                "incomplete_role_fallback",
                detail="Role fallback needs both a base URL and a model when enabled.",
            )
        self._store.save_role_fallback(
            RoleFallbackSettings(
                enabled=dto.enabled,
                base_url=base_url,
                model=model,
                api_key_env=api_key_env,
            )
        )
        _origin_wiring.approve_role_fallback_origin(
            self._store, self._secrets, dto.enabled, base_url, api_key_env
        )
        return _role_fallback_from(self._store.load())

    # Live browser (noVNC) toggle (persisted; agent-server reads per request) ------

    def live_browser_config(self) -> LiveBrowserConfigDTO:
        return _live_browser_from(self._store.load())

    def update_live_browser_config(self, dto: LiveBrowserConfigDTO) -> LiveBrowserConfigDTO:
        """Persist the live-browser toggle. Affects the next /browser/live-url call.

        Defense in depth: REJECT enabling noVNC when the configured sandbox backend
        can't run the live stack (only gVisor ships Xvfb/x11vnc/websockify — see
        LIVE_VIEW_BACKENDS). The Settings UI already greys the toggle on an unsupported
        backend, but a stale persisted flag or a direct API call must not be able to set
        enabled=true for a backend that can't stream it (it would only ever produce the
        live_start_failed error at runtime). Turning it OFF is always allowed."""
        from disco.core.llm.config import LiveBrowserSettings
        from disco.tools.sandbox._container import LIVE_VIEW_BACKENDS

        if dto.enabled:
            backend = self._store.load().sandbox.backend
            if backend not in LIVE_VIEW_BACKENDS:
                raise ConfigValidationError(
                    "unsupported_backend",
                    detail=(
                        f"Live browser needs a containerized gVisor sandbox — it can't run "
                        f"on the {backend} sandbox. Switch the sandbox backend to gVisor first."
                    ),
                )

        self._store.save_live_browser(LiveBrowserSettings(enabled=dto.enabled))
        return _live_browser_from(self._store.load())

    # Build kernel selector (persisted; agent-server reads it per request) --------

    def build_kernel_config(self) -> BuildKernelConfigDTO:
        """The vestigial Build kernel config. Legacy values load as Disco."""
        return _build_kernel_from(self._store.load())

    def update_build_kernel_config(self, dto: BuildKernelConfigDTO) -> BuildKernelConfigDTO:
        """Persist the vestigial Build kernel selector."""
        self._store.save_build_kernel(dto.kind)
        return self.build_kernel_config()

    # Build-project storage path (persisted; agent-server reads it per request) ---

    def projects_config(self) -> ProjectStorageConfigDTO:
        return _projects_from(self._store.load())

    def update_projects_config(self, dto: ProjectStorageConfigDTO) -> ProjectStorageConfigDTO:
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
        return self._mcp_service._mcp_config()

    def _mcp_approvals(self) -> dict[str, dict]:
        return self._mcp_service._mcp_approvals()

    def _mcp_approval_pending(self) -> dict[str, dict]:
        return self._mcp_service._mcp_approval_pending()

    def _mcp_config_approvals(self) -> dict[str, dict]:
        return self._mcp_service._mcp_config_approvals()

    def mcp_connections(self) -> list[McpConnectionDTO]:
        return self._mcp_service.mcp_connections()

    def create_mcp_server(self, body: McpServerConfigDTO) -> McpConnectionDTO:
        return self._mcp_service.create_mcp_server(body)

    def update_mcp_server(self, name: str, patch: McpServerPatchDTO) -> McpConnectionDTO | None:
        return self._mcp_service.update_mcp_server(name, patch)

    def delete_mcp_server(self, name: str) -> bool:
        return self._mcp_service.delete_mcp_server(name)

    def approve_mcp_server(self, name: str, body: McpServerApproveDTO) -> McpConnectionDTO:
        return self._mcp_service.approve_mcp_server(name, body)

    def _mcp_secret_refs(self, srv: dict) -> tuple[str, ...]:
        return self._mcp_service._mcp_secret_refs(srv)

    def mcp_approval_diff(self, name: str, new_hash: str) -> dict | None:
        return self._mcp_service.mcp_approval_diff(name, new_hash)
