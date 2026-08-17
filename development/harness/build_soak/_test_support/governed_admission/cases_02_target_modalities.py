"""Moved target modalities collection implementations."""

from __future__ import annotations

from ._shared import (
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationExecutionIdentity,
    _shared_execution_authority,
    pytest,
)
from .helpers_01 import (
    _repeat_appkit_preview,
    _replace_receipt,
    _result,
    _segment,
    _use_appkit_owned_preview,
)


def _impl_test_current_appkit_owned_preview_with_exact_receipt_passes() -> None:
    events = _segment()
    _use_appkit_owned_preview(events)

    assert _result(events).passed


def _impl_test_appkit_preview_reobservation_retains_same_operational_handoff() -> None:
    events = _segment()
    _use_appkit_owned_preview(events)
    _repeat_appkit_preview(events)

    assert _result(events).passed


@pytest.mark.parametrize("same_operational_identity", (True, False))
def _impl_test_appkit_preview_receipt_cannot_replace_handoff_provenance(
    same_operational_identity: bool,
) -> None:
    events = _segment()
    _use_appkit_owned_preview(events)
    repeated = _repeat_appkit_preview(
        events,
        same_operational_identity=same_operational_identity,
    )
    started = next(event for event in events if event.get("kind") == "verifier_started")
    started["preview_selection"] = repeated
    execution = dict(started["execution_identity"])
    execution["instance_id"] = repeated["projection_id"]
    started["execution_identity"] = execution
    _replace_receipt(
        events,
        preview_selection=PreviewSelectionIdentity.model_validate(repeated),
        execution_identity=VerificationExecutionIdentity.model_validate(execution),
    )

    assert _result(events).failed


def _impl_test_composed_checks_share_execution_generation_but_retain_distinct_modalities() -> None:
    strict = {
        "modality": "appkit_strict_runtime",
        "instance_id": "pv_same",
        "generation": "sandbox-1:1",
        "locator": "http://preview.test",
    }
    functional = {
        **strict,
        "modality": "managed_preview",
    }

    assert _shared_execution_authority(strict) == _shared_execution_authority(functional)
    for field, foreign in (
        ("instance_id", "pv_foreign"),
        ("generation", "sandbox-1:2"),
        ("locator", "http://foreign.test"),
    ):
        changed = {**functional, field: foreign}
        assert _shared_execution_authority(strict) != _shared_execution_authority(changed)


@pytest.mark.parametrize(
    "corrupt",
    ("failed_verifier", "missing_runtime", "call_mismatch"),
)
def _impl_test_appkit_owned_preview_requires_exact_successful_source_pair(corrupt: str) -> None:
    events = _segment()
    _use_appkit_owned_preview(events)
    action = next(
        event
        for event in events
        if event.get("kind") == "action"
        and event.get("tool_call", {}).get("tool_name") == "verify_appkit_app"
    )
    observation = next(
        event
        for event in events
        if event.get("kind") == "observation" and event.get("action_id") == action.get("id")
    )
    if corrupt == "failed_verifier":
        observation["tool_result"]["structured"]["passed"] = False
    elif corrupt == "missing_runtime":
        observation["tool_result"]["structured"].pop("preview_runtime")
    else:
        observation["tool_result"]["call_id"] = "foreign-call"

    assert _result(events).failed


def _impl_test_target_specific_nonweb_policy_needs_no_preview() -> None:
    assert _result(_segment(native=True), native=True).passed


def _impl_test_output_producing_nonweb_target_requires_post_start_artifact_seal() -> None:
    assert _result(
        _segment(native=True, sealed_output=True),
        native=True,
    ).passed


def _impl_test_output_producing_target_without_artifact_identity_fails() -> None:
    events = _segment(native=True, sealed_output=True)
    _replace_receipt(events, artifact_identity=None)

    assert _result(events, native=True).failed


def _impl_test_output_producing_target_with_unadmitted_identity_scheme_fails() -> None:
    events = _segment(native=True, sealed_output=True)
    _replace_receipt(
        events,
        artifact_identity=VerificationArtifactIdentity(
            scheme="opaque-unverified",
            digest="sha256:" + "e" * 64,
            entry_reference="build/Fixture.app",
            producer_id="synthetic.native_verifier@1",
        ),
    )

    assert _result(events, native=True).failed
