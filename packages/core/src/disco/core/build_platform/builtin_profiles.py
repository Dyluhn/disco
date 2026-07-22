"""Host-owned built-in declarations used by shadowing and later migration."""

from __future__ import annotations

from .compiler import ModuleBody
from .contracts import (
    BuildProfile,
    CapabilityLayer,
    ComponentId,
    ComponentIntent,
    ComponentKind,
    ConstructionPlan,
    ConstructionRequest,
    DeliveryIntent,
    EntryDescriptor,
    ModuleRef,
    PackagePlan,
    PackageRequest,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
    PreviewPlan,
    TargetPlan,
    TargetRequest,
    TrustLevel,
    VerifierCheck,
    VerifierPlan,
)
from .registry import BuildPlatformRegistry, ComponentSpec

FREEFORM_PROFILE_ID = ComponentId(namespace="disco", name="freeform_web", version="1")
APPKIT_PROFILE_ID = ComponentId(namespace="disco", name="appkit_web", version="1")
FREEFORM_ENGINE_ID = ComponentId(namespace="disco", name="freeform", version="1")
APPKIT_ENGINE_ID = ComponentId(namespace="disco", name="appkit", version="1")
WEB_TARGET_ID = ComponentId(namespace="disco", name="legacy_web", version="1")
HOST_VERIFIER_ID = ComponentId(namespace="disco", name="host_web_verifier", version="1")
HOST_PREVIEW_ID = ComponentId(namespace="disco", name="host_preview", version="1")
LEGACY_EXPORTER_ID = ComponentId(namespace="disco", name="legacy_exporter", version="1")
FREEFORM_PROMPT_ID = ComponentId(namespace="disco", name="freeform_prompt", version="1")
APPKIT_PROMPT_ID = ComponentId(namespace="disco", name="appkit_prompt", version="1")

BUILTIN_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "process.execute",
        "network.egress",
        "display.interactive",
    }
)


def _capabilities(source: str) -> CapabilityLayer:
    return CapabilityLayer(source=source, allowed=BUILTIN_CAPABILITIES)


def _policy(source: str, *rules: PolicyRule) -> PolicyLayer:
    return PolicyLayer(source=source, rules=rules)


FREEFORM_RULES = (PolicyRule(key="mutation.raw_files", decision=PolicyDecision.ALLOW),)
APPKIT_RULES = (
    PolicyRule(
        key="mutation.raw_files",
        decision=PolicyDecision.DENY,
        reason="strict AppKit uses governed semantic mutations",
    ),
    PolicyRule(key="mutation.semantic", decision=PolicyDecision.ALLOW),
)


class _BuiltinEngine:
    def __init__(
        self,
        component_id: ComponentId,
        *,
        requested_tools: frozenset[str],
        rules: tuple[PolicyRule, ...],
    ) -> None:
        self._id = component_id
        self._requested_tools = requested_tools
        self._rules = rules

    @property
    def id(self) -> ComponentId:
        return self._id

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=(
                ComponentIntent(
                    operation="workspace.author",
                    required_capabilities=frozenset({"workspace.write"}),
                ),
            ),
            requested_tools=self._requested_tools,
            required_capabilities=frozenset({"workspace.read", "workspace.write"}),
            policy=self._rules,
        )


class _LegacyWebTarget:
    @property
    def id(self) -> ComponentId:
        return WEB_TARGET_ID

    def plan(self, request: TargetRequest) -> TargetPlan:
        entry = EntryDescriptor(kind="deliverable_manifest", reference="active-deliverable")
        return TargetPlan(
            target=self.id,
            delivery=DeliveryIntent(shape="web.legacy_deliverable", entry=entry),
            preview=PreviewPlan(modality="legacy_host", entry=entry),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="host_verifier",
                        intent=ComponentIntent(
                            operation="host.verify_deliverable",
                            required_capabilities=frozenset({"workspace.read"}),
                        ),
                    ),
                )
            ),
            package=PackagePlan(
                package_shape="web.legacy_archive",
                intents=(
                    ComponentIntent(
                        operation="host.package_deliverable",
                        required_capabilities=frozenset({"workspace.read"}),
                    ),
                ),
            ),
            required_capabilities=frozenset({"workspace.read"}),
        )


