from __future__ import annotations

from typing import ClassVar

import pytest
from disco.core.build_platform import (
    BuildPlatformRegistry,
    BuildProfile,
    CapabilityLayer,
    CompatibilityRequirements,
    ComponentId,
    ComponentIntent,
    ComponentKind,
    ComponentRequirement,
    ComponentSpec,
    ConstructionPlan,
    ConstructionRequest,
    DeliveryIntent,
    EntryDescriptor,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
    PreviewPlan,
    RegistryError,
    ResolutionError,
    ResolutionInputs,
    TargetPlan,
    TargetRequest,
    VerifierCheck,
    VerifierPlan,
    resolve_build_composition,
)

_ALL_CAPS = frozenset({"workspace.read", "workspace.write", "process.execute"})


def _id(name: str, *, namespace: str = "acme") -> ComponentId:
    return ComponentId(namespace=namespace, name=name, version="1")


def _layer(source: str, allowed: frozenset[str] = _ALL_CAPS) -> CapabilityLayer:
    return CapabilityLayer(source=source, allowed=allowed)


def _policy(source: str, *rules: PolicyRule) -> PolicyLayer:
    return PolicyLayer(source=source, rules=rules)


class _Engine:
    id: ClassVar[ComponentId] = _id("engine")

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=(
                ComponentIntent(
                    operation="workspace.author",
                    required_capabilities=frozenset({"workspace.write"}),
                ),
            ),
            requested_tools=frozenset({"file_edit", "shell"}),
        )


class _Target:
    id: ClassVar[ComponentId] = _id("target")

    def plan(self, request: TargetRequest) -> TargetPlan:
        entry = EntryDescriptor(kind="manifest", reference="result/task.json")
        return TargetPlan(
            target=self.id,
            delivery=DeliveryIntent(shape="data.job_bundle", entry=entry),
            preview=PreviewPlan(modality="none"),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="fixture",
                        intent=ComponentIntent(
                            operation="target.verify",
                            required_capabilities=frozenset({"process.execute"}),
                        ),
                    ),
                )
            ),
        )


class _WrongEngine(_Engine):
    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(engine=_id("other"))


def _spec(
    component_id: ComponentId,
    kind: ComponentKind,
    *,
    features: frozenset[str] = frozenset(),
) -> ComponentSpec:
    return ComponentSpec(
        id=component_id,
        kind=kind,
        features=features,
        capabilities=_layer(component_id.canonical),
        policy=_policy(component_id.canonical),
    )


def _profile(*, exporter: ComponentId | None = None) -> BuildProfile:
    return BuildProfile(
        id=_id("batch"),
        label="Batch job",
        engine=_Engine.id,
        target=_Target.id,
        verifier=_id("verifier"),
        preview=_id("preview"),
        exporter=exporter,
        compatible_reference_categories=frozenset({"reference_pack"}),
        capabilities=_layer("profile"),
        policy=_policy(
            "profile",
            PolicyRule(key="network.egress", decision=PolicyDecision.ALLOW),
        ),
        requirements=CompatibilityRequirements(
            components=(
                ComponentRequirement(
                    kind=ComponentKind.TARGET,
                    accepted=(_Target.id,),
                    required_features=frozenset({"non_web"}),
                    operation="target",
                ),
            ),
            required_capabilities=frozenset({"workspace.read"}),
        ),
    )


def _registry(
    *, exporter: ComponentId | None = None, wrong_engine: bool = False
) -> BuildPlatformRegistry:
    registry = BuildPlatformRegistry()
    engine = _WrongEngine() if wrong_engine else _Engine()
    registry.register_engine(_spec(_Engine.id, ComponentKind.ENGINE), engine)
    registry.register_target(
        _spec(
            _Target.id,
            ComponentKind.TARGET,
            features=frozenset({"non_web"}),
        ),
        _Target(),
    )
    registry.register_component(_spec(_id("verifier"), ComponentKind.VERIFIER))
    registry.register_component(_spec(_id("preview"), ComponentKind.PREVIEW))
    registry.register_profile(_profile(exporter=exporter))
    return registry


