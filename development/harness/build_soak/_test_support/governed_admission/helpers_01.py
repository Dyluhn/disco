"""Shared helpers for the moved collection implementations."""

from __future__ import annotations

from dataclasses import dataclass

from ._shared import (
    _RUN_ID,
    AdmittedVerificationContract,
    Any,
    GovernedAdmissionOracle,
    HostVerificationClaim,
    HostVerificationClaimResult,
    HostVerificationDeliverable,
    HostVerificationResult,
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerificationDeliveryContract,
    VerificationEvidenceModality,
    VerificationExecutionIdentity,
    _preview_identity_from_pair,
    default_structured_web_claims,
    hashlib,
    json,
    target_verification_result,
    with_verification_effect_receipt,
)


def _scenario(*, native: bool = False) -> dict[str, Any]:
    return {
        "id": "fixture",
        "assertions": {
            "governed_verification": {
                "required": True,
                "route": "platform",
                "composition_authority": "build_platform_core",
                "delivery_mode": "artifact" if native else "interactive",
                "required_receipt_kinds": [
                    "synthetic.native@1" if native else "disco.web_functional@1"
                ],
                "required_claim_kinds": (
                    {"target_specific": 1}
                    if native
                    else {
                        "artifact_identity": 1,
                        "http_ready": 1,
                        "rendered_content": 1,
                        "console_clean": 1,
                        "network_clean": 1,
                    }
                ),
                "required_execution_modality": ("simulator" if native else "managed_preview"),
            }
        },
    }


def _contract(
    *,
    native: bool = False,
    sealed_output: bool = False,
) -> AdmittedVerificationContract:
    claims = (
        (
            HostVerificationClaim(
                claim_id="native.launch",
                kind=VerificationClaimKind.TARGET_SPECIFIC,
                expected="bundle launches",
                source_authority="target.native@1",
            ),
        )
        if native
        else default_structured_web_claims()
    )
    return AdmittedVerificationContract(
        target_id="synthetic.native@1" if native else "disco.legacy_web@1",
        verifier_id=("synthetic.native_verifier@1" if native else "disco.host_web_verifier@1"),
        delivery=VerificationDeliveryContract(
            shape="native.bundle" if native else "web.legacy_deliverable",
            mode="artifact" if native else "interactive",
            entry_kind="manifest",
            entry_reference="build/Fixture.app" if native else "active-deliverable",
        ),
        preview_modality="simulator" if native else "legacy_host",
        checks=(
            VerificationCheckContract(
                check_id="native" if native else "web_functional",
                receipt_kind="synthetic.native@1" if native else "disco.web_functional@1",
                issuer_id=(
                    "synthetic.native_verifier@1" if native else "disco.host_web_verifier@1"
                ),
                operation="host.verify_native" if native else "host.verify_deliverable",
                required_execution_modality="simulator" if native else "managed_preview",
                required_artifact_identity_scheme=(
                    "sha256-tree-manifest-v1" if sealed_output else None
                ),
                accepted_claim_kinds=frozenset(claim.kind for claim in claims),
                claims=claims,
            ),
        ),
    )


def _claim_results(
    contract: AdmittedVerificationContract,
    *,
    passed: bool,
    evidence_event_id: str | None = None,
) -> tuple[HostVerificationClaimResult, ...]:
    status = VerificationClaimStatus.PASS if passed else VerificationClaimStatus.FAIL
    return tuple(
        HostVerificationClaimResult(
            claim_id=claim.claim_id,
            kind=claim.kind,
            required=claim.required,
            expected=claim.expected,
            source_authority=claim.source_authority,
            status=status,
            reason="fixture pass" if passed else "fixture fail",
            verifier_id=contract.checks[0].issuer_id,
            capability_basis="configured target-specific host verifier",
            evidence_modalities=(VerificationEvidenceModality.TARGET_SPECIFIC,),
            evidence_refs=(
                (f"event:{evidence_event_id}",) if evidence_event_id is not None else ()
            ),
        )
        for claim in contract.required_claims
    )


@dataclass(frozen=True)
class _SegmentFixture:
    base: int
    passed: bool
    sealed_output: bool
    contract: AdmittedVerificationContract
    intent_id: str
    admission_id: str
    view_id: str
    write_action_id: str
    delivery_id: str
    started_id: str
    action_id: str
    observation_id: str
    preview_intent: dict[str, Any]
    preview: PreviewSelectionIdentity | None
    execution: VerificationExecutionIdentity
    artifact_path: str
    artifact_kind: str
    output_observation_id: str
    receipt: dict[str, Any]


