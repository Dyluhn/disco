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

from typing import TYPE_CHECKING

from disco.core import SkillStore
from disco.core.llm import ConfigStore, ModelRole, RouterConfig, SecretStore
from disco.core.llm.config import McpSettings
from disco.core.quota import SqliteQuotaStore
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
from disco.core.webhook_host_service import (
    WEBHOOK_PURPOSE,
    WebhookAppConfigStore,
    WebhookTargetConfig,
    configure_webhook_inbound_secret,
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
    ProbeResult,
    ProjectStorageConfigDTO,
    RoleFallbackConfigDTO,
    SandboxConfigDTO,
    SandboxHealthDTO,
    TtsConfigDTO,
)
from .config.mappers import (
    _assignments_from,
    _build_kernel_from,
    _data_sources_from,
    _encoders_from,
    _image_gen_from,
    _live_browser_from,
    _projects_from,
    _role_fallback_from,
    _sandbox_from,
    _tts_from,
)
from .general_config_service import (
    ModelConfigStateAdapter,
    ProviderConfigStateAdapter,
    SecretConfigStateAdapter,
    SkillConfigStateAdapter,
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


class _McpConfigStateMixin:
    """Unchanged MCP compatibility delegate retained for PKG-11-MCP."""

    _mcp_service: McpConfigService

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

    def update_mcp_server(
        self,
        name: str,
        patch: McpServerPatchDTO,
    ) -> McpConnectionDTO | None:
        return self._mcp_service.update_mcp_server(name, patch)

    def delete_mcp_server(self, name: str) -> bool:
        return self._mcp_service.delete_mcp_server(name)

    def revoke_mcp_server(self, name: str) -> McpConnectionDTO | None:
        return self._mcp_service.revoke_mcp_server(name)

    def approve_mcp_server(
        self,
        name: str,
        body: McpServerApproveDTO,
    ) -> McpConnectionDTO:
        return self._mcp_service.approve_mcp_server(name, body)

    def _mcp_secret_refs(self, server: dict) -> tuple[str, ...]:
        return self._mcp_service._mcp_secret_refs(server)

    def mcp_approval_diff(self, name: str, new_hash: str) -> dict | None:
        return self._mcp_service.mcp_approval_diff(name, new_hash)


class _ConfigStateComposition(
    _McpConfigStateMixin,
    ModelConfigStateAdapter,
    ProviderConfigStateAdapter,
    SecretConfigStateAdapter,
    SkillConfigStateAdapter,
):
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
        webhook_configs: WebhookAppConfigStore | None = None,
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
        self._webhook_configs = webhook_configs
        self._provider_config = ProviderConfigService(
            self._store,
            self._secrets,
            add_model=lambda body: self.add_model(body),
            remove_model=lambda model_id: self.remove_model(model_id),
        )
        self._mcp_service = McpConfigService(self._store, self._secrets, db_conn)


class _IntegrationConfigState(_ConfigStateComposition):
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

    def configure_webhook_inbound(
        self,
        *,
        owner_id: str,
        audience: str,
        signing_secret: str,
    ) -> None:
        """Store an app-scoped inbound verifier secret without exposing it."""
        configure_webhook_inbound_secret(
            self._secrets,
            owner_id,
            audience,
            signing_secret,
        )

    def configure_webhook_outbound(
        self,
        *,
        owner_id: str,
        audience: str,
        endpoint_id: str,
        target_url: str,
        signing_secret: str,
        event_types: frozenset[str],
        enabled: bool,
    ) -> WebhookTargetConfig:
        """Store one owner/app-scoped outbound target and approve its exact origin."""
        if self._webhook_configs is None:
            raise RuntimeError("Webhook configuration store is not wired to shared host state")
        config = self._webhook_configs.configure(
            owner_id=owner_id,
            audience=audience,
            endpoint_id=endpoint_id,
            target_url=target_url,
            signing_secret=signing_secret,
            event_types=event_types,
            enabled=enabled,
            secret_store=self._secrets,
        )
        self._approve_origin(config.target_url, WEBHOOK_PURPOSE, config.secret_ref)
        return config

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


class _ExecutionConfigState(_IntegrationConfigState):
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


class _MediaConfigState(_ExecutionConfigState):
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


class _SurfaceConfigState(_MediaConfigState):
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


class ConfigState(_SurfaceConfigState):
    """Source-compatible, state-free settings composition adapter."""


def app_quota_store(state: ConfigState) -> SqliteQuotaStore:
    """Return the shared quota service or fail closed when host wiring is absent."""
    if state._quota_store is None:
        raise RuntimeError("quota store is not wired to shared host state")
    return state._quota_store
