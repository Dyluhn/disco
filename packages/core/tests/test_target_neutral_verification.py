from __future__ import annotations

import hashlib
import json

import pytest
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    NoOpCondenser,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
)
from disco.core.effects import ActionProfile, EffectCapability
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import AgentLoop, HostVerificationDeliverable, NeverConfirm
from disco.core.loop.finish.common import (
    _last_productive_seq,
    _last_verification_authority_seq,
)
from disco.core.loop.finish.verify_gates import _typed_host_result
from disco.core.verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    HostVerificationClaimResult,
    VerificationArtifactIdentity,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerificationDeliveryContract,
    VerificationEvidenceModality,
    VerificationExecutionIdentity,
    aggregate_verification_receipts,
    target_verification_result,
    unavailable_verification_result,
)
from loop_fakes import (
    FakeAnalyzer,
    FakeExecutor,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    finish_step,
)
from pydantic import ValidationError

_RUN_ID = "run:sha256:" + "a" * 64


def _claim(claim_id: str, expected: str) -> HostVerificationClaim:
    return HostVerificationClaim(
        claim_id=claim_id,
        kind=VerificationClaimKind.TARGET_SPECIFIC,
        expected=expected,
        source_authority="target.synthetic_native@1",
    )


def _check(check_id: str, claim: HostVerificationClaim) -> VerificationCheckContract:
    return VerificationCheckContract(
        check_id=check_id,
        receipt_kind=f"synthetic.{check_id}@1",
        issuer_id=f"synthetic.{check_id}_verifier@1",
        operation=f"host.verify_{check_id}",
        required_execution_modality="simulator",
        accepted_claim_kinds=frozenset({VerificationClaimKind.TARGET_SPECIFIC}),
        claims=(claim,),
    )


def _contract(
    checks: tuple[VerificationCheckContract, ...],
) -> AdmittedVerificationContract:
    return AdmittedVerificationContract(
        target_id="synthetic.native_application@1",
        verifier_id="synthetic.native_verifier_set@1",
        delivery=VerificationDeliveryContract(
            shape="native.application_bundle",
            mode="interactive",
            entry_kind="bundle_identifier",
            entry_reference="dev.disco.fixture",
        ),
        preview_modality="simulator",
        checks=checks,
    )


def _deliverable(
    contract: AdmittedVerificationContract,
    check: VerificationCheckContract,
    *,
    generation: str = "boot-7",
) -> HostVerificationDeliverable:
    return HostVerificationDeliverable(
        conversation_id="conv_native",
        run_intent_id="intent_native",
        run_identity=_RUN_ID,
        agent_view_id="view_native",
        deliverable_event_id="evt_delivery",
        artifact_path="build/Fixture.app",
        artifact_kind="app",
        workspace_revision=19,
        workspace_generation="workspace-3",
        workspace_epoch=4,
        observed_after_seq=24,
        required_claims=check.claims,
        verification_contract=contract,
        verification_check=check,
        execution_identity=VerificationExecutionIdentity(
            modality="simulator",
            instance_id="sim-device-1",
            generation=generation,
            locator="dev.disco.fixture",
        ),
    )


def test_failed_partial_mutation_invalidates_verification_without_earning_progress() -> None:
    action = ActionEvent(
        thought="update the application",
        tool_call=ToolCall(tool_name="synthetic_mutator", arguments={}),
    ).model_copy(update={"seq": 10})
    failure = AgentErrorEvent(
        error="synthetic failure after a partial write",
        action_id=action.id,
        tool_call_id=action.tool_call.call_id,
        action_profile=ActionProfile(capabilities=frozenset({EffectCapability.WORKSPACE_MUTATE})),
    ).model_copy(update={"seq": 11})

    assert _last_productive_seq([action, failure]) == 0
    assert _last_verification_authority_seq([action, failure]) == 11


