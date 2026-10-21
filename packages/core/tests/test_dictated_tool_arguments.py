"""Tool configuration must not become required visible page copy on replay."""

from __future__ import annotations

import pytest
from _dictated_content_support import _plan, _status, _user
from disco.core.loop.finish.verify_gate_parts.host_claims import host_verification_claims
from disco.core.loop.plan_conditions import (
    dictated_content_conditions_from_events,
    extract_dictated_content_literals,
)
from disco.core.verification import (
    HostVerificationClaim,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationRequestedClaim,
    VerificationRequirementsDirective,
)

# Original USER request from the real UI AppKit acceptance run. No requested
# visible recipe label and no explicit verification directive accompanied it.
_ORCHARD_REQUEST = (
    "Create a compact lead-generation AppKit site called Orchard Studio using "
    "app_create with recipe_id='editorial-ledger', primitive_id='lead_gen', "
    "brief='Orchard Studio', owner_controls=true. Replace placeholder copy through "
    "semantic AppKit tools, keep the six owner controls, run verify_appkit_app, "
    "and finish with the local preview and source. No external deployment. "
    "Do not hand-write .disco files."
)


def test_original_appkit_request_does_not_require_rendering_tool_configuration():
    events = [_user(_ORCHARD_REQUEST, 1), _plan(1, 2), _status("plan_approved", 3)]
    before = [event.model_dump(mode="json") for event in events]

    assert extract_dictated_content_literals(_ORCHARD_REQUEST) == []
    assert dictated_content_conditions_from_events(events) == []
    claims = host_verification_claims({}, events, artifact_path="index.html")
    assert claims  # The ordinary host verification floor is still present.
    assert not any(claim.kind == VerificationClaimKind.VISIBLE_TEXT for claim in claims)
    assert [event.model_dump(mode="json") for event in events] == before


@pytest.mark.parametrize(
    "invocation",
    [
        "using arbitrary_builder",
        "Use arbitrary.builder",
        "call renderer",
        "invoke the renderer tool",
        "use the tool renderer",
        "using renderer tool",
    ],
)
def test_generic_tool_arguments_do_not_hide_later_explicit_copy(invocation):
    text = (
        f"Build a site called 'Visible title' {invocation} with enabled=true, "
        "count=2, ratio=-1.25, mode='Internal mode', brief=\"Internal brief\". "
        "Then show a heading 'Internal mode' and a button 'Buy now'."
    )
    assert extract_dictated_content_literals(text) == ["Visible title", "Internal mode", "Buy now"]


@pytest.mark.parametrize(
    "instruction",
    [
        "Use a button with label='Buy now'.",
        "Use button with label='Buy now'.",
        "Set heading='Buy now'.",
        "Call the button 'Buy now'.",
        "using app_create with recipe_id='metadata', then label='Buy now'.",
        "using app_create with recipe_id='metadata'; label='Buy now'.",
        "using app_create with recipe_id='metadata'. label='Buy now'.",
        "using app_create with recipe_id='metadata' and label='Buy now'.",
        "Show 'using app_create with' and label='Buy now'.",
    ],
)
def test_ordinary_copy_and_clauses_after_tool_arguments_remain_required(instruction):
    assert "Buy now" in extract_dictated_content_literals(instruction)


def test_explicit_user_requirement_survives_argument_exclusion_and_replanning():
    initial = _user(_ORCHARD_REQUEST, 1)
    directive = VerificationRequirementsDirective(
        claims=(
            VerificationRequestedClaim(
                claim_id="required.recipe-label",
                kind=VerificationClaimKind.VISIBLE_TEXT,
                expected="editorial-ledger",
            ),
        ),
    )
    initial = initial.model_copy(update={"verification_requirements": directive})
    events = [
        initial,
        _plan(1, 2),
        _status("plan_approved", 3),
        _user("Verify the original site again.", 4),
        _plan(2, 5),
    ]
    claims = host_verification_claims({}, events)
    label = next(claim for claim in claims if claim.claim_id == "required.recipe-label")
    assert label.expected == "editorial-ledger"
    assert label.required is True
    assert label.source_authority == f"user_event:{initial.id}"


def test_target_owned_visible_claim_is_not_removed_by_argument_exclusion():
    claim = HostVerificationClaim(
        claim_id="target.recipe-label",
        kind=VerificationClaimKind.VISIBLE_TEXT,
        expected="editorial-ledger",
        source_authority="target:fixture@1",
    )
    check = VerificationCheckContract(
        check_id="web",
        receipt_kind="host.web@1",
        issuer_id="host",
        operation="verify_web_app",
        claims=(claim,),
    )
    claims = host_verification_claims({}, [_user(_ORCHARD_REQUEST, 1)], check=check)
    assert claim in claims
    assert claim.required is True
