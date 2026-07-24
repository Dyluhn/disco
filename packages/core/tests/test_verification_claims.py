from __future__ import annotations

import base64

import pytest
from disco.core.events import EventSource, LLMMessage, MessageEvent
from disco.core.loop import HostVerificationDeliverable
from disco.core.loop.finish.verify_gates import _user_verification_material
from disco.core.verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    HostVerificationResult,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerificationDeliveryContract,
    VerificationEvidenceModality,
    VerificationRequestedClaim,
    VerificationRequirementsDirective,
    apply_semantic_verifier_result,
    default_structured_web_claims,
    preview_binding_failure_result,
    requires_structured_browser_runtime,
    structured_web_verification_result,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_structured_browser_runtime_is_required_by_claims_not_artifact_names() -> None:
    assert requires_structured_browser_runtime(default_structured_web_claims()) is True
    assert (
        requires_structured_browser_runtime(
            [
                HostVerificationClaim(
                    claim_id="target.report",
                    kind=VerificationClaimKind.TARGET_SPECIFIC,
                    expected="valid report archive",
                    source_authority="target:report@1",
                ),
                HostVerificationClaim(
                    claim_id="target.optional_text",
                    kind=VerificationClaimKind.VISIBLE_TEXT,
                    required=False,
                    expected="optional",
                    source_authority="target:report@1",
                ),
            ]
        )
        is False
    )
    assert (
        requires_structured_browser_runtime(
            [
                HostVerificationClaim(
                    claim_id="application.title",
                    kind=VerificationClaimKind.APPLICATION_IDENTITY,
                    expected="Native Notes",
                    source_authority="target:application_model@1",
                )
            ]
        )
        is False
    )


def _claim(
    claim_id: str,
    kind: VerificationClaimKind,
    expected: str,
) -> HostVerificationClaim:
    return HostVerificationClaim(
        claim_id=claim_id,
        kind=kind,
        expected=expected,
        source_authority="scenario:test@1",
    )


def _deliverable(
    *extra: HostVerificationClaim,
    conversation_id: str = "conv-a",
    run_intent_id: str = "intent-a",
    workspace_revision: int = 7,
    workspace_generation: str = "a" * 32,
    workspace_epoch: int = 4,
) -> HostVerificationDeliverable:
    return HostVerificationDeliverable(
        conversation_id=conversation_id,
        run_intent_id=run_intent_id,
        run_identity="run:sha256:" + "b" * 64,
        agent_view_id="view-a",
        deliverable_event_id="evt-deliverable-a",
        artifact_path="index.html",
        artifact_kind="app",
        deployment_url="http://127.0.0.1:8123/",
        workspace_revision=workspace_revision,
        workspace_generation=workspace_generation,
        workspace_epoch=workspace_epoch,
        observed_after_seq=12,
        required_claims=(*default_structured_web_claims(), *extra),
    )


def _governed_deliverable(
    *extra: HostVerificationClaim,
) -> HostVerificationDeliverable:
    base = _deliverable(*extra)
    check = VerificationCheckContract(
        check_id="web_functional",
        receipt_kind="disco.web_functional@1",
        issuer_id="disco.host_web_verifier@1",
        operation="host.verify_deliverable",
        required_execution_modality="managed_preview",
        accepted_claim_kinds=frozenset(claim.kind for claim in base.required_claims),
        claims=base.required_claims,
    )
    contract = AdmittedVerificationContract(
        target_id="disco.legacy_web@1",
        verifier_id=check.issuer_id,
        delivery=VerificationDeliveryContract(
            shape="web.legacy_deliverable",
            mode="interactive",
            entry_kind="manifest",
            entry_reference="active-deliverable",
        ),
        preview_modality="legacy_host",
        checks=(check,),
    )
    return base.model_copy(update={"verification_contract": contract, "verification_check": check})


def _verdict(**updates: object) -> dict[str, object]:
    verdict: dict[str, object] = {
        "passed": True,
        "verdict": "pass",
        "url": "http://127.0.0.1:8123/",
        "http_status": 200,
        "meaningful_content": True,
        "visible_text_chars": 42,
        "elements_count": 1,
        "rendered_text": "Static Seed 400301\nLaunch",
        "console_errors": [],
        "network_failures": [],
        "screenshot_path": ".pmx/screenshots/current.png",
        "freshness": {
            "executor_generation": "a" * 32,
            "synchronized_epoch": 4,
        },
        "summary": "structured checks passed",
        "artifact_identity": {
            "conversation_id": "conv-a",
            "artifact_path": "index.html",
            "artifact_kind": "app",
            "requested_url": "http://127.0.0.1:8123/",
            "observed_url": "http://127.0.0.1:8123/",
            "preview_selection": None,
            "preview_live_match": True,
        },
    }
    verdict.update(updates)
    return verdict