def _pass_result(
    deliverable: HostVerificationDeliverable,
) -> HostVerificationClaimResult:
    claim = deliverable.required_claims[0]
    return HostVerificationClaimResult(
        claim_id=claim.claim_id,
        kind=claim.kind,
        required=claim.required,
        expected=claim.expected,
        source_authority=claim.source_authority,
        status=VerificationClaimStatus.PASS,
        reason="target verifier observed the exact required native outcome",
        verifier_id=deliverable.verification_check.issuer_id,  # type: ignore[union-attr]
        capability_basis="configured target-specific simulator verifier",
        evidence_modalities=(VerificationEvidenceModality.TARGET_SPECIFIC,),
        evidence_refs=("simulator:sim-device-1",),
    )


def test_multiple_native_checks_aggregate_without_browser_or_html() -> None:
    launch = _check("launch", _claim("native.launch", "bundle launches"))
    interaction = _check(
        "interaction",
        _claim("native.interaction", "primary action changes target state"),
    )
    contract = _contract((launch, interaction))
    launch_delivery = _deliverable(contract, launch)
    interaction_delivery = _deliverable(contract, interaction)
    receipts = tuple(
        target_verification_result(
            deliverable=deliverable,
            claim_results=(_pass_result(deliverable),),
            verifier_id=deliverable.verification_check.issuer_id,  # type: ignore[union-attr]
            tool_id=deliverable.verification_check.operation,  # type: ignore[union-attr]
            reason="pass",
        )
        for deliverable in (launch_delivery, interaction_delivery)
    )

    coverage = aggregate_verification_receipts(
        deliverables=(launch_delivery, interaction_delivery),
        receipts=receipts,
    )
    assert coverage.passed
    assert coverage.missing_claim_ids == ()
    assert all(receipt.effect_receipt is not None for receipt in receipts)
    serialized = contract.model_dump_json().casefold()
    assert "browser" not in serialized
    assert "html" not in serialized
    assert "http" not in serialized


def test_output_producing_target_pass_requires_exact_artifact_identity() -> None:
    check = _check("launch", _claim("native.launch", "bundle launches")).model_copy(
        update={"required_artifact_identity_scheme": "sha256-tree-manifest-v1"}
    )
    contract = _contract((check,))
    unsealed = _deliverable(contract, check)

    with pytest.raises(ValueError, match="immutable artifact identity"):
        target_verification_result(
            deliverable=unsealed,
            claim_results=(_pass_result(unsealed),),
            verifier_id=check.issuer_id,
            tool_id=check.operation,
            reason="pass",
        )

    wrong_scheme = unsealed.model_copy(
        update={
            "artifact_identity": VerificationArtifactIdentity(
                scheme="opaque-unverified",
                digest="sha256:" + "e" * 64,
                entry_reference=unsealed.artifact_path,
                producer_id=check.issuer_id,
            )
        }
    )
    with pytest.raises(ValueError, match="exact immutable artifact identity scheme"):
        target_verification_result(
            deliverable=wrong_scheme,
            claim_results=(_pass_result(wrong_scheme),),
            verifier_id=check.issuer_id,
            tool_id=check.operation,
            reason="pass",
        )

    sealed = unsealed.model_copy(
        update={
            "artifact_identity": VerificationArtifactIdentity(
                scheme="sha256-tree-manifest-v1",
                digest="sha256:" + "d" * 64,
                entry_reference=unsealed.artifact_path,
                producer_id=check.issuer_id,
            )
        }
    )
    receipt = target_verification_result(
        deliverable=sealed,
        claim_results=(_pass_result(sealed),),
        verifier_id=check.issuer_id,
        tool_id=check.operation,
        reason="pass",
    )

    assert receipt.passed
    assert receipt.artifact_identity == sealed.artifact_identity


