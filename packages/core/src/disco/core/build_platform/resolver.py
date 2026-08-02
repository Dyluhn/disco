"""The sole deterministic constructor of an inspectable Build composition."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import Field

from .builtin_inputs import BuiltinInputOwner
from .compiler import (
    CompositionCompileError,
    PromptContextComposition,
    PromptContextInputs,
    compile_prompt_context,
)
from .contracts import (
    BuildProfile,
    CapabilityDenial,
    CapabilityLayer,
    CapabilitySupport,
    ComponentId,
    ComponentKind,
    ConstructionEngine,
    ConstructionPlan,
    ConstructionRequest,
    EffectiveCapabilityPolicy,
    EffectivePolicy,
    FrozenModel,
    ModuleRef,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
    TargetAdapter,
    TargetPlan,
    TargetRequest,
)
from .identity import CompositionDigest, composition_digest
from .registry import BuildPlatformRegistry, ComponentSpec


class ResolutionError(ValueError):
    """A required profile/engine/target cannot be resolved exactly."""


class OperationBlock(FrozenModel):
    operation: str = Field(min_length=1, max_length=96)
    code: str = Field(min_length=1, max_length=96)
    detail: str
    component: ComponentId | None = None
    missing_capabilities: tuple[str, ...] = ()


class ResolutionInputs(FrozenModel):
    profile: ComponentId
    goal: str
    platform_capabilities: CapabilityLayer
    user_capabilities: CapabilityLayer
    host_capabilities: CapabilityLayer
    host_support: tuple[CapabilitySupport, ...] = ()
    platform_policy: PolicyLayer
    user_policy: PolicyLayer
    requested_reference_categories: frozenset[str] = frozenset()
    prompt_context: PromptContextInputs = PromptContextInputs()
    builtin_input_owners: tuple[BuiltinInputOwner, ...] = ()


class BuildComposition(FrozenModel):
    """The one typed composition record. Constructed only by this resolver."""

    schema_version: Literal[1] = 1
    profile: BuildProfile
    engine: ComponentId
    target: ComponentId
    verifier: ComponentId
    preview: ComponentId
    exporter: ComponentId | None
    connector: ComponentId | None
    construction: ConstructionPlan
    target_plan: TargetPlan
    prompt_modules: tuple[ModuleRef, ...]
    context_modules: tuple[ModuleRef, ...]
    compatible_reference_categories: tuple[str, ...]
    required_runtime_capabilities: tuple[str, ...]
    effective_capabilities: EffectiveCapabilityPolicy
    effective_policy: EffectivePolicy
    prompt_context: PromptContextComposition
    builtin_input_owners: tuple[BuiltinInputOwner, ...] = ()
    blocked_operations: tuple[OperationBlock, ...] = ()

    def blocked(self, operation: str) -> bool:
        return any(block.operation == operation for block in self.blocked_operations)

    @property
    def digest(self) -> CompositionDigest:
        return composition_digest(self)


def _denial_reasons(layer: CapabilityLayer) -> dict[str, str]:
    return {
        item.name: item.value
        for item in layer.denials
        if isinstance(item.value, str) and item.value
    }


def _allowed_capabilities(
    layers: tuple[CapabilityLayer, ...],
    support_by_name: dict[str, CapabilitySupport],
) -> set[str]:
    allowed = set.intersection(*(set(layer.allowed) for layer in layers)) if layers else set()
    return {
        capability
        for capability in allowed
        if support_by_name.get(capability) is None
        or support_by_name[capability].level.value != "unsupported"
    }


def _capability_denial(
    capability: str,
    layers: tuple[CapabilityLayer, ...],
    support_by_name: dict[str, CapabilitySupport],
) -> CapabilityDenial:
    denied_by = [layer.source for layer in layers if capability not in layer.allowed]
    host_item = support_by_name.get(capability)
    if host_item is not None and host_item.level.value == "unsupported":
        denied_by.append("host-support")
    reasons = [
        _denial_reasons(layer).get(capability, "")
        for layer in layers
        if capability not in layer.allowed
    ]
    if host_item is not None and host_item.level.value == "unsupported" and host_item.detail:
        reasons.append(host_item.detail)
    reason = "; ".join(reason for reason in reasons if reason) or "denied by capability ceiling"
    return CapabilityDenial(
        capability=capability,
        denied_by=tuple(sorted(set(denied_by))),
        reason=reason,
    )


def _effective_capabilities(
    layers: tuple[CapabilityLayer, ...],
    *,
    required: frozenset[str],
    support: tuple[CapabilitySupport, ...],
) -> EffectiveCapabilityPolicy:
    candidates = set(required)
    for layer in layers:
        candidates.update(layer.allowed)
    support_by_name = {item.capability: item for item in support}
    allowed = _allowed_capabilities(layers, support_by_name)
    denied: list[CapabilityDenial] = []
    for capability in sorted(candidates - allowed):
        denied.append(_capability_denial(capability, layers, support_by_name))
    return EffectiveCapabilityPolicy(
        allowed=frozenset(allowed),
        denied=tuple(denied),
        supports=tuple(sorted(support, key=lambda item: item.capability)),
    )


def _effective_policy(layers: tuple[PolicyLayer, ...]) -> EffectivePolicy:
    by_key: dict[str, list[tuple[str, PolicyRule]]] = {}
    for layer in layers:
        for rule in layer.rules:
            by_key.setdefault(rule.key, []).append((layer.source, rule))
    effective: list[PolicyRule] = []
    for key in sorted(by_key):
        entries = by_key[key]
        denials = [
            (source, rule) for source, rule in entries if rule.decision is PolicyDecision.DENY
        ]
        if denials:
            sources = ", ".join(sorted(source for source, _rule in denials))
            details = "; ".join(rule.reason for _source, rule in denials if rule.reason)
            reason = f"denied by {sources}" + (f": {details}" if details else "")
            effective.append(PolicyRule(key=key, decision=PolicyDecision.DENY, reason=reason))
        else:
            effective.append(PolicyRule(key=key, decision=PolicyDecision.ALLOW))
    return EffectivePolicy(rules=tuple(effective))


def _intent_capabilities(intents: Iterable[object]) -> frozenset[str]:
    required: set[str] = set()
    for intent in intents:
        required.update(getattr(intent, "required_capabilities", frozenset()))
    return frozenset(required)


def _plan_requirements(
    construction: ConstructionPlan, target: TargetPlan
) -> dict[str, frozenset[str]]:
    deployment_intents = tuple(
        intent for deployment in target.deployments for intent in deployment.intents
    )
    return {
        "construct": construction.required_capabilities
        | _intent_capabilities(construction.intents),
        "target": target.required_capabilities | _intent_capabilities(target.intents),
        "preview": target.preview.required_capabilities
        | _intent_capabilities(target.preview.intents),
        "verify": _intent_capabilities(check.intent for check in target.verifier.checks),
        "package": (
            _intent_capabilities(target.package.intents)
            if target.package is not None
            else frozenset()
        ),
        "deploy": _intent_capabilities(deployment_intents),
    }


def _component_for_kind(profile: BuildProfile, kind: ComponentKind) -> ComponentId | None:
    return {
        ComponentKind.PROFILE: profile.id,
        ComponentKind.ENGINE: profile.engine,
        ComponentKind.TARGET: profile.target,
        ComponentKind.VERIFIER: profile.verifier,
        ComponentKind.PREVIEW: profile.preview,
        ComponentKind.EXPORTER: profile.exporter,
        ComponentKind.CONNECTOR: profile.connector,
        ComponentKind.PROMPT_MODULE: None,
        ComponentKind.CONTEXT_MODULE: None,
    }[kind]


def _compatibility_blocks(
    registry: BuildPlatformRegistry, profile: BuildProfile
) -> list[OperationBlock]:
    blocks: list[OperationBlock] = []
    for requirement in profile.requirements.components:
        selected = _component_for_kind(profile, requirement.kind)
        if selected is None:
            blocks.append(
                OperationBlock(
                    operation=requirement.operation,
                    code="missing_component",
                    detail=f"profile does not select a required {requirement.kind.value}",
                )
            )
            continue
        spec = registry.components.spec(selected)
        if spec is None:
            blocks.append(
                OperationBlock(
                    operation=requirement.operation,
                    code="missing_component",
                    detail=f"component {selected.canonical} is not registered",
                    component=selected,
                )
            )
            continue
        if requirement.accepted and selected not in requirement.accepted:
            blocks.append(
                OperationBlock(
                    operation=requirement.operation,
                    code="incompatible_component",
                    detail=(f"component {selected.canonical} is not in the accepted exact-ID set"),
                    component=selected,
                )
            )
        missing_features = requirement.required_features - spec.features
        if missing_features:
            blocks.append(
                OperationBlock(
                    operation=requirement.operation,
                    code="missing_features",
                    detail="component lacks required features: "
                    + ", ".join(sorted(missing_features)),
                    component=selected,
                )
            )
    return blocks


def _registered_plan_component_blocks(
    registry: BuildPlatformRegistry, profile: BuildProfile
) -> list[OperationBlock]:
    blocks: list[OperationBlock] = []
    for operation, component, expected_kind in (
        ("verify", profile.verifier, ComponentKind.VERIFIER),
        ("preview", profile.preview, ComponentKind.PREVIEW),
    ):
        spec = registry.components.spec(component)
        if spec is None or spec.kind is not expected_kind:
            blocks.append(
                OperationBlock(
                    operation=operation,
                    code="missing_component",
                    detail=(f"exact {expected_kind.value} {component.canonical} is not registered"),
                    component=component,
                )
            )
    if profile.exporter is not None and registry.components.exporter(profile.exporter) is None:
        blocks.append(
            OperationBlock(
                operation="package",
                code="missing_component",
                detail=f"exact exporter {profile.exporter.canonical} is not registered",
                component=profile.exporter,
            )
        )
    if profile.connector is not None and registry.components.connector(profile.connector) is None:
        blocks.append(
            OperationBlock(
                operation="deploy",
                code="missing_component",
                detail=f"exact connector {profile.connector.canonical} is not registered",
                component=profile.connector,
            )
        )
    return blocks


def _module_blocks(registry: BuildPlatformRegistry, profile: BuildProfile) -> list[OperationBlock]:
    blocks: list[OperationBlock] = []
    modules = tuple(
        (module, ComponentKind.PROMPT_MODULE) for module in profile.prompt_modules
    ) + tuple((module, ComponentKind.CONTEXT_MODULE) for module in profile.context_modules)
    for module, expected_kind in modules:
        spec = registry.components.spec(module.component)
        if spec is None or spec.kind is not expected_kind:
            blocks.append(
                OperationBlock(
                    operation="construct",
                    code="missing_component",
                    detail=(
                        f"exact {expected_kind.value} {module.component.canonical} "
                        "is not registered"
                    ),
                    component=module.component,
                )
            )
    return blocks


def _resolve_exact_components(
    registry: BuildPlatformRegistry, profile_id: ComponentId
) -> tuple[BuildProfile, ConstructionEngine, TargetAdapter, ComponentSpec, ComponentSpec]:
    profile = registry.profiles.get(profile_id)
    if profile is None:
        raise ResolutionError(f"build profile {profile_id.canonical} is not registered")
    engine = registry.components.engine(profile.engine)
    if engine is None:
        raise ResolutionError(f"construction engine {profile.engine.canonical} is not registered")
    target = registry.components.target(profile.target)
    if target is None:
        raise ResolutionError(f"target adapter {profile.target.canonical} is not registered")
    engine_spec = registry.components.spec(profile.engine)
    target_spec = registry.components.spec(profile.target)
    if engine_spec is None or target_spec is None:
        raise ResolutionError("required engine/target declaration is missing")
    return profile, engine, target, engine_spec, target_spec


def _build_plans(
    profile: BuildProfile, engine: ConstructionEngine, target: TargetAdapter, goal: str
) -> tuple[ConstructionPlan, TargetPlan]:
    try:
        construction = engine.plan(
            ConstructionRequest(
                profile=profile.id,
                goal=goal,
                modules=profile.prompt_modules + profile.context_modules,
            )
        )
        target_plan = target.plan(
            TargetRequest(profile=profile.id, engine=profile.engine, goal=goal)
        )
    except Exception as exc:
        raise ResolutionError(f"component planning failed closed: {type(exc).__name__}") from exc
    if not isinstance(construction, ConstructionPlan) or construction.engine != profile.engine:
        raise ResolutionError("construction engine returned a malformed or mismatched plan")
    if not isinstance(target_plan, TargetPlan) or target_plan.target != profile.target:
        raise ResolutionError("target adapter returned a malformed or mismatched plan")
    return construction, target_plan


def _collect_component_specs(
    registry: BuildPlatformRegistry,
    profile: BuildProfile,
    engine_spec: ComponentSpec,
    target_spec: ComponentSpec,
) -> list[ComponentSpec]:
    component_specs: list[ComponentSpec] = [engine_spec, target_spec]
    component_ids = (
        profile.verifier,
        profile.preview,
        profile.exporter,
        profile.connector,
        *(module.component for module in profile.prompt_modules),
        *(module.component for module in profile.context_modules),
    )
    for component_id in component_ids:
        if component_id is None:
            continue
        spec = registry.components.spec(component_id)
        if spec is not None:
            component_specs.append(spec)
    return component_specs


def _operation_blocks(
    registry: BuildPlatformRegistry,
    profile: BuildProfile,
    prompt_context: PromptContextComposition,
    inputs: ResolutionInputs,
    plan_requirements: dict[str, frozenset[str]],
    effective_capabilities: EffectiveCapabilityPolicy,
) -> list[OperationBlock]:
    operation_blocks = _registered_plan_component_blocks(registry, profile)
    operation_blocks.extend(_compatibility_blocks(registry, profile))
    operation_blocks.extend(_module_blocks(registry, profile))
    for denial in prompt_context.denials:
        operation_blocks.append(
            OperationBlock(
                operation=("construct" if denial.operation in {"construct", "tool"} else "context"),
                code=denial.code,
                detail=f"{denial.subject}: {denial.detail}",
            )
        )
    incompatible_references = (
        inputs.requested_reference_categories - profile.compatible_reference_categories
    )
    for category in sorted(incompatible_references):
        operation_blocks.append(
            OperationBlock(
                operation="context",
                code="incompatible_reference_category",
                detail=(f"reference category {category!r} is not compatible with this profile"),
            )
        )
    for operation, operation_required in plan_requirements.items():
        missing = tuple(sorted(operation_required - effective_capabilities.allowed))
        if missing:
            operation_blocks.append(
                OperationBlock(
                    operation=operation,
                    code="capability_denied",
                    detail=("required capabilities were denied by the effective intersection"),
                    missing_capabilities=missing,
                )
            )
    missing_profile_capabilities = tuple(
        sorted(profile.requirements.required_capabilities - effective_capabilities.allowed)
    )
    if missing_profile_capabilities:
        operation_blocks.append(
            OperationBlock(
                operation="construct",
                code="capability_denied",
                detail=("profile-required capabilities were denied by the effective intersection"),
                missing_capabilities=missing_profile_capabilities,
            )
        )
    return operation_blocks


def resolve_build_composition(
    registry: BuildPlatformRegistry, inputs: ResolutionInputs
) -> BuildComposition:
    """Resolve one exact profile with no downgrade, alias, or fallback path."""

    profile, engine, target, engine_spec, target_spec = _resolve_exact_components(
        registry, inputs.profile
    )
    construction, target_plan = _build_plans(profile, engine, target, inputs.goal)
    component_specs = _collect_component_specs(registry, profile, engine_spec, target_spec)

    plan_requirements = _plan_requirements(construction, target_plan)
    required = frozenset().union(
        profile.requirements.required_capabilities,
        *(requirements for requirements in plan_requirements.values()),
    )
    capability_layers = (
        inputs.platform_capabilities,
        inputs.user_capabilities,
        inputs.host_capabilities,
        profile.capabilities,
        *(spec.capabilities for spec in component_specs),
    )
    effective_capabilities = _effective_capabilities(
        capability_layers,
        required=required,
        support=inputs.host_support,
    )
    policy_layers = (
        inputs.platform_policy,
        inputs.user_policy,
        profile.policy,
        *(spec.policy for spec in component_specs),
        PolicyLayer(source=construction.engine.canonical, rules=construction.policy),
        PolicyLayer(source=target_plan.target.canonical, rules=target_plan.policy),
    )
    effective_policy = _effective_policy(policy_layers)

    try:
        prompt_context = compile_prompt_context(
            prompt_modules=profile.prompt_modules,
            context_modules=profile.context_modules,
            requested_tools=construction.requested_tools,
            effective_capabilities=effective_capabilities,
            effective_policy=effective_policy,
            inputs=inputs.prompt_context,
        )
    except CompositionCompileError as exc:
        raise ResolutionError(f"prompt/context compilation failed closed: {exc}") from exc

    operation_blocks = _operation_blocks(
        registry, profile, prompt_context, inputs, plan_requirements, effective_capabilities
    )

    serialized_blocks = sorted(set(block.model_dump_json() for block in operation_blocks))
    normalized_blocks = tuple(
        OperationBlock.model_validate_json(block) for block in serialized_blocks
    )
    return BuildComposition(
        profile=profile,
        engine=profile.engine,
        target=profile.target,
        verifier=profile.verifier,
        preview=profile.preview,
        exporter=profile.exporter,
        connector=profile.connector,
        construction=construction,
        target_plan=target_plan,
        prompt_modules=profile.prompt_modules,
        context_modules=profile.context_modules,
        compatible_reference_categories=tuple(sorted(profile.compatible_reference_categories)),
        required_runtime_capabilities=tuple(sorted(required)),
        effective_capabilities=effective_capabilities,
        effective_policy=effective_policy,
        prompt_context=prompt_context,
        builtin_input_owners=inputs.builtin_input_owners,
        blocked_operations=normalized_blocks,
    )