def test_text_only_web_receipt_proves_exact_visible_text_without_vision() -> None:
    visible = _claim(
        "web.visible_text:static_seed",
        VerificationClaimKind.VISIBLE_TEXT,
        "Static Seed 400301",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(visible),
        verdict=_verdict(),
    )

    assert receipt.status is VerificationClaimStatus.PASS
    required_text = next(
        result for result in receipt.claim_results if result.claim_id == visible.claim_id
    )
    assert required_text.status is VerificationClaimStatus.PASS
    assert required_text.evidence_modalities == (VerificationEvidenceModality.DOM_ACCESSIBILITY,)
    assert all(
        VerificationEvidenceModality.SCREENSHOT_PIXELS not in result.evidence_modalities
        for result in receipt.claim_results
    )


def test_exact_visible_text_uses_rendered_visible_dom_nodes_before_css_case_transform() -> None:
    visible = _claim(
        "web.visible_text:imported_complete",
        VerificationClaimKind.VISIBLE_TEXT,
        "Imported Complete 405115",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(visible),
        verdict=_verdict(
            rendered_text="Imported Seed 405115\nIMPORTED COMPLETE 405115",
            visible_dom_text="Imported Seed 405115\nImported Complete 405115",
        ),
    )

    assert receipt.status is VerificationClaimStatus.PASS
    result = next(item for item in receipt.claim_results if item.claim_id == visible.claim_id)
    assert result.status is VerificationClaimStatus.PASS
    assert receipt.observed_facts[0].value == ("Imported Seed 405115\nImported Complete 405115")


def test_exact_visible_text_does_not_casefold_or_accept_absent_dom_text() -> None:
    visible = _claim(
        "web.visible_text:imported_complete",
        VerificationClaimKind.VISIBLE_TEXT,
        "Imported Complete 405115",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(visible),
        verdict=_verdict(
            rendered_text="IMPORTED COMPLETE 405115",
            visible_dom_text="Different authored text",
        ),
    )

    assert receipt.status is VerificationClaimStatus.FAIL
    result = next(item for item in receipt.claim_results if item.claim_id == visible.claim_id)
    assert result.status is VerificationClaimStatus.FAIL


def test_structured_receipt_retains_integrity_bound_visible_text_observation() -> None:
    receipt = structured_web_verification_result(
        deliverable=_governed_deliverable(),
        verdict=_verdict(),
    )

    assert len(receipt.observed_facts) == 1
    fact = receipt.observed_facts[0]
    assert fact.kind is VerificationClaimKind.VISIBLE_TEXT
    assert fact.value == "Static Seed 400301\nLaunch"
    assert fact.evidence_modalities == (VerificationEvidenceModality.DOM_ACCESSIBILITY,)

    forged = receipt.model_dump(mode="json")
    forged["observed_facts"][0]["value"] = "model-authored replacement"
    with pytest.raises(ValueError, match="effect receipt subject"):
        HostVerificationResult.model_validate(forged)


def test_http_dom_console_network_and_interaction_are_nonvision_claims() -> None:
    interaction = _claim(
        "web.interaction:primary",
        VerificationClaimKind.INTERACTION,
        "primary interaction completes",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(interaction),
        verdict=_verdict(
            interaction_claims={
                interaction.claim_id: {
                    "expected": interaction.expected,
                    "passed": True,
                    "steps": [{"action": "click", "success": True}],
                }
            }
        ),
    )

    assert receipt.passed
    by_kind = {result.kind: result for result in receipt.claim_results}
    for kind in (
        VerificationClaimKind.HTTP_READY,
        VerificationClaimKind.RENDERED_CONTENT,
        VerificationClaimKind.CONSOLE_CLEAN,
        VerificationClaimKind.NETWORK_CLEAN,
        VerificationClaimKind.INTERACTION,
    ):
        assert by_kind[kind].status is VerificationClaimStatus.PASS


def test_dom_and_screenshot_path_cannot_satisfy_visual_semantics() -> None:
    visual = _claim(
        "web.visual:reference",
        VerificationClaimKind.VISUAL_SEMANTIC,
        "matches the supplied reference image",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(visual),
        verdict=_verdict(summary="I verified it visually and it looks perfect"),
    )

    visual_result = next(result for result in receipt.claim_results if result.kind is visual.kind)
    assert receipt.status is VerificationClaimStatus.UNAVAILABLE
    assert visual_result.status is VerificationClaimStatus.UNAVAILABLE
    assert "vision-capable verifier" in visual_result.reason
    # Even an injected passing judge cannot certify pixels that were never captured.
    still_unavailable = apply_semantic_verifier_result(
        receipt,
        verified=True,
        verdict="pass",
        detail="model said it looked right",
    )
    assert still_unavailable.status is VerificationClaimStatus.UNAVAILABLE