def test_missing_or_foreign_native_receipt_cannot_finish() -> None:
    launch = _check("launch", _claim("native.launch", "bundle launches"))
    interaction = _check(
        "interaction",
        _claim("native.interaction", "primary action changes target state"),
    )
    contract = _contract((launch, interaction))
    launch_delivery = _deliverable(contract, launch)
    interaction_delivery = _deliverable(contract, interaction)
    launch_receipt = target_verification_result(
        deliverable=launch_delivery,
        claim_results=(_pass_result(launch_delivery),),
        verifier_id=launch.issuer_id,
        tool_id=launch.operation,
        reason="pass",
    )

    missing = aggregate_verification_receipts(
        deliverables=(launch_delivery, interaction_delivery),
        receipts=(launch_receipt,),
    )
    assert missing.status is VerificationClaimStatus.UNAVAILABLE
    assert missing.missing_claim_ids == ("native.interaction",)

    restarted_delivery = _deliverable(contract, launch, generation="boot-8")
    foreign = aggregate_verification_receipts(
        deliverables=(restarted_delivery,),
        receipts=(launch_receipt,),
    )
    assert foreign.status is VerificationClaimStatus.UNAVAILABLE
    assert foreign.missing_claim_ids == ("native.launch",)


def test_receipt_from_another_target_check_cannot_cross_certify() -> None:
    launch = _check("launch", _claim("native.launch", "bundle launches"))
    interaction = _check(
        "interaction",
        _claim("native.interaction", "primary action changes target state"),
    )
    contract = _contract((launch, interaction))
    launch_delivery = _deliverable(contract, launch)
    interaction_delivery = _deliverable(contract, interaction)
    launch_receipt = target_verification_result(
        deliverable=launch_delivery,
        claim_results=(_pass_result(launch_delivery),),
        verifier_id=launch.issuer_id,
        tool_id=launch.operation,
        reason="pass",
    )

    coverage = aggregate_verification_receipts(
        deliverables=(interaction_delivery,),
        receipts=(launch_receipt,),
    )
    assert coverage.status is VerificationClaimStatus.UNAVAILABLE
    assert coverage.missing_claim_ids == ("native.interaction",)


def test_foreign_verifier_or_operation_cannot_mint_a_governed_pass() -> None:
    check = _check("launch", _claim("native.launch", "bundle launches"))
    deliverable = _deliverable(_contract((check,)), check)

    with pytest.raises(ValueError, match="issuer/operation"):
        target_verification_result(
            deliverable=deliverable,
            claim_results=(_pass_result(deliverable),),
            verifier_id="foreign.verifier@9",
            tool_id=check.operation,
            reason="foreign pass",
        )
    with pytest.raises(ValueError, match="issuer/operation"):
        target_verification_result(
            deliverable=deliverable,
            claim_results=(_pass_result(deliverable),),
            verifier_id=check.issuer_id,
            tool_id="foreign.verify",
            reason="foreign pass",
        )


def test_screenshot_provenance_without_vision_authority_cannot_pass_visual_claim() -> None:
    with pytest.raises(ValidationError, match="vision-capable"):
        HostVerificationClaimResult(
            claim_id="native.visual",
            kind=VerificationClaimKind.VISUAL_SEMANTIC,
            expected="matches the reference",
            source_authority="user_event:evt_visual",
            status=VerificationClaimStatus.PASS,
            reason="a screenshot exists",
            verifier_id="synthetic.text_only@1",
            capability_basis="configured text-only target verifier",
            evidence_modalities=(VerificationEvidenceModality.SCREENSHOT_PIXELS,),
            evidence_refs=("screenshot:output.png",),
        )


def test_interactive_contract_rejects_workspace_only_execution_modality() -> None:
    check = _check("launch", _claim("native.launch", "bundle launches")).model_copy(
        update={"required_execution_modality": "workspace_artifact"}
    )
    with pytest.raises(ValidationError, match="explicit runtime modality"):
        _contract((check,))


def test_pre_field_builtin_web_contract_replays_with_authoritative_modality() -> None:
    claim = HostVerificationClaim(
        claim_id="web.http_ready",
        kind=VerificationClaimKind.HTTP_READY,
        source_authority="target.freeform_web.functional_floor@1",
    )
    raw = {
        "check_id": "web_functional",
        "receipt_kind": "disco.web_functional@1",
        "issuer_id": "disco.host_web_verifier@1",
        "operation": "host.verify_deliverable",
        "accepted_claim_kinds": ["http_ready"],
        "claims": [claim.model_dump(mode="json")],
    }

    migrated = VerificationCheckContract.model_validate(raw)

    assert migrated.required_execution_modality == "managed_preview"