class _LegacyExporter:
    @property
    def id(self) -> ComponentId:
        return LEGACY_EXPORTER_ID

    def plan(self, request: PackageRequest) -> PackagePlan:
        return PackagePlan(
            package_shape="web.legacy_archive",
            intents=(
                ComponentIntent(
                    operation="host.package_deliverable",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
            ),
        )


def _spec(
    component_id: ComponentId,
    kind: ComponentKind,
    *,
    rules: tuple[PolicyRule, ...] = (),
    features: frozenset[str] = frozenset(),
) -> ComponentSpec:
    return ComponentSpec(
        id=component_id,
        kind=kind,
        features=features,
        capabilities=_capabilities(component_id.canonical),
        policy=_policy(component_id.canonical, *rules),
    )


def _profile(*, appkit: bool) -> BuildProfile:
    profile_id = APPKIT_PROFILE_ID if appkit else FREEFORM_PROFILE_ID
    engine_id = APPKIT_ENGINE_ID if appkit else FREEFORM_ENGINE_ID
    prompt_id = APPKIT_PROMPT_ID if appkit else FREEFORM_PROMPT_ID
    rules = APPKIT_RULES if appkit else FREEFORM_RULES
    return BuildProfile(
        id=profile_id,
        label="AppKit" if appkit else "Freeform",
        engine=engine_id,
        target=WEB_TARGET_ID,
        verifier=HOST_VERIFIER_ID,
        preview=HOST_PREVIEW_ID,
        exporter=LEGACY_EXPORTER_ID,
        prompt_modules=(
            ModuleRef(
                component=prompt_id,
                trust=TrustLevel.TRUSTED_LOCAL,
                provenance="built-in legacy driver",
            ),
        ),
        compatible_reference_categories=frozenset(
            {"reference_pack", "starter_recipe", "library_recipe"}
        ),
        capabilities=_capabilities(profile_id.canonical),
        policy=_policy(profile_id.canonical, *rules),
    )


def build_builtin_registry(
    *,
    freeform_tools: frozenset[str] = frozenset(),
    appkit_tools: frozenset[str] = frozenset(),
) -> BuildPlatformRegistry:
    """Build the protected host catalog; this is not user-library ingestion."""

    registry = BuildPlatformRegistry()
    registry._register_engine(
        _spec(FREEFORM_ENGINE_ID, ComponentKind.ENGINE, rules=FREEFORM_RULES),
        _BuiltinEngine(
            FREEFORM_ENGINE_ID,
            requested_tools=freeform_tools,
            rules=FREEFORM_RULES,
        ),
    )
    registry._register_engine(
        _spec(APPKIT_ENGINE_ID, ComponentKind.ENGINE, rules=APPKIT_RULES),
        _BuiltinEngine(
            APPKIT_ENGINE_ID,
            requested_tools=appkit_tools,
            rules=APPKIT_RULES,
        ),
    )
    registry._register_target(
        _spec(WEB_TARGET_ID, ComponentKind.TARGET, features=frozenset({"web"})),
        _LegacyWebTarget(),
    )
    registry._register_exporter(
        _spec(LEGACY_EXPORTER_ID, ComponentKind.EXPORTER),
        _LegacyExporter(),
    )
    for component in (
        _spec(HOST_VERIFIER_ID, ComponentKind.VERIFIER),
        _spec(HOST_PREVIEW_ID, ComponentKind.PREVIEW),
        _spec(FREEFORM_PROMPT_ID, ComponentKind.PROMPT_MODULE),
        _spec(APPKIT_PROMPT_ID, ComponentKind.PROMPT_MODULE),
    ):
        registry._register_component(component)
    registry._register_profile(_profile(appkit=False))
    registry._register_profile(_profile(appkit=True))
    return registry


def builtin_module_bodies() -> tuple[ModuleBody, ...]:
    return (
        ModuleBody(
            component=FREEFORM_PROMPT_ID,
            content="Preserve the existing flexible Freeform Build driver composition.",
        ),
        ModuleBody(
            component=APPKIT_PROMPT_ID,
            content="Preserve the existing strict AppKit governed driver composition.",
        ),
    )