def test_independent_image_backed_verifier_can_satisfy_visual_claim() -> None:
    visual = _claim(
        "web.visual:reference",
        VerificationClaimKind.VISUAL_SEMANTIC,
        "matches the supplied reference image",
    )
    pixels = base64.b64encode(_PNG).decode("ascii")
    base = _deliverable(visual)
    check = VerificationCheckContract(
        check_id="web_functional",
        receipt_kind="disco.web_functional@1",
        issuer_id="disco.host_web_verifier@1",
        operation="host.verify_deliverable",
        required_execution_modality="managed_preview",
        delegated_issuer_ids=frozenset({"model_role.verifier@1"}),
        accepted_claim_kinds=frozenset(claim.kind for claim in base.required_claims),
        claims=base.required_claims,
    )
    contract = AdmittedVerificationContract(
        target_id="disco.legacy_web@1",
        verifier_id=check.issuer_id,
        delivery=VerificationDeliveryContract(
            shape="web.legacy_deliverable",
            mode="interactive",
            entry_kind="manifest",
            entry_reference="active-deliverable",
        ),
        preview_modality="legacy_host",
        checks=(check,),
    )
    deliverable = base.model_copy(
        update={"verification_contract": contract, "verification_check": check}
    )
    receipt = structured_web_verification_result(
        deliverable=deliverable,
        verdict=_verdict(screenshot_b64=pixels),
    )
    judged = apply_semantic_verifier_result(
        receipt,
        verified=True,
        verdict="pass",
        detail="configured independent verifier matched the reference",
    )

    assert judged.passed
    round_tripped = HostVerificationResult.model_validate(judged.model_dump(mode="json"))
    assert round_tripped.effect_receipt is not None
    assert round_tripped.effect_receipt.passed is True
    visual_result = next(result for result in judged.claim_results if result.kind is visual.kind)
    assert visual_result.verifier_id == "model_role.verifier@1"
    assert visual_result.evidence_modalities == (VerificationEvidenceModality.SCREENSHOT_PIXELS,)


@pytest.mark.parametrize(
    "raw",
    [b"not-an-image", b"\x89PNG\r\n\x1a\ncorrupt-after-valid-signature"],
)
def test_invalid_png_bytes_cannot_be_promoted_to_visual_pixel_evidence(raw: bytes) -> None:
    visual = _claim(
        "web.visual:reference",
        VerificationClaimKind.VISUAL_SEMANTIC,
        "matches the supplied reference image",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(visual),
        verdict=_verdict(screenshot_b64=base64.b64encode(raw).decode("ascii")),
    )

    judged = apply_semantic_verifier_result(
        receipt,
        verified=True,
        verdict="pass",
        detail="model claimed it saw an image",
    )
    assert judged.status is VerificationClaimStatus.UNAVAILABLE
    assert judged.screenshot_sha256 is None


def test_structured_runtime_pass_cannot_self_certify_contract_semantics() -> None:
    semantic = _claim(
        "web.contract:registered",
        VerificationClaimKind.CONTRACT_SEMANTIC,
        "sha256:registered-contract",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(semantic),
        verdict=_verdict(),
    )

    assert receipt.status is VerificationClaimStatus.UNAVAILABLE
    semantic_result = next(
        result for result in receipt.claim_results if result.claim_id == semantic.claim_id
    )
    assert semantic_result.status is VerificationClaimStatus.UNAVAILABLE
    assert semantic_result.evidence_modalities == ()


def test_interaction_receipt_must_name_the_exact_claim_and_expected_outcome() -> None:
    interaction = _claim(
        "web.interaction:primary",
        VerificationClaimKind.INTERACTION,
        "primary interaction completes",
    )
    receipt = structured_web_verification_result(
        deliverable=_deliverable(interaction),
        verdict=_verdict(
            interaction_claims={
                interaction.claim_id: {
                    "expected": "some other outcome",
                    "passed": True,
                    "steps": [{"action": "click", "success": True}],
                }
            }
        ),
    )

    assert receipt.status is VerificationClaimStatus.FAIL