def test_pre_field_contract_replay_preserves_linked_authority_digest() -> None:
    check = _check("launch", _claim("native.launch", "bundle launches"))
    raw_check = check.model_dump(mode="json")
    raw_check.pop("required_execution_modality")
    raw_check.pop("required_artifact_identity_scheme")
    raw_check["receipt_kind"] = "disco.web_functional@1"
    raw_check["issuer_id"] = "disco.host_web_verifier@1"
    raw_check["operation"] = "host.verify_deliverable"
    raw = {
        "schema_version": 1,
        "target_id": "disco.legacy_web@1",
        "verifier_id": "disco.host_web_verifier@1",
        "delivery": {
            "shape": "web.legacy_deliverable",
            "mode": "interactive",
            "entry_kind": "manifest",
            "entry_reference": "active-deliverable",
            "entry_parameters": [],
        },
        "preview_modality": "legacy_host",
        "checks": [raw_check],
        "required": True,
        "unavailable": "block",
        "unverified_finish": "block",
    }
    old_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                raw,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )

    replayed = AdmittedVerificationContract.model_validate(raw)

    assert replayed.schema_version == 1
    assert replayed.checks[0].required_execution_modality == "managed_preview"
    assert replayed.digest == old_digest


def test_pre_artifact_identity_contract_replay_preserves_v2_digest() -> None:
    contract = _contract((_check("launch", _claim("native.launch", "bundle launches")),))
    raw = contract.model_dump(mode="json")
    raw["schema_version"] = 2
    for check in raw["checks"]:
        check.pop("required_artifact_identity_scheme")
    old_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                raw,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )

    replayed = AdmittedVerificationContract.model_validate(raw)

    assert replayed.schema_version == 2
    assert replayed.checks[0].required_artifact_identity_scheme is None
    assert replayed.digest == old_digest


def test_copied_receipt_instance_cannot_bypass_live_boundary_revalidation() -> None:
    check = _check("launch", _claim("native.launch", "bundle launches"))
    deliverable = _deliverable(_contract((check,)), check)
    receipt = target_verification_result(
        deliverable=deliverable,
        claim_results=(_pass_result(deliverable),),
        verifier_id=check.issuer_id,
        tool_id=check.operation,
        reason="pass",
    )
    forged = receipt.model_copy(
        update={
            "verifier_id": "foreign.verifier@1",
            "tool_id": "foreign.operation",
        }
    )

    assert (
        _typed_host_result(
            {"verification_result": forged, "url": ""},
            deliverable,
        )
        is None
    )
    assert not forged.is_current_authority_for(deliverable, observed_url="")


def test_aggregate_itself_rejects_cross_generation_checks() -> None:
    launch = _check("launch", _claim("native.launch", "bundle launches"))
    interaction = _check(
        "interaction",
        _claim("native.interaction", "primary action changes target state"),
    )
    contract = _contract((launch, interaction))
    launch_delivery = _deliverable(contract, launch, generation="boot-A")
    interaction_delivery = _deliverable(contract, interaction, generation="boot-B")
    receipts = tuple(
        target_verification_result(
            deliverable=deliverable,
            claim_results=(_pass_result(deliverable),),
            verifier_id=deliverable.verification_check.issuer_id,  # type: ignore[union-attr]
            tool_id=deliverable.verification_check.operation,  # type: ignore[union-attr]
            reason="pass",
        )
        for deliverable in (launch_delivery, interaction_delivery)
    )

    coverage = aggregate_verification_receipts(
        deliverables=(launch_delivery, interaction_delivery),
        receipts=receipts,
    )

    assert coverage.status is VerificationClaimStatus.UNAVAILABLE
    assert coverage.missing_claim_ids == ("native.launch", "native.interaction")


