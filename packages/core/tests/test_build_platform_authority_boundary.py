from __future__ import annotations

from typing import Any

import pytest
from disco.core.build_platform import (
    BuildPlatformRegistry,
    CapabilityLayer,
    ComponentId,
    ComponentKind,
    ComponentSpec,
    ConstructionPlan,
    PolicyLayer,
    RegistryError,
    TargetPlan,
)


def _id(name: str) -> ComponentId:
    return ComponentId(namespace="hostile", name=name, version="1")


def _spec(component_id: ComponentId, kind: ComponentKind) -> ComponentSpec:
    return ComponentSpec(
        id=component_id,
        kind=kind,
        capabilities=CapabilityLayer(source="hostile"),
        policy=PolicyLayer(source="hostile"),
    )


class _HostilePlanner:
    """Every authority violation would become observable if registry invoked it."""

    def __init__(self) -> None:
        self.id_read = False
        self.plan_called = False
        self.state_mutated = False
        self.secret_read = False
        self.process_spawned = False

    @property
    def id(self) -> ComponentId:
        self.id_read = True
        return _id("engine")

    def plan(self, request: Any) -> ConstructionPlan:
        self.plan_called = True
        self.state_mutated = True
        self.secret_read = True
        self.process_spawned = True
        return ConstructionPlan(engine=_id("engine"))


def test_public_engine_registration_is_exact_data_and_never_invokes_fake_code() -> None:
    registry = BuildPlatformRegistry()
    hostile = _HostilePlanner()
    with pytest.raises(RegistryError, match="data-only"):
        registry.register_engine(  # type: ignore[arg-type]
            _spec(_id("engine"), ComponentKind.ENGINE), hostile
        )
    assert hostile.id_read is False
    assert hostile.plan_called is False
    assert hostile.state_mutated is False
    assert hostile.secret_read is False
    assert hostile.process_spawned is False


def test_public_target_registration_is_exact_data_and_never_invokes_fake_code() -> None:
    registry = BuildPlatformRegistry()
    hostile = _HostilePlanner()
    with pytest.raises(RegistryError, match="data-only"):
        registry.register_target(  # type: ignore[arg-type]
            _spec(_id("target"), ComponentKind.TARGET), hostile
        )
    assert hostile.id_read is False
    assert hostile.plan_called is False


def test_plan_values_have_no_execution_or_success_authority() -> None:
    assert set(ConstructionPlan.model_fields).isdisjoint(
        {"execute", "runtime", "store", "secret", "revision", "verdict", "success"}
    )
    assert set(TargetPlan.model_fields).isdisjoint(
        {"execute", "runtime", "store", "secret", "revision", "verdict", "success"}
    )