def test_user_image_alone_does_not_imply_visual_inspection() -> None:
    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    event = MessageEvent(
        id="evt-user-image",
        source=EventSource.USER,
        message=LLMMessage(
            role="user",
            content="Match this reference without changing its visual hierarchy.",
            images=[data_url],
        ),
    )

    claims, references = _user_verification_material([event])

    assert claims == references == ()


def test_explicit_visual_requirement_compiles_exact_reference_pixels() -> None:
    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    event = MessageEvent(
        id="evt-user-image",
        source=EventSource.USER,
        message=LLMMessage(
            role="user",
            content="Match this reference without changing its visual hierarchy.",
            images=[data_url],
        ),
        verification_requirements=VerificationRequirementsDirective(
            claims=(
                VerificationRequestedClaim(
                    claim_id="web.visual:reference",
                    kind=VerificationClaimKind.VISUAL_SEMANTIC,
                    expected="match the supplied reference visual",
                    reference_image_index=0,
                ),
            ),
            reference_images=(data_url,),
        ),
    )

    claims, references = _user_verification_material([event])

    assert len(claims) == len(references) == 1
    assert claims[0].kind is VerificationClaimKind.VISUAL_SEMANTIC
    assert claims[0].source_authority == "user_event:evt-user-image"
    assert claims[0].reference_image_sha256 == references[0].sha256
    assert references[0].image_data_url == data_url
    assert references[0].instruction.startswith("Match this reference")


def test_invalid_reference_pixels_are_rejected_before_they_can_become_evidence() -> None:
    with pytest.raises(ValueError, match="valid bounded image"):
        VerificationRequirementsDirective(
            claims=(
                VerificationRequestedClaim(
                    claim_id="web.visual:reference",
                    kind=VerificationClaimKind.VISUAL_SEMANTIC,
                    expected="match the supplied reference visual",
                    reference_image_index=0,
                ),
            ),
            reference_images=("https://example.invalid/reference.png",),
        )


def test_requirement_snapshot_is_retained_and_exactly_superseded() -> None:
    first = MessageEvent(
        id="evt-requirements-1",
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Use a strong visual hierarchy."),
        verification_requirements=VerificationRequirementsDirective(
            claims=(
                VerificationRequestedClaim(
                    claim_id="web.visual:hierarchy",
                    kind=VerificationClaimKind.VISUAL_SEMANTIC,
                    expected="strong visual hierarchy",
                ),
            ),
        ),
    )
    ordinary_followup = MessageEvent(
        id="evt-followup",
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Also add a footer."),
    )
    replacement = MessageEvent(
        id="evt-requirements-2",
        source=EventSource.USER,
        message=LLMMessage(role="user", content="No additional scenario proof is required."),
        verification_requirements=VerificationRequirementsDirective(
            claims=(),
            supersedes_event_id=first.id,
        ),
    )

    retained, _ = _user_verification_material([first, ordinary_followup])
    cleared, _ = _user_verification_material([first, ordinary_followup, replacement])

    assert [claim.claim_id for claim in retained] == ["web.visual:hierarchy"]
    assert cleared == ()


def test_stale_requirement_replacement_cannot_weaken_active_snapshot() -> None:
    first = MessageEvent(
        id="evt-requirements-current",
        source=EventSource.USER,
        message=LLMMessage(role="user", content="Require a visual review."),
        verification_requirements=VerificationRequirementsDirective(
            claims=(
                VerificationRequestedClaim(
                    claim_id="web.visual:quality",
                    kind=VerificationClaimKind.VISUAL_SEMANTIC,
                    expected="no visible layout defects",
                ),
            ),
        ),
    )
    stale = MessageEvent(
        id="evt-requirements-stale",
        source=EventSource.USER,
        message=LLMMessage(role="user", content="stale replacement"),
        verification_requirements=VerificationRequirementsDirective(
            claims=(),
            supersedes_event_id="evt-foreign",
        ),
    )

    claims, _ = _user_verification_material([first, stale])

    assert [claim.claim_id for claim in claims] == ["web.visual:quality"]