class _NativeVerifier:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[HostVerificationDeliverable] = []

    async def verify(self, deliverable: HostVerificationDeliverable) -> dict:
        self.calls.append(deliverable)
        if not self.available:
            receipt = unavailable_verification_result(
                deliverable=deliverable,
                reason="configured simulator verifier is unavailable",
                verifier_id=deliverable.verification_check.issuer_id,  # type: ignore[union-attr]
                tool_id=deliverable.verification_check.operation,  # type: ignore[union-attr]
            )
            return {
                "passed": False,
                "verdict": "unavailable",
                "url": "",
                "summary": receipt.reason,
                "verification_result": receipt.model_dump(mode="json"),
            }
        receipt = target_verification_result(
            deliverable=deliverable,
            claim_results=(_pass_result(deliverable),),
            verifier_id=deliverable.verification_check.issuer_id,  # type: ignore[union-attr]
            tool_id=deliverable.verification_check.operation,  # type: ignore[union-attr]
            reason="native target passed",
        )
        return {
            "passed": True,
            "verdict": "pass",
            "url": "",
            "summary": receipt.reason,
            "verification_result": receipt.model_dump(mode="json"),
        }

    async def bind_execution(
        self,
        deliverable: HostVerificationDeliverable,
    ) -> HostVerificationDeliverable:
        return deliverable.model_copy(
            update={
                "execution_identity": VerificationExecutionIdentity(
                    modality="simulator",
                    instance_id="sim-device-1",
                    generation="boot-1",
                    locator="dev.disco.fixture",
                )
            }
        )


class _ChangingExecutionVerifier(_NativeVerifier):
    def __init__(self) -> None:
        super().__init__()
        self.bind_calls = 0

    async def bind_execution(
        self,
        deliverable: HostVerificationDeliverable,
    ) -> HostVerificationDeliverable:
        self.bind_calls += 1
        return deliverable.model_copy(
            update={
                "execution_identity": VerificationExecutionIdentity(
                    modality="simulator",
                    instance_id="sim-device-1",
                    generation=f"boot-{self.bind_calls}",
                    locator="dev.disco.fixture",
                )
            }
        )


class _NoBindingNativeVerifier(_NativeVerifier):
    async def bind_execution(
        self,
        deliverable: HostVerificationDeliverable,
    ) -> HostVerificationDeliverable:
        return deliverable