def _inputs(
    *,
    profile: ComponentId | None = None,
    host_caps: frozenset[str] = _ALL_CAPS,
    user_policy: PolicyLayer | None = None,
    references: frozenset[str] = frozenset(),
) -> ResolutionInputs:
    return ResolutionInputs(
        profile=profile or _id("batch"),
        goal="transform a fixture",
        platform_capabilities=_layer("platform"),
        user_capabilities=_layer("user"),
        host_capabilities=_layer("host", host_caps),
        platform_policy=_policy("platform"),
        user_policy=user_policy or _policy("user"),
        requested_reference_categories=references,
    )


def test_resolver_is_deterministic_and_inspectable() -> None:
    registry = _registry()
    first = resolve_build_composition(registry, _inputs())
    second = resolve_build_composition(registry, _inputs())
    assert first.model_dump_json() == second.model_dump_json()
    assert first.digest == second.digest
    assert first.engine == _Engine.id
    assert first.target == _Target.id
    assert first.target_plan.delivery.shape == "data.job_bundle"
    assert first.target_plan.preview.modality == "none"
    assert first.blocked_operations == ()


def test_protected_namespace_and_duplicate_ids_fail_closed() -> None:
    registry = BuildPlatformRegistry()
    protected_id = _id("engine", namespace="disco")
    protected = _spec(protected_id, ComponentKind.ENGINE)
    with pytest.raises(RegistryError, match="host-protected"):
        registry.register_engine(protected, _Engine())
    registry.register_component(_spec(_id("verifier"), ComponentKind.VERIFIER))
    with pytest.raises(RegistryError, match="duplicate"):
        registry.register_component(_spec(_id("verifier"), ComponentKind.VERIFIER))


def test_unknown_profile_never_falls_back() -> None:
    with pytest.raises(ResolutionError, match="not registered"):
        resolve_build_composition(_registry(), _inputs(profile=_id("missing")))


def test_component_plan_identity_mismatch_fails_closed() -> None:
    with pytest.raises(ResolutionError, match="mismatched"):
        resolve_build_composition(_registry(wrong_engine=True), _inputs())


def test_capability_is_intersection_and_blocks_only_dependent_operation() -> None:
    composition = resolve_build_composition(
        _registry(),
        _inputs(host_caps=frozenset({"workspace.read", "workspace.write"})),
    )
    assert "process.execute" not in composition.effective_capabilities.allowed
    assert not composition.blocked("construct")
    assert composition.blocked("verify")
    assert not composition.blocked("preview")
    verify_block = next(
        block for block in composition.blocked_operations if block.operation == "verify"
    )
    assert verify_block.missing_capabilities == ("process.execute",)


def test_policy_denial_dominates_component_allow() -> None:
    composition = resolve_build_composition(
        _registry(),
        _inputs(
            user_policy=_policy(
                "user",
                PolicyRule(
                    key="network.egress",
                    decision=PolicyDecision.DENY,
                    reason="owner denied network",
                ),
            )
        ),
    )
    rule = next(rule for rule in composition.effective_policy.rules if rule.key == "network.egress")
    assert rule.decision is PolicyDecision.DENY
    assert "user" in rule.reason


def test_missing_optional_exporter_blocks_package_not_build() -> None:
    exporter = _id("missing_exporter")
    composition = resolve_build_composition(_registry(exporter=exporter), _inputs())
    assert composition.blocked("package")
    assert not composition.blocked("construct")
    assert not composition.blocked("target")


def test_incompatible_reference_category_is_visible_and_scoped() -> None:
    composition = resolve_build_composition(
        _registry(), _inputs(references=frozenset({"library_recipe"}))
    )
    assert composition.blocked("context")
    assert not composition.blocked("construct")


def test_profile_choices_are_small_sorted_profile_records() -> None:
    registry = _registry()
    choices = registry.profile_choices()
    assert [(choice.id, choice.label) for choice in choices] == [(_id("batch"), "Batch job")]
    assert set(type(choices[0]).model_fields) == {"id", "label"}
