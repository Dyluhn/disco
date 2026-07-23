"""Host-owned built-in declarations used by shadowing and later migration."""

from __future__ import annotations

from ..verification import (
    HostVerificationClaim,
    VerificationClaimKind,
    default_structured_web_claims,
)
from .builtin_inputs import builtin_input_catalog
from .compiler import ModuleBody, PromptContextInputs, ToolDescriptor
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
from .resolver import BuildComposition, ResolutionInputs, resolve_build_composition

FREEFORM_PROFILE_ID = ComponentId(namespace="disco", name="freeform_web", version="1")
APPKIT_PROFILE_ID = ComponentId(namespace="disco", name="appkit_web", version="1")
FREEFORM_ENGINE_ID = ComponentId(namespace="disco", name="freeform", version="1")
APPKIT_ENGINE_ID = ComponentId(namespace="disco", name="appkit", version="1")
WEB_TARGET_ID = ComponentId(namespace="disco", name="legacy_web", version="1")
HOST_VERIFIER_ID = ComponentId(namespace="disco", name="host_web_verifier", version="1")
APPKIT_VERIFIER_ID = ComponentId(namespace="disco", name="appkit_strict_verifier", version="1")
MODEL_VERIFIER_ID = ComponentId(namespace="model_role", name="verifier", version="1")
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

# These are descriptions of the existing host lifecycle, not executable hooks.
# Keeping them in the Freeform engine plan makes the migration contract explicit
# while the legacy AgentLoop/WorkspaceCoordinator retain every effect authority.
FREEFORM_LIFECYCLE_OPERATIONS = (
    "host.plan_approval",
    "workspace.author",
    "host.commit_revision",
    "host.recover_workspace",
    "host.finish_after_verification",
)

# AppKit remains a governed product, not a constrained spelling of Freeform.
# These inert intents describe the existing strict lifecycle while the semantic
# tools, generator, verifier, revision store, and deployment mediator retain all
# effect authority.
APPKIT_LIFECYCLE_OPERATIONS = (
    "host.plan_approval",
    "appkit.scaffold_governed",
    "appkit.mutate_semantic",
    "appkit.govern_session_auth",
    "appkit.govern_rbac",
    "appkit.govern_persistence",
    "host.commit_revision",
    "host.eject_to_freeform_revision",
    "host.recover_workspace",
    "host.verify_appkit_strict",
    "host.deploy_mediated",
    "host.finish_after_verification",
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


class _FreeformEngine(_BuiltinEngine):
    """Typed description of the current flexible Build execution contract."""

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=tuple(
                ComponentIntent(
                    operation=operation,
                    required_capabilities=(
                        frozenset({"workspace.write"})
                        if operation == "workspace.author"
                        else frozenset()
                    ),
                )
                for operation in FREEFORM_LIFECYCLE_OPERATIONS
            ),
            requested_tools=self._requested_tools,
            required_capabilities=frozenset({"workspace.read", "workspace.write"}),
            policy=self._rules,
        )