def _preview_fixture(
    *,
    base: int,
    native: bool,
    action_id: str,
    observation_id: str,
) -> tuple[dict[str, Any], PreviewSelectionIdentity | None]:
    preview_intent = {"launch_kind": "framework"}
    preview_digest = hashlib.sha256(
        json.dumps(
            {
                "command": "npm run dev",
                "exec_dir": ".",
                "intent": preview_intent,
                "name": "web",
                "port": 9134,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    preview = (
        None
        if native
        else PreviewSelectionIdentity(
            projection_id="pv_" + f"{base + 1:032x}"[-32:],
            session_name="web",
            port=9134,
            url="http://127.0.0.1:9134/",
            launch_kind="framework",
            intent_digest=preview_digest,
            sandbox_instance_id="sandbox-1",
            sandbox_generation=1,
            source_action_id=action_id,
            source_action_seq=base + 6,
            source_observation_id=observation_id,
            source_observation_seq=base + 7,
        )
    )
    return preview_intent, preview


def _execution_fixture(
    *,
    native: bool,
    preview: PreviewSelectionIdentity | None,
) -> VerificationExecutionIdentity:
    return VerificationExecutionIdentity(
        modality="simulator" if native else "managed_preview",
        instance_id="sim-1" if native else preview.projection_id,  # type: ignore[union-attr]
        generation="boot-1" if native else "sandbox-1:1",
        locator="dev.fixture" if native else preview.url,  # type: ignore[union-attr]
    )


def _receipt_fixture(
    *,
    contract: AdmittedVerificationContract,
    base: int,
    passed: bool,
    native: bool,
    sealed_output: bool,
    intent_id: str,
    delivery_id: str,
    preview: PreviewSelectionIdentity | None,
    execution: VerificationExecutionIdentity,
) -> tuple[str, str, str, dict[str, Any]]:
    artifact_path = "build/Fixture.app" if native else "index.html"
    artifact_kind = "files" if native else "app"
    output_observation_id = f"evt_verifier_output_{base}"
    artifact_identity = (
        VerificationArtifactIdentity(
            scheme="sha256-tree-manifest-v1",
            digest="sha256:" + "d" * 64,
            entry_reference=artifact_path,
            producer_id=contract.checks[0].issuer_id,
        )
        if sealed_output
        else None
    )
    deliverable = HostVerificationDeliverable(
        conversation_id="conv_fixture",
        run_intent_id=intent_id,
        run_identity=_RUN_ID,
        agent_view_id="view-fixture",
        deliverable_event_id=delivery_id,
        artifact_path=artifact_path,
        artifact_kind=artifact_kind,
        workspace_revision=base + 4,
        workspace_generation="workspace-1",
        workspace_epoch=1,
        observed_after_seq=base + 11 if sealed_output else base + 8,
        required_claims=contract.required_claims,
        verification_contract=contract,
        verification_check=contract.checks[0],
        execution_identity=execution,
        artifact_identity=artifact_identity,
        preview_selection=preview,
    )
    receipt = target_verification_result(
        deliverable=deliverable,
        claim_results=_claim_results(
            contract,
            passed=passed,
            evidence_event_id=output_observation_id if sealed_output else None,
        ),
        verifier_id=contract.checks[0].issuer_id,
        tool_id=contract.checks[0].operation,
        reason="fixture pass" if passed else "fixture fail",
        observed_url="" if native else preview.url,  # type: ignore[union-attr]
    ).model_dump(mode="json")
    return artifact_path, artifact_kind, output_observation_id, receipt


def _segment_fixture(
    *,
    base: int,
    passed: bool,
    native: bool,
    sealed_output: bool,
) -> _SegmentFixture:
    contract = _contract(native=native, sealed_output=sealed_output)
    intent_id = f"evt_intent_{base}"
    delivery_id = f"evt_delivery_{base}"
    action_id = f"evt_preview_action_{base}"
    observation_id = f"evt_preview_observation_{base}"
    preview_intent, preview = _preview_fixture(
        base=base,
        native=native,
        action_id=action_id,
        observation_id=observation_id,
    )
    execution = _execution_fixture(native=native, preview=preview)
    artifact_path, artifact_kind, output_observation_id, receipt = _receipt_fixture(
        contract=contract,
        base=base,
        passed=passed,
        native=native,
        sealed_output=sealed_output,
        intent_id=intent_id,
        delivery_id=delivery_id,
        preview=preview,
        execution=execution,
    )
    return _SegmentFixture(
        base=base,
        passed=passed,
        sealed_output=sealed_output,
        contract=contract,
        intent_id=intent_id,
        admission_id=f"evt_admission_{base}",
        view_id=f"evt_view_{base}",
        write_action_id=f"evt_write_action_{base}",
        delivery_id=delivery_id,
        started_id=f"evt_started_{base}",
        action_id=action_id,
        observation_id=observation_id,
        preview_intent=preview_intent,
        preview=preview,
        execution=execution,
        artifact_path=artifact_path,
        artifact_kind=artifact_kind,
        output_observation_id=output_observation_id,
        receipt=receipt,
    )


def _base_segment_events(fixture: _SegmentFixture) -> list[dict[str, Any]]:
    return [
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": fixture.base + 1,
            "id": fixture.intent_id,
            "operation": "agent.run-intent.message",
        },
        {
            "kind": "build_platform_admission",
            "source": "system",
            "seq": fixture.base + 2,
            "id": fixture.admission_id,
            "route": "platform",
            "profile_id": "synthetic.profile@1",
            "run_intent_id": fixture.intent_id,
            "composition_authority": "build_platform_core",
            "composition_digest": "sha256:" + "a" * 64,
            "run_identity": _RUN_ID,
            "verification_claims": [
                claim.model_dump(mode="json") for claim in fixture.contract.required_claims
            ],
            "verification_contract": fixture.contract.model_dump(mode="json"),
        },
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": fixture.base + 3,
            "id": fixture.view_id,
            "operation": "agent.view-admitted",
            "run_intent_id": fixture.intent_id,
            "agent_view_id": "view-fixture",
        },
        {
            "kind": "action",
            "source": "agent",
            "seq": fixture.base + 4,
            "id": fixture.write_action_id,
            "agent_view_id": "view-fixture",
            "tool_call": {
                "tool_name": "file_write",
                "call_id": "call-write",
                "arguments": {"path": fixture.artifact_path},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": fixture.base + 5,
            "id": f"evt_write_observation_{fixture.base}",
            "agent_view_id": "view-fixture",
            "action_id": fixture.write_action_id,
            "tool_result": {
                "tool_name": "file_write",
                "call_id": "call-write",
                "success": True,
            },
        },
    ]


def _preview_segment_events(fixture: _SegmentFixture) -> list[dict[str, Any]]:
    preview = fixture.preview
    if preview is None:
        return []
    return [
        {
            "kind": "action",
            "source": "agent",
            "seq": fixture.base + 6,
            "id": fixture.action_id,
            "tool_call": {
                "tool_name": "preview_start",
                "call_id": "call-preview",
                "arguments": {},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": fixture.base + 7,
            "id": fixture.observation_id,
            "action_id": fixture.action_id,
            "tool_result": {
                "tool_name": "preview_start",
                "call_id": "call-preview",
                "success": True,
                "structured": {
                    "status": "running",
                    "projection_id": preview.projection_id,
                    "name": preview.session_name,
                    "port": preview.port,
                    "url": preview.url,
                    "command": "npm run dev",
                    "exec_dir": ".",
                    "intent": fixture.preview_intent,
                    "launch_kind": preview.launch_kind,
                    "intent_digest": preview.intent_digest,
                    "sandbox_instance_id": preview.sandbox_instance_id,
                    "sandbox_generation": preview.sandbox_generation,
                },
            },
        },
    ]


def _deliverable_segment_event(fixture: _SegmentFixture) -> dict[str, Any]:
    return {
        "kind": "deliverable",
        "source": "agent",
        "seq": fixture.base + 8,
        "id": fixture.delivery_id,
        "path": fixture.artifact_path,
        "artifact_kind": fixture.artifact_kind,
        "target_id": fixture.contract.target_id,
        "delivery_contract": fixture.contract.delivery.model_dump(mode="json"),
        "verification_contract_digest": fixture.contract.digest,
        "agent_view_id": "view-fixture",
    }


def _started_segment_event(fixture: _SegmentFixture) -> dict[str, Any]:
    preview = fixture.preview
    return {
        "kind": "verifier_started",
        "source": "system",
        "seq": fixture.base + 9,
        "id": fixture.started_id,
        "agent_view_id": "view-fixture",
        "artifact_path": fixture.artifact_path,
        "artifact_kind": fixture.artifact_kind,
        "target_id": fixture.contract.target_id,
        "run_intent_id": fixture.intent_id,
        "run_identity": _RUN_ID,
        "delivery_shape": fixture.contract.delivery.shape,
        "delivery_entry_reference": fixture.contract.delivery.entry_reference,
        "check_id": fixture.contract.checks[0].check_id,
        "receipt_kind": fixture.contract.checks[0].receipt_kind,
        "issuer_id": fixture.contract.checks[0].issuer_id,
        "operation": fixture.contract.checks[0].operation,
        "delegated_issuer_ids": sorted(fixture.contract.checks[0].delegated_issuer_ids),
        "verification_contract_digest": fixture.contract.digest,
        "deliverable_event_id": fixture.delivery_id,
        "execution_identity": fixture.execution.model_dump(mode="json"),
        "preview_selection": preview.model_dump(mode="json") if preview is not None else None,
        "workspace_revision": fixture.base + 4,
        "workspace_generation": "workspace-1",
        "workspace_epoch": 1,
        "observed_after_seq": fixture.base + 8,
    }


def _sealed_output_segment_events(fixture: _SegmentFixture) -> list[dict[str, Any]]:
    if not fixture.sealed_output:
        return []
    action_id = f"evt_verifier_action_{fixture.base}"
    call_id = f"call-verifier-{fixture.base}"
    return [
        {
            "kind": "action",
            "source": "system",
            "seq": fixture.base + 10,
            "id": action_id,
            "tool_call": {
                "tool_name": "verify_native",
                "call_id": call_id,
                "arguments": {},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": fixture.base + 11,
            "id": fixture.output_observation_id,
            "action_id": action_id,
            "tool_result": {
                "tool_name": "verify_native",
                "call_id": call_id,
                "success": True,
            },
        },
    ]


def _terminal_segment_events(fixture: _SegmentFixture) -> list[dict[str, Any]]:
    verdict_seq = fixture.base + 12 if fixture.sealed_output else fixture.base + 10
    terminal_seq = verdict_seq + 1
    return [
        {
            "kind": "verifier_verdict",
            "source": "system",
            "seq": verdict_seq,
            "id": f"evt_verdict_{fixture.base}",
            "artifact_path": fixture.artifact_path,
            "artifact_kind": fixture.artifact_kind,
            "verified": fixture.passed,
            "verdict": "pass" if fixture.passed else "fail",
            "target_id": fixture.contract.target_id,
            "check_id": fixture.contract.checks[0].check_id,
            "receipt_kind": fixture.contract.checks[0].receipt_kind,
            "verification_contract_digest": fixture.contract.digest,
            "requested_by_event_id": fixture.started_id,
            "verification_result": fixture.receipt,
        },
        {
            "kind": "status",
            "source": "system",
            "seq": terminal_seq,
            "id": f"evt_terminal_{fixture.base}",
            "status": "FINISHED",
            "agent_view_id": "view-fixture",
        },
    ]


def _segment(
    *,
    base: int = 0,
    passed: bool = True,
    native: bool = False,
    sealed_output: bool = False,
) -> list[dict[str, Any]]:
    fixture = _segment_fixture(
        base=base,
        passed=passed,
        native=native,
        sealed_output=sealed_output,
    )
    events = _base_segment_events(fixture)
    events.extend(_preview_segment_events(fixture))
    events.append(_deliverable_segment_event(fixture))
    events.append(_started_segment_event(fixture))
    events.extend(_sealed_output_segment_events(fixture))
    events.extend(_terminal_segment_events(fixture))
    return events


def _result(events: list[dict[str, Any]], *, native: bool = False):
    return GovernedAdmissionOracle().check(
        events,
        scenario=_scenario(native=native),
        conversation_id="conv_fixture",
    )[0]


def _replace_receipt(
    events: list[dict[str, Any]],
    **updates: Any,
) -> HostVerificationResult:
    verdict = next(event for event in reversed(events) if event.get("kind") == "verifier_verdict")
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    changed = receipt.model_copy(update={**updates, "effect_receipt": None})
    anchored = with_verification_effect_receipt(changed)
    verdict["verification_result"] = anchored.model_dump(mode="json")
    return anchored


def _use_appkit_owned_preview(events: list[dict[str, Any]], *, passed: bool = True) -> None:
    action = next(
        event
        for event in events
        if event.get("kind") == "action"
        and event.get("tool_call", {}).get("tool_name") == "preview_start"
    )
    observation = next(
        event
        for event in events
        if event.get("kind") == "observation" and event.get("action_id") == action.get("id")
    )
    action["tool_call"]["tool_name"] = "verify_appkit_app"
    result = observation["tool_result"]
    runtime = result["structured"]
    result["tool_name"] = "verify_appkit_app"
    result["structured"] = {
        "passed": passed,
        "preview_runtime": runtime,
    }


def _repeat_appkit_preview(
    events: list[dict[str, Any]],
    *,
    same_operational_identity: bool = True,
) -> dict[str, Any]:
    first_action = next(
        event
        for event in events
        if event.get("kind") == "action"
        and event.get("tool_call", {}).get("tool_name") == "verify_appkit_app"
    )
    first_observation = next(
        event
        for event in events
        if event.get("kind") == "observation" and event.get("action_id") == first_action.get("id")
    )
    repeated_action = json.loads(json.dumps(first_action))
    repeated_observation = json.loads(json.dumps(first_observation))
    repeated_action.update(seq=10, id="evt_appkit_preview_retry")
    repeated_action["tool_call"]["call_id"] = "call-appkit-preview-retry"
    repeated_observation.update(
        seq=11,
        id="evt_appkit_preview_retry_result",
        action_id=repeated_action["id"],
    )
    repeated_observation["tool_result"]["call_id"] = "call-appkit-preview-retry"
    if not same_operational_identity:
        repeated_observation["tool_result"]["structured"]["preview_runtime"]["projection_id"] = (
            "pv_" + "f" * 32
        )
    identity = _preview_identity_from_pair(repeated_action, repeated_observation)
    assert identity is not None
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = events[-1]
    verdict["seq"] = 12
    terminal["seq"] = 13
    events[events.index(verdict) : events.index(verdict)] = [
        repeated_action,
        repeated_observation,
    ]
    return identity


def _continued_segment_reusing_prior_preview(*, stopped: bool = False) -> list[dict[str, Any]]:
    events = [*_segment(base=0), *_segment(base=20)]
    first_verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    first_receipt = HostVerificationResult.model_validate(first_verdict["verification_result"])
    assert first_receipt.preview_selection is not None
    second_preview_action = next(
        event
        for event in events
        if event.get("kind") == "action"
        and event.get("seq") == 26
        and event.get("tool_call", {}).get("tool_name") == "preview_start"
    )
    second_preview_observation = next(
        event
        for event in events
        if event.get("kind") == "observation"
        and event.get("action_id") == second_preview_action["id"]
    )
    events.remove(second_preview_action)
    events.remove(second_preview_observation)
    if stopped:
        events.extend(
            [
                {
                    "kind": "action",
                    "source": "agent",
                    "seq": 26,
                    "id": "evt_continue_preview_stop",
                    "tool_call": {
                        "tool_name": "preview_stop",
                        "call_id": "call-continue-preview-stop",
                        "arguments": {"name": "web"},
                    },
                },
                {
                    "kind": "observation",
                    "source": "environment",
                    "seq": 27,
                    "id": "evt_continue_preview_stop_result",
                    "action_id": "evt_continue_preview_stop",
                    "tool_result": {
                        "tool_name": "preview_stop",
                        "call_id": "call-continue-preview-stop",
                        "success": True,
                        "structured": {"stopped": ["web"]},
                    },
                },
            ]
        )
    started = next(event for event in reversed(events) if event.get("kind") == "verifier_started")
    started["preview_selection"] = first_receipt.preview_selection.model_dump(mode="json")
    started["execution_identity"] = first_receipt.execution_identity.model_dump(mode="json")  # type: ignore[union-attr]
    _replace_receipt(
        events,
        preview_selection=first_receipt.preview_selection,
        execution_identity=first_receipt.execution_identity,
    )
    return events


def _shift_verdict_and_insert_mutation(events: list[dict[str, Any]], operation: str) -> None:
    """Insert a workspace_mutation between the receipt's observed_after_seq and
    the verdict by shifting the verdict/terminal seqs up one slot."""
    verdict = next(event for event in reversed(events) if event.get("kind") == "verifier_verdict")
    terminal = next(event for event in reversed(events) if event.get("kind") == "status")
    inserted_seq = verdict["seq"]
    verdict["seq"] += 1
    terminal["seq"] += 1
    events.insert(
        events.index(verdict),
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": inserted_seq,
            "id": f"evt_inserted_mutation_{inserted_seq}",
            "operation": operation,
        },
    )


def _insert_mutation_before_terminal(events: list[dict[str, Any]], operation: str) -> None:
    """Insert a workspace_mutation between the PASS verdict and the terminal
    status, reproducing the live terminal layout (verdict, agent message, fold,
    FINISHED) by shifting the terminal seq up one slot."""
    terminal = next(event for event in reversed(events) if event.get("kind") == "status")
    inserted_seq = terminal["seq"]
    terminal["seq"] += 1
    events.insert(
        events.index(terminal),
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": inserted_seq,
            "id": f"evt_inserted_terminal_mutation_{inserted_seq}",
            "operation": operation,
        },
    )
