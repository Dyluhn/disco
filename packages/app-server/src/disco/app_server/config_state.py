"""Config + scaffold state for the app-server (settings surface, BoD §5.2).

The model catalogue + assignments are derived from the core `RouterConfig`
(llm-router contract §7) — the one source of truth for the deterministic, manual
model story. Skills and MCP are in-memory scaffolds (wiring-pending) mirroring the
frontend fixtures until their subsystems land. All state is session-scoped
(process memory); swap for a persisted config store when wiring beyond demo.

The DTO shapes are the wire contract the frontend's data layer consumes — they
mirror the frontend's `src/types/models.ts` + `src/types/config.ts`.

`ConfigState` (PY-0365) is an aggregate wired over nine domain repositories,
each owning its own persisted records and exposed as a plain `__init__`
attribute (never a `@property` — a property still counts against the class's
public-method cap, a plain attribute costs nothing):

  * `models` (`ConfigModelCatalogue`) — the manual model catalogue.
  * `openrouter` (`ConfigOpenRouterKey`) — the reserved OpenRouter key slot.
  * `providers` (`ProviderConfigService`) — generic provider objects + catalogue
    toggles (already its own collaborator; only the exposure moved here).
  * `secrets_admin` (`ConfigSecretsAdmin`) — the generic provider-secret surface.
  * `integrations` (`ConfigIntegrations`) — Stripe + webhook configuration.
  * `sandbox` (`ConfigSandboxAdmin`) — sandbox backend + connectivity probes.
  * `features` (`ConfigFeatures`) — the LLM-pipeline feature-toggle DTO pairs
    (encoders, TTS, image-gen, data sources, role fallback) + the data-source
    probe.
  * `platform` (`ConfigPlatformSettings`) — the Build-platform feature-toggle
    DTO pairs (live browser, Build kernel, project storage). Split from
    `features` because the seam evaluation's "feature config" grouping (17
    methods) would itself exceed the 12-method cap as one class — see
    `config_features.py`'s module docstring.
  * `skills` (`ConfigSkills`) — the persistent .md skill files.
  * `assignments` (`ConfigAssignments`) — the per-role model assignment matrix.

`ConfigState` itself retains only `approve_origin` plus MCP (via
`_McpConfigStateMixin`, a pre-existing, separately-scoped facade) — it holds no
config-domain state of its own; it wires the repositories together over the
shared `ConfigStore`/`SecretStore` and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core import SkillStore
from disco.core.llm import ConfigStore, RouterConfig, SecretStore
from disco.core.llm.config import McpSettings
from disco.core.quota import SqliteQuotaStore
from disco.core.stripe_host_service import StripeAppConfigStore
from disco.core.webhook_host_service import WebhookAppConfigStore

if TYPE_CHECKING:
    # The shared mcp_approvals DB connection — a duck-typed sqlite3-like conn
    # (execute/commit). Type-only import keeps it off the runtime path.
    from disco.tools.mcp.migrations import _ApprovalConn

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import (
    McpConnectionDTO,
    McpServerApproveDTO,
    McpServerConfigDTO,
    McpServerPatchDTO,
)
from .config_assignments import ConfigAssignments
from .config_features import ConfigFeatures
from .config_integrations import ConfigIntegrations
from .config_model_catalogue import ConfigModelCatalogue
from .config_openrouter_key import ConfigOpenRouterKey
from .config_platform_settings import ConfigPlatformSettings
from .config_sandbox_admin import ConfigSandboxAdmin
from .config_secrets_admin import ConfigSecretsAdmin
from .config_skills import ConfigSkills
from .config_state_errors import ConfigValidationError as ConfigValidationError
from .mcp_config_service import McpConfigService
from .provider_config_service import (
    ProviderConfigService,
)
from .provider_config_service import (
    ProviderInUseError as ProviderInUseError,
)

# ---- the session-scoped config state ----------------------------------------


class _McpConfigStateMixin:
    """MCP configuration facade kept separate from the settings coordinator."""

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

    def update_mcp_server(self, name: str, patch: McpServerPatchDTO) -> McpConnectionDTO | None:
        return self._mcp_service.update_mcp_server(name, patch)

    def delete_mcp_server(self, name: str) -> bool:
        return self._mcp_service.delete_mcp_server(name)

    def revoke_mcp_server(self, name: str) -> McpConnectionDTO | None:
        return self._mcp_service.revoke_mcp_server(name)

    def approve_mcp_server(self, name: str, body: McpServerApproveDTO) -> McpConnectionDTO:
        return self._mcp_service.approve_mcp_server(name, body)

    def _mcp_secret_refs(self, srv: dict) -> tuple[str, ...]:
        return self._mcp_service._mcp_secret_refs(srv)

    def mcp_approval_diff(self, name: str, new_hash: str) -> dict | None:
        return self._mcp_service.mcp_approval_diff(name, new_hash)


class ConfigState(_McpConfigStateMixin):
    """Holds the live config the settings surface reads/writes. The catalogue
    (add/edit/remove a model) AND the per-role assignments are mutable and PERSISTED
    via a shared ConfigStore so the agent-server runtime actually honors them.
    Skills/MCP are wiring-pending scaffolds.

    `store` is the shared config store (defaults to PMX_CONFIG). `config` only seeds
    the catalogue when no store is supplied (test convenience).

    The settings surface itself is decomposed into nine domain repositories (see
    the module docstring) — this aggregate wires them over the shared stores and
    retains only `approve_origin` + the MCP facade."""

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

        self.models = ConfigModelCatalogue(self._store, self._secrets)
        self.openrouter = ConfigOpenRouterKey(self._secrets)
        self.providers = ProviderConfigService(
            self._store,
            self._secrets,
            add_model=lambda body: self.models.add_model(body),
            remove_model=lambda model_id: self.models.remove_model(model_id),
        )
        self.secrets_admin = ConfigSecretsAdmin(self._store, self._secrets)
        self.integrations = ConfigIntegrations(
            self._store,
            self._secrets,
            stripe_configs=stripe_configs,
            webhook_configs=webhook_configs,
        )
        self.sandbox = ConfigSandboxAdmin(self._store)
        self.features = ConfigFeatures(self._store, self._secrets)
        self.platform = ConfigPlatformSettings(self._store)
        self.skills = ConfigSkills(self._skills_store)
        self.assignments = ConfigAssignments(self._store)
        self._mcp_service = McpConfigService(self._store, self._secrets, db_conn)

    def approve_origin(self, url: str, purpose: str, secret_ref: str | None = "") -> None:
        _origin_wiring.approve_origin(self._store, self._secrets, url, purpose, secret_ref)


def app_quota_store(state: ConfigState) -> SqliteQuotaStore:
    """Return the shared quota service or fail closed when host wiring is absent."""
    if state._quota_store is None:
        raise RuntimeError("quota store is not wired to shared host state")
    return state._quota_store