class _AppKitEngine(_BuiltinEngine):
    """Typed description of the current governed AppKit execution contract."""

    _WRITE_OPERATIONS = frozenset(
        {
            "appkit.scaffold_governed",
            "appkit.mutate_semantic",
            "host.commit_revision",
            "host.eject_to_freeform_revision",
        }
    )
    _READ_OPERATIONS = frozenset(
        {
            "host.recover_workspace",
            "host.verify_appkit_strict",
            "host.deploy_mediated",
        }
    )

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=tuple(
                ComponentIntent(
                    operation=operation,
                    required_capabilities=(
                        frozenset({"workspace.write"})
                        if operation in self._WRITE_OPERATIONS
                        else (
                            frozenset({"workspace.read"})
                            if operation in self._READ_OPERATIONS
                            else frozenset()
                        )
                    ),
                )
                for operation in APPKIT_LIFECYCLE_OPERATIONS
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
        appkit = request.engine == APPKIT_ENGINE_ID
        check = (
            VerifierCheck(
                check_id="appkit_strict",
                issuer=APPKIT_VERIFIER_ID,
                receipt_kind="disco.appkit_strict@1",
                required_execution_modality="appkit_strict_runtime",
                required_artifact_identity_scheme="sha256-tree-manifest-v1",
                intent=ComponentIntent(
                    operation="host.verify_appkit_strict",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
                accepted_claim_kinds=frozenset({VerificationClaimKind.TARGET_SPECIFIC}),
                claims=(
                    HostVerificationClaim(
                        claim_id="appkit.strict_contract",
                        kind=VerificationClaimKind.TARGET_SPECIFIC,
                        expected=(
                            "canonical AppKit entry passes the complete strict target verifier"
                        ),
                        source_authority="target.appkit.strict_floor@1",
                    ),
                ),
            )
            if appkit
            else VerifierCheck(
                check_id="web_functional",
                issuer=HOST_VERIFIER_ID,
                receipt_kind="disco.web_functional@1",
                required_execution_modality="managed_preview",
                delegated_issuers=(MODEL_VERIFIER_ID,),
                intent=ComponentIntent(
                    operation="host.verify_deliverable",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
                accepted_claim_kinds=frozenset(
                    {
                        VerificationClaimKind.ARTIFACT_IDENTITY,
                        VerificationClaimKind.HTTP_READY,
                        VerificationClaimKind.RENDERED_CONTENT,
                        VerificationClaimKind.VISIBLE_TEXT,
                        VerificationClaimKind.CONSOLE_CLEAN,
                        VerificationClaimKind.NETWORK_CLEAN,
                        VerificationClaimKind.INTERACTION,
                        VerificationClaimKind.ROUTE,
                        VerificationClaimKind.CONTRACT_SEMANTIC,
                        VerificationClaimKind.VISUAL_SEMANTIC,
                    }
                ),
                claims=default_structured_web_claims(),
            )
        )
        return TargetPlan(
            target=self.id,
            intents=(
                ComponentIntent(
                    operation="host.detect_delivery",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
                ComponentIntent(
                    operation="host.bind_revision",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
            ),
            delivery=DeliveryIntent(
                shape="web.legacy_deliverable",
                entry=entry,
                mode="interactive",
            ),
            preview=PreviewPlan(modality="legacy_host", entry=entry),
            verifier=VerifierPlan(checks=(check,)),
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
        verifier=APPKIT_VERIFIER_ID if appkit else HOST_VERIFIER_ID,
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
        _FreeformEngine(
            FREEFORM_ENGINE_ID,
            requested_tools=freeform_tools,
            rules=FREEFORM_RULES,
        ),
    )
    registry._register_engine(
        _spec(APPKIT_ENGINE_ID, ComponentKind.ENGINE, rules=APPKIT_RULES),
        _AppKitEngine(
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
        _spec(APPKIT_VERIFIER_ID, ComponentKind.VERIFIER),
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


def resolve_builtin_composition(
    *,
    appkit: bool,
    goal: str,
    tool_catalog: tuple[ToolDescriptor, ...],
    visible_tools: frozenset[str],
    requested_reference_categories: frozenset[str] = frozenset(),
) -> BuildComposition:
    """Resolve one built-in profile from the exact legacy-visible tool surface.

    This pure seam is shared by observe-only comparison and the Phase-4 new-run
    admission path.  It cannot execute a tool or mutate runtime state.
    """

    registry = build_builtin_registry(
        freeform_tools=(frozenset() if appkit else visible_tools),
        appkit_tools=(visible_tools if appkit else frozenset()),
    )
    profile_id = APPKIT_PROFILE_ID if appkit else FREEFORM_PROFILE_ID
    profile = registry.profile(profile_id)
    if profile is None or not profile.prompt_modules:
        raise ValueError("built-in profile is incomplete")
    prompt_id = profile.prompt_modules[0].component
    bodies = tuple(body for body in builtin_module_bodies() if body.component == prompt_id)
    if len(bodies) != 1 or not isinstance(bodies[0], ModuleBody):
        raise ValueError("built-in prompt body is not unique")
    layer = CapabilityLayer(source="built-in", allowed=BUILTIN_CAPABILITIES)
    return resolve_build_composition(
        registry,
        ResolutionInputs(
            profile=profile_id,
            goal=goal,
            platform_capabilities=layer.model_copy(update={"source": "platform"}),
            user_capabilities=layer.model_copy(update={"source": "user"}),
            host_capabilities=layer.model_copy(update={"source": "host"}),
            platform_policy=PolicyLayer(source="platform"),
            user_policy=PolicyLayer(source="user"),
            requested_reference_categories=requested_reference_categories,
            prompt_context=PromptContextInputs(
                module_bodies=bodies,
                tool_catalog=tool_catalog,
                host_visible_tools=visible_tools,
            ),
            builtin_input_owners=builtin_input_catalog().items,
        ),
    )