def test_one_aggregate_judgement_cannot_fan_out_across_semantic_claims() -> None:
    first = _claim("web.contract:first", VerificationClaimKind.CONTRACT_SEMANTIC, "first")
    second = _claim("web.contract:second", VerificationClaimKind.CONTRACT_SEMANTIC, "second")
    receipt = structured_web_verification_result(
        deliverable=_deliverable(first, second),
        verdict=_verdict(),
    )

    judged = apply_semantic_verifier_result(
        receipt,
        verified=True,
        verdict="pass",
        detail="aggregate pass",
    )

    assert judged.status is VerificationClaimStatus.UNAVAILABLE
    assert all(
        result.status is VerificationClaimStatus.UNAVAILABLE
        for result in judged.claim_results
        if result.kind is VerificationClaimKind.CONTRACT_SEMANTIC
    )


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"passed": False, "verdict": "fail"}, VerificationClaimStatus.FAIL),
        (
            {"passed": False, "verdict": "unverifiable", "browser_unavailable": True},
            VerificationClaimStatus.UNAVAILABLE,
        ),
        ({"passed": "true", "verdict": "pass"}, VerificationClaimStatus.FAIL),
    ],
)
def test_failed_unverifiable_or_malformed_tool_results_never_count(
    updates: dict[str, object],
    expected: VerificationClaimStatus,
) -> None:
    receipt = structured_web_verification_result(
        deliverable=_deliverable(),
        verdict=_verdict(**updates),
    )
    assert receipt.status is expected


def test_forged_served_artifact_binding_never_counts() -> None:
    receipt = structured_web_verification_result(
        deliverable=_deliverable(),
        verdict=_verdict(
            artifact_identity={
                "conversation_id": "conv-a",
                "artifact_path": "other.html",
                "artifact_kind": "app",
                "requested_url": "http://127.0.0.1:8123/",
                "observed_url": "http://127.0.0.1:8123/",
            }
        ),
    )

    assert receipt.status is VerificationClaimStatus.FAIL


def test_preview_binding_preflight_is_current_but_can_never_pass() -> None:
    deliverable = _deliverable()
    receipt = preview_binding_failure_result(
        deliverable=deliverable,
        observed_url=deliverable.deployment_url,
    )

    assert receipt.is_current_for(deliverable, observed_url=deliverable.deployment_url)
    assert receipt.status is VerificationClaimStatus.FAIL
    artifact = next(
        result
        for result in receipt.claim_results
        if result.kind is VerificationClaimKind.ARTIFACT_IDENTITY
    )
    assert artifact.status is VerificationClaimStatus.FAIL
    assert artifact.evidence_modalities == (VerificationEvidenceModality.ARTIFACT_BINDING,)
    assert all(
        result.status is VerificationClaimStatus.UNAVAILABLE
        for result in receipt.claim_results
        if result.kind is not VerificationClaimKind.ARTIFACT_IDENTITY
    )


def test_browser_pass_without_authenticated_freshness_is_not_current() -> None:
    deliverable = _deliverable()
    receipt = structured_web_verification_result(
        deliverable=deliverable,
        verdict=_verdict(freshness={}),
    )

    assert receipt.status is VerificationClaimStatus.PASS
    assert not receipt.is_current_for(deliverable, observed_url=deliverable.deployment_url)


@pytest.mark.parametrize(
    "changed",
    [
        {"conversation_id": "conv-b"},
        {"run_intent_id": "intent-b"},
        {"run_identity": "run:sha256:" + "d" * 64},
        {"agent_view_id": "view-b"},
        {"deliverable_event_id": "evt-deliverable-b"},
        {"artifact_path": "other.html"},
        {"deployment_url": "http://127.0.0.1:9999/"},
        {"workspace_revision": 8},
        {"workspace_generation": "c" * 32},
        {"workspace_epoch": 5},
    ],
)
def test_foreign_or_stale_receipt_is_not_current(changed: dict[str, object]) -> None:
    original = _deliverable()
    receipt = structured_web_verification_result(deliverable=original, verdict=_verdict())
    foreign = original.model_copy(update=changed)

    assert not receipt.is_current_for(foreign, observed_url=foreign.deployment_url)


def test_mutation_after_verification_requires_a_fresh_receipt() -> None:
    before = _deliverable(workspace_revision=7)
    receipt = structured_web_verification_result(deliverable=before, verdict=_verdict())
    after = before.model_copy(update={"workspace_revision": 13, "observed_after_seq": 14})

    assert receipt.is_current_for(before, observed_url=before.deployment_url)
    assert not receipt.is_current_for(after, observed_url=after.deployment_url)


def test_same_claim_id_cannot_substitute_a_different_expected_contract() -> None:
    original_claim = _claim(
        "web.visible_text:stable",
        VerificationClaimKind.VISIBLE_TEXT,
        "Static Seed 400301",
    )
    changed_claim = original_claim.model_copy(update={"expected": "Different heading"})
    original = _deliverable(original_claim)
    changed = original.model_copy(
        update={"required_claims": (*default_structured_web_claims(), changed_claim)}
    )
    receipt = structured_web_verification_result(deliverable=original, verdict=_verdict())

    assert not receipt.is_current_for(changed, observed_url=changed.deployment_url)
