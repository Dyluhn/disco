from __future__ import annotations

import hashlib

import pytest
from disco.core import (
    ActionProfile,
    EffectCapability,
    MutationReceipt,
    ObservationEvent,
    ObservationReceipt,
    OpaqueEffectReceipt,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
    ToolBehavior,
    ToolResult,
    VerificationReceipt,
    event_from_json_dict,
    event_to_json_dict,
    validate_action_profile,
    validate_effect_receipts,
)
from disco.core.effects import CoverageSpan, CoverageUnit
from pydantic import ValidationError

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def _resource(path: str = "src/app.py") -> ResourceKey:
    return ResourceKey(namespace="workspace", identifier=path)


def _revision(path: str = "src/app.py", digest: str = _SHA_A) -> ResourceRevision:
    return ResourceRevision(resource=_resource(path), digest=digest)


def _observation() -> ObservationReceipt:
    return ObservationReceipt(
        capability=EffectCapability.WORKSPACE_CONTENT_READ,
        revision=_revision(),
        coverage=ResourceCoverage(
            unit=CoverageUnit.BYTES,
            spans=(CoverageSpan(start=0, end=3),),
            total=3,
        ),
        complete=True,
        raw_size_bytes=3,
        rendered_size_bytes=3,
        rendered_sha256=hashlib.sha256(b"abc").hexdigest(),
    )


def test_old_tool_result_without_receipts_is_backward_compatible() -> None:
    result = ToolResult.model_validate(
        {
            "call_id": "call_old",
            "tool_name": "file_read",
            "success": True,
            "content": "old log body",
        }
    )

    assert result.effect_receipts == ()


def test_effect_receipts_round_trip_through_observation_event() -> None:
    event = ObservationEvent(
        action_id="evt_action",
        tool_result=ToolResult(
            call_id="call_1",
            tool_name="file_read",
            success=True,
            content="abc",
            effect_receipts=(_observation(),),
        ),
    )

    restored = event_from_json_dict(event_to_json_dict(event))

    assert isinstance(restored, ObservationEvent)
    assert restored.tool_result.effect_receipts == (_observation(),)


def test_rendered_observation_proof_requires_size_and_digest_together() -> None:
    payload = _observation().model_dump()
    payload.pop("rendered_sha256")
    with pytest.raises(ValidationError, match="must be declared together"):
        ObservationReceipt.model_validate(payload)


def test_model_controlled_structured_payload_cannot_forge_effect_receipts() -> None:
    forged = _observation().model_dump(mode="json")
    result = ToolResult(
        call_id="call_1",
        tool_name="unknown_domain_tool",
        success=True,
        content="ok",
        structured={"effect_receipts": [forged]},
    )

    assert result.effect_receipts == ()
    assert result.structured == {"effect_receipts": [forged]}


def test_complete_coverage_must_be_exact_and_canonical() -> None:
    with pytest.raises(ValidationError, match="sorted and non-overlapping"):
        ResourceCoverage(
            unit=CoverageUnit.LINES,
            spans=(CoverageSpan(start=2, end=4), CoverageSpan(start=1, end=3)),
            total=4,
        )

    with pytest.raises(ValidationError, match="cover the declared total"):
        ObservationReceipt(
            capability=EffectCapability.WORKSPACE_CONTENT_READ,
            revision=_revision(),
            coverage=ResourceCoverage(
                unit=CoverageUnit.BYTES,
                spans=(CoverageSpan(start=1, end=3),),
                total=3,
            ),
            complete=True,
        )


def test_mutation_receipt_binds_revisions_and_size_to_one_resource() -> None:
    receipt = MutationReceipt(
        resource=_resource(),
        before=_revision(digest=_SHA_A),
        after=_revision(digest=_SHA_B),
        after_size_bytes=12,
    )
    assert receipt.before != receipt.after

    with pytest.raises(ValidationError, match="does not match mutation resource"):
        MutationReceipt(
            resource=_resource(),
            before=_revision("other.py", _SHA_C),
            after=_revision(digest=_SHA_B),
            after_size_bytes=12,
        )

    with pytest.raises(ValidationError, match="must declare an after size"):
        MutationReceipt(resource=_resource(), after=_revision())

    with pytest.raises(ValidationError, match="not mutation progress"):
        MutationReceipt(
            resource=_resource(),
            before=_revision(),
            after=_revision(),
            after_size_bytes=12,
        )


def test_failed_verification_requires_a_failure_fingerprint() -> None:
    with pytest.raises(ValidationError, match="requires a failure fingerprint"):
        VerificationReceipt(
            verifier_id="web-ready",
            subject=_revision(),
            requirement_fingerprint=_SHA_B,
            passed=False,
        )


def test_action_profile_and_receipts_must_narrow_declared_behavior() -> None:
    behavior = ToolBehavior(
        planner_safe=True,
        possible_capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ}),
    )
    profile = ActionProfile(capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ}))
    assert validate_action_profile(behavior, profile) == profile
    assert validate_effect_receipts(profile, (_observation(),)) == (_observation(),)

    bad_profile = ActionProfile(capabilities=frozenset({EffectCapability.OPAQUE_EXECUTE}))
    with pytest.raises(ValueError, match="undeclared capabilities"):
        validate_action_profile(behavior, bad_profile)

    opaque = OpaqueEffectReceipt(
        capability=EffectCapability.OPAQUE_EXECUTE,
        reason="exact resource effects unknown",
    )
    with pytest.raises(ValueError, match="undeclared capabilities"):
        validate_effect_receipts(profile, (opaque,))
