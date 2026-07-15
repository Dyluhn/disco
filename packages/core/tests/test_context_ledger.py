"""CXT-1 tests: ledger/refs models — roundtrip, validation, immutability,
and SourcePriority/CompactionPolicy semantics."""

from __future__ import annotations

import pytest
from disco.core.context import (
    ArtifactMemoryKind,
    ArtifactMemoryRef,
    CompactionPolicy,
    ContextLedger,
    DirectEditKind,
    DirectEditRef,
    HandoffRef,
    ResolvedContextRange,
    ResourceRef,
    Severity,
    SourceKind,
    SourcePriority,
    VerifierFailureRef,
)
from pydantic import BaseModel, ValidationError


def _roundtrip(m: BaseModel) -> None:
    restored = type(m).model_validate(m.model_dump(mode="json"))
    assert restored == m


def test_all_models_roundtrip() -> None:
    amr = ArtifactMemoryRef(
        kind=ArtifactMemoryKind.TODO, rel_path=".disco/context/todo.md", sha256="abc"
    )
    models: list[BaseModel] = [
        amr,
        SourcePriority.default(),
        CompactionPolicy.default(),
        ResolvedContextRange(reason="explored, dead end", event_ids=("evt_1", "evt_2")),
        DirectEditRef(
            target_id="hero-cta",
            rel_path="index.html",
            kind=DirectEditKind.TEXT,
            summary="reworded",
        ),
        VerifierFailureRef(
            kind="console_error",
            message="ReferenceError: x",
            rel_path="app.js",
            severity=Severity.BLOCKER,
        ),
        ResourceRef(
            rel_path="assets/logo.svg", source="github://o/r/logo.svg", sha256="d", license="MIT"
        ),
        HandoffRef(kind="owner", rel_path="owner_handoff/"),
        ContextLedger.empty("conv_1", "/ws/conv_1"),
    ]
    for m in models:
        _roundtrip(m)


def test_generated_ids_have_stable_prefixes() -> None:
    assert ResolvedContextRange(reason="x").range_id.startswith("cxr_")
    assert VerifierFailureRef(kind="k", message="m").failure_id.startswith("vf_")


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ContextLedger.model_validate({"conversation_id": "c", "bogus": 1})


def test_bad_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        VerifierFailureRef.model_validate({"kind": "k", "message": "m", "severity": "nonsense"})


def test_empty_ledger_minimal_construction() -> None:
    led = ContextLedger.empty("conv_42")
    assert led.conversation_id == "conv_42"
    assert led.workspace_root is None
    assert led.current_version == 0
    assert led.latest_verifier_failures == ()
    assert led.resource_manifest == ()


def test_immutability_via_model_copy() -> None:
    led = ContextLedger.empty("conv_1")
    updated = led.model_copy(update={"active_goal": "build a site", "current_version": 2})
    assert updated.active_goal == "build a site"
    assert updated.current_version == 2
    # original untouched
    assert led.active_goal is None
    assert led.current_version == 0
    # frozen: direct mutation forbidden
    with pytest.raises(ValidationError):
        led.active_goal = "mutated"  # type: ignore[misc]


def test_source_priority_default_order_semantics() -> None:
    sp = SourcePriority.default()
    # every SourceKind appears exactly once
    assert set(sp.order) == set(SourceKind)
    assert len(sp.order) == len(set(sp.order)) == len(list(SourceKind))
    # documented precedence: anchors first, an unresolved failure outranks the
    # live todo, raw history last.
    assert sp.order[0] is SourceKind.GOAL
    assert sp.order[1] is SourceKind.CONTRACT
    assert sp.rank(SourceKind.DESIGN_DIRECTION) < sp.rank(SourceKind.TODO)
    assert sp.rank(SourceKind.VERIFIER_FAILURE) < sp.rank(SourceKind.TODO)
    assert sp.order[-1] is SourceKind.HISTORY


def test_compaction_policy_limit_for_by_kind() -> None:
    pol = CompactionPolicy.default()
    # never-compact kinds are unbounded
    for kind in (
        SourceKind.GOAL,
        SourceKind.CONTRACT,
        SourceKind.DESIGN_DIRECTION,
        SourceKind.VERIFIER_FAILURE,
        SourceKind.DIRECT_EDIT,
    ):
        assert pol.limit_for(kind) is None
    # omittable kinds carry their configured caps
    assert pol.limit_for(SourceKind.RESOURCE) == pol.max_resource_refs
    assert pol.limit_for(SourceKind.RECOVERABLE_REF) == pol.max_recoverable_refs
    assert pol.limit_for(SourceKind.COMMENT) == pol.max_comments
    # kinds with no count cap return None
    assert pol.limit_for(SourceKind.HANDOFF) is None