def _native_loop(
    agent: ScriptedAgent,
    verifier: _NativeVerifier,
) -> tuple[AgentLoop, SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    executor = FakeExecutor(
        tools=[
            ToolSpec(name="file_write", description="write", parameters_schema={}),
            ToolSpec(name="serve", description="serve", parameters_schema={}),
        ]
    )
    loop = AgentLoop(
        "conv_native",
        store,
        agent,
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.INTERACTIVE,
        host_verifier=verifier,
        host_verify_authoritative=True,
    )
    return loop, store


def _native_admission(contract: AdmittedVerificationContract) -> BuildPlatformAdmissionEvent:
    return BuildPlatformAdmissionEvent(
        route="platform",
        profile_id="synthetic.native_profile@1",
        run_intent_id="intent_native",
        composition_authority="build_platform_core",
        composition_digest="sha256:" + "b" * 64,
        run_identity=_RUN_ID,
        verification_claims=contract.required_claims,
        verification_contract=contract,
    )


@pytest.mark.asyncio
async def test_governed_native_finish_uses_target_verifier_without_browser_gate() -> None:
    launch = _check("launch", _claim("native.launch", "bundle launches"))
    interaction = _check(
        "interaction",
        _claim("native.interaction", "primary action changes target state"),
    )
    contract = _contract((launch, interaction))
    verifier = _NativeVerifier()
    loop, store = _native_loop(
        ScriptedAgent(
            [
                action_step(
                    tool="file_write",
                    args={"path": "build/Fixture.app", "content": "native-bundle"},
                ),
                action_step(
                    tool="serve",
                    args={
                        "title": "Fixture",
                        "path": "build/Fixture.app",
                        "kind": "app",
                    },
                ),
                finish_step("native target complete"),
            ]
        ),
        verifier,
    )
    await loop._emit(_native_admission(contract))
    await loop.send_message("build a native application fixture")

    state = await loop.run()
    events = await store.get_events("conv_native")

    assert state.execution_status is ConversationStatus.FINISHED
    assert len(verifier.calls) == 2
    assert [call.verification_check.check_id for call in verifier.calls] == [  # type: ignore[union-attr]
        "launch",
        "interaction",
    ]
    assert verifier.calls[0].preview_selection is None
    assert verifier.calls[0].execution_identity is not None
    assert verifier.calls[0].execution_identity.modality == "simulator"
    assert not any(
        isinstance(event, StatusEvent) and event.detail == "unverified_release" for event in events
    )


@pytest.mark.asyncio
async def test_governed_native_finish_fails_closed_when_target_verifier_unavailable() -> None:
    check = _check("launch", _claim("native.launch", "bundle launches"))
    contract = _contract((check,))
    verifier = _NativeVerifier(available=False)
    loop, store = _native_loop(
        ScriptedAgent(
            [
                action_step(
                    tool="file_write",
                    args={"path": "build/Fixture.app", "content": "native-bundle"},
                ),
                action_step(
                    tool="serve",
                    args={
                        "title": "Fixture",
                        "path": "build/Fixture.app",
                        "kind": "app",
                    },
                ),
                finish_step("native target complete"),
                finish_step("native target complete"),
            ]
        ),
        verifier,
    )
    await loop._emit(_native_admission(contract))
    await loop.send_message("build a native application fixture")

    state = await loop.run()
    events = await store.get_events("conv_native")

    assert state.execution_status is not ConversationStatus.FINISHED
    assert any(
        isinstance(event, StatusEvent) and event.status is ConversationStatus.STUCK
        for event in events
    )
    assert not any(
        isinstance(event, StatusEvent) and event.detail == "unverified_release" for event in events
    )


@pytest.mark.asyncio
async def test_interactive_native_finish_requires_target_runtime_binding() -> None:
    check = _check("launch", _claim("native.launch", "bundle launches"))
    contract = _contract((check,))
    verifier = _NoBindingNativeVerifier()
    loop, store = _native_loop(
        ScriptedAgent(
            [
                action_step(
                    tool="file_write",
                    args={"path": "build/Fixture.app", "content": "native-bundle"},
                ),
                action_step(
                    tool="serve",
                    args={
                        "title": "Fixture",
                        "path": "build/Fixture.app",
                        "kind": "app",
                    },
                ),
                finish_step("native target complete"),
                finish_step("native target complete"),
            ]
        ),
        verifier,
    )
    await loop._emit(_native_admission(contract))
    await loop.send_message("build a native application fixture")

    state = await loop.run()
    events = await store.get_events("conv_native")

    assert state.execution_status is not ConversationStatus.FINISHED
    assert verifier.calls == []
    assert any(
        isinstance(event, StatusEvent) and event.status is ConversationStatus.STUCK
        for event in events
    )


@pytest.mark.asyncio
async def test_required_checks_cannot_certify_different_execution_generations() -> None:
    launch = _check("launch", _claim("native.launch", "bundle launches"))
    interaction = _check(
        "interaction",
        _claim("native.interaction", "primary action changes target state"),
    )
    contract = _contract((launch, interaction))
    verifier = _ChangingExecutionVerifier()
    loop, store = _native_loop(
        ScriptedAgent(
            [
                action_step(
                    tool="file_write",
                    args={"path": "build/Fixture.app", "content": "native-bundle"},
                ),
                action_step(
                    tool="serve",
                    args={
                        "title": "Fixture",
                        "path": "build/Fixture.app",
                        "kind": "app",
                    },
                ),
                finish_step("native target complete"),
                finish_step("native target complete"),
            ]
        ),
        verifier,
    )
    await loop._emit(_native_admission(contract))
    await loop.send_message("build a native application fixture")

    state = await loop.run()
    events = await store.get_events("conv_native")

    assert state.execution_status is not ConversationStatus.FINISHED
    assert verifier.calls == []
    assert any(
        isinstance(event, StatusEvent) and event.status is ConversationStatus.STUCK
        for event in events
    )
