"""Wiring-only construction for ``ConversationRuntime``."""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import base64
import logging

from disco.core import SkillStore
from disco.core.inspect import inspect_enabled
from disco.core.inspect import install as install_inspect
from disco.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    OperatingMode,
    SecretStore,
)
from disco.core.llm.config import RouterConfig
from disco.core.store.sqlite import SqliteEventStore
from disco.tools import SandboxService, SandboxSpec

from .build_composition_ports import (
    BuildExecutorAccess,
)
from .build_platform_runtime import AppKitEjectionLedger
from .connection_tracker import ConnectionState
from .driver_context_state import DriverContextState
from .driver_runtime import DriverPreflight, DriverRuntime
from .driver_runtime_ports import (
    RuntimeDriverProbeSeams,
    RuntimeDriverPromptState,
    RuntimeDriverSelections,
)
from .lifecycle_ports import (
    LifecycleIdleSweepDeps,
)
from .mcp_manager import McpManager
from .preview_capture_ownership import PreviewCaptureOwnership
from .project_runtime_service import ProjectRuntimeService
from .reference_pack_runtime import ReferencePackRuntime
from .run_registry import (
    CancellationRegistry,
    KernelPinStore,
    LoopRegistry,
    RunAuthorityLedger,
    RunIngressLedger,
    RunRecoveryLedger,
    RunRegistry,
    RunResourceRegistry,
)
from .runtime_settings import (
    RuntimeSettings,
    _RuntimeModelBindings,
    _RuntimeSettingsRouting,
)
from .runtime_wiring_schema import _RuntimeWiringSchema
from .sandbox_runtime_service import SandboxRuntimeService
from .suggestion_service import SuggestionService
from .title_service import TitleService
from .upload_store import UploadStore
from .vision_inspection import VisionInspectionCapability
from .workspace_fence import WorkspaceFenceService

_LOG = logging.getLogger(__name__)


def _config_store(
    config: RouterConfig | None,
    config_store: ConfigStore | None,
) -> ConfigStore:
    if config_store is not None:
        return config_store
    if config is None:
        return ConfigStore()
    result = ConfigStore(base_factory=lambda: config)
    if result.path.exists():
        _LOG.warning(
            "ConversationRuntime(config=...) is shadowed by %s; pass "
            "config_store= or point DISCO_CONFIG at the intended file",
            result.path,
        )
    return result


def _wire_foundation(
    rt: _RuntimeWiringSchema,
    store: SqliteEventStore,
    *,
    db_path: str,
    config: RouterConfig | None,
    config_store: ConfigStore | None,
    secret_store: SecretStore | None,
    router: DefaultLLMRouter | None,
    enable_thinking: bool,
    mode: OperatingMode,
    sandbox_service: SandboxService | None,
    sandbox_spec: SandboxSpec | None,
    skill_store: SkillStore | None,
) -> None:
    rt._store = store
    rt._skill_store = skill_store or SkillStore()
    rt.uploads = UploadStore(f"{db_path}.uploads" if db_path else "")
    rt._secret_store = secret_store or SecretStore()
    rt._config_store = _config_store(config, config_store)

    # One host-owned Reference Pack runtime is shared by CRUD routes and every
    # later Agent/Build composition.  The tool package receives only the
    # narrow per-executor seam; it never discovers this service itself.
    def reference_visual_observer(conversation_id: str):
        """Adapt the existing bounded visual capability to pack image bytes."""

        capability = VisionInspectionCapability(
            rt._router_now(conversation_id=conversation_id),
            conversation_id=conversation_id,
        )

        async def inspect_image(data: bytes, file_name: str, question: str) -> object:
            return await capability.inspect(
                question=question,
                screenshot_b64=base64.b64encode(data).decode("ascii"),
                screenshot_path=file_name,
            )

        return inspect_image

    rt.reference_packs = ReferencePackRuntime(
        event_store=store,
        visual_observer_factory=reference_visual_observer,
    )
    rt.sandbox = SandboxRuntimeService(
        rt._config_store,
        sandbox_service,
        sandbox_spec or SandboxSpec(),
    )
    rt.projects = ProjectRuntimeService(rt._config_store, store)
    if inspect_enabled():
        install_inspect()

    rt._loop_registry = LoopRegistry()
    rt.run_registry = RunRegistry()
    rt._run_authorities = RunAuthorityLedger()
    rt._run_ingress = RunIngressLedger()
    rt._run_resources = RunResourceRegistry()
    rt._run_recovery = RunRecoveryLedger()
    rt._driver_contexts = DriverContextState(timeout_s=5.0)
    rt._appkit_ejections = AppKitEjectionLedger()
    executors = BuildExecutorAccess(rt._run_resources)
    rt.settings = RuntimeSettings(
        db_path=db_path,
        store=store,
        config_store=rt._config_store,
        routing=_RuntimeSettingsRouting(rt._config_store, rt._secret_store, router),
        model_bindings=_RuntimeModelBindings(
            loops=rt._loop_registry,
            runs=rt.run_registry,
            contexts=rt._driver_contexts,
            resources=rt._run_resources,
        ),
        appkit_ejections=rt._appkit_ejections,
    )
    rt.drivers = DriverRuntime(
        config_store=rt._config_store,
        secret_store=rt._secret_store,
        skill_store=rt._skill_store,
        prompt_state=RuntimeDriverPromptState(executors),
        selections=RuntimeDriverSelections(rt.settings),
        probes=RuntimeDriverProbeSeams(),
        contexts=rt._driver_contexts,
        injected_router=router,
        enable_thinking=enable_thinking,
    )
    rt._driver_preflight = DriverPreflight(rt.drivers)
    rt.drivers.bind_readiness_observer(rt._driver_preflight.observe_success)
    rt._cancellations = CancellationRegistry()
    rt.mcp = McpManager(rt._config_store, rt._secret_store, store)
    rt._kernel_pin_store = KernelPinStore()
    rt._connection_state = ConnectionState()
    rt._preview_capture_ownership = PreviewCaptureOwnership()
    rt._lifecycle_idle = LifecycleIdleSweepDeps(rt._config_store)
    rt._workspace_fence = WorkspaceFenceService(
        rt._store,
        rt.projects,
        rt.settings,
    )
    rt.titles = TitleService(store, rt._router_now)
    rt.suggestions = SuggestionService(
        rt._router_now,
        lambda: rt.projects.current_project_store().root or "",
    )
