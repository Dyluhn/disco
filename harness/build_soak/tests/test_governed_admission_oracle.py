from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from disco.core.loop import HostVerificationDeliverable
from disco.core.verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    HostVerificationClaimResult,
    HostVerificationResult,
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerificationDeliveryContract,
    VerificationEvidenceModality,
    VerificationExecutionIdentity,
    default_structured_web_claims,
    target_verification_result,
    with_verification_effect_receipt,
)

from harness.build_soak.failure_codes import GOVERNED_ADMISSION_BYPASSED
from harness.build_soak.oracles.contract import ContractOracle
from harness.build_soak.oracles.governed_admission import (
    GovernedAdmissionOracle,
    _preview_identity_from_pair,
    _shared_execution_authority,
)
from harness.build_soak.run import load_scenarios

_RUN_ID = "run:sha256:" + "b" * 64


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


def _segment(
    *,
    base: int = 0,
    passed: bool = True,
    native: bool = False,
    sealed_output: bool = False,
) -> list[dict[str, Any]]:
    contract = _contract(native=native, sealed_output=sealed_output)
    intent_id = f"evt_intent_{base}"
    admission_id = f"evt_admission_{base}"
    view_id = f"evt_view_{base}"
    write_action_id = f"evt_write_action_{base}"
    delivery_id = f"evt_delivery_{base}"
    started_id = f"evt_started_{base}"
    action_id = f"evt_preview_action_{base}"
    observation_id = f"evt_preview_observation_{base}"
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
    execution = VerificationExecutionIdentity(
        modality="simulator" if native else "managed_preview",
        instance_id="sim-1" if native else preview.projection_id,  # type: ignore[union-attr]
        generation="boot-1" if native else "sandbox-1:1",
        locator="dev.fixture" if native else preview.url,  # type: ignore[union-attr]
    )
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
    events: list[dict[str, Any]] = [
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": base + 1,
            "id": intent_id,
            "operation": "agent.run-intent.message",
        },
        {
            "kind": "build_platform_admission",
            "source": "system",
            "seq": base + 2,
            "id": admission_id,
            "route": "platform",
            "profile_id": "synthetic.profile@1",
            "run_intent_id": intent_id,
            "composition_authority": "build_platform_core",
            "composition_digest": "sha256:" + "a" * 64,
            "run_identity": _RUN_ID,
            "verification_claims": [
                claim.model_dump(mode="json") for claim in contract.required_claims
            ],
            "verification_contract": contract.model_dump(mode="json"),
        },
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": base + 3,
            "id": view_id,
            "operation": "agent.view-admitted",
            "run_intent_id": intent_id,
            "agent_view_id": "view-fixture",
        },
        {
            "kind": "action",
            "source": "agent",
            "seq": base + 4,
            "id": write_action_id,
            "agent_view_id": "view-fixture",
            "tool_call": {
                "tool_name": "file_write",
                "call_id": "call-write",
                "arguments": {"path": artifact_path},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": base + 5,
            "id": f"evt_write_observation_{base}",
            "agent_view_id": "view-fixture",
            "action_id": write_action_id,
            "tool_result": {
                "tool_name": "file_write",
                "call_id": "call-write",
                "success": True,
            },
        },
    ]
    if preview is not None:
        events.extend(
            [
                {
                    "kind": "action",
                    "source": "agent",
                    "seq": base + 6,
                    "id": action_id,
                    "tool_call": {
                        "tool_name": "preview_start",
                        "call_id": "call-preview",
                        "arguments": {},
                    },
                },
                {
                    "kind": "observation",
                    "source": "environment",
                    "seq": base + 7,
                    "id": observation_id,
                    "action_id": action_id,
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
                            "intent": preview_intent,
                            "launch_kind": preview.launch_kind,
                            "intent_digest": preview.intent_digest,
                            "sandbox_instance_id": preview.sandbox_instance_id,
                            "sandbox_generation": preview.sandbox_generation,
                        },
                    },
                },
            ]
        )
    events.append(
        {
            "kind": "deliverable",
            "source": "agent",
            "seq": base + 8,
            "id": delivery_id,
            "path": artifact_path,
            "artifact_kind": artifact_kind,
            "target_id": contract.target_id,
            "delivery_contract": contract.delivery.model_dump(mode="json"),
            "verification_contract_digest": contract.digest,
            "agent_view_id": "view-fixture",
        }
    )
    events.append(
        {
            "kind": "verifier_started",
            "source": "system",
            "seq": base + 9,
            "id": started_id,
            "agent_view_id": "view-fixture",
            "artifact_path": artifact_path,
            "artifact_kind": artifact_kind,
            "target_id": contract.target_id,
            "run_intent_id": intent_id,
            "run_identity": _RUN_ID,
            "delivery_shape": contract.delivery.shape,
            "delivery_entry_reference": contract.delivery.entry_reference,
            "check_id": contract.checks[0].check_id,
            "receipt_kind": contract.checks[0].receipt_kind,
            "issuer_id": contract.checks[0].issuer_id,
            "operation": contract.checks[0].operation,
            "delegated_issuer_ids": sorted(contract.checks[0].delegated_issuer_ids),
            "verification_contract_digest": contract.digest,
            "deliverable_event_id": delivery_id,
            "execution_identity": execution.model_dump(mode="json"),
            "preview_selection": (preview.model_dump(mode="json") if preview is not None else None),
            "workspace_revision": base + 4,
            "workspace_generation": "workspace-1",
            "workspace_epoch": 1,
            "observed_after_seq": base + 8,
        }
    )
    if sealed_output:
        events.extend(
            [
                {
                    "kind": "action",
                    "source": "system",
                    "seq": base + 10,
                    "id": f"evt_verifier_action_{base}",
                    "tool_call": {
                        "tool_name": "verify_native",
                        "call_id": f"call-verifier-{base}",
                        "arguments": {},
                    },
                },
                {
                    "kind": "observation",
                    "source": "environment",
                    "seq": base + 11,
                    "id": output_observation_id,
                    "action_id": f"evt_verifier_action_{base}",
                    "tool_result": {
                        "tool_name": "verify_native",
                        "call_id": f"call-verifier-{base}",
                        "success": True,
                    },
                },
            ]
        )
    verdict_seq = base + 12 if sealed_output else base + 10
    terminal_seq = verdict_seq + 1
    events.extend(
        [
            {
                "kind": "verifier_verdict",
                "source": "system",
                "seq": verdict_seq,
                "id": f"evt_verdict_{base}",
                "artifact_path": artifact_path,
                "artifact_kind": artifact_kind,
                "verified": passed,
                "verdict": "pass" if passed else "fail",
                "target_id": contract.target_id,
                "check_id": contract.checks[0].check_id,
                "receipt_kind": contract.checks[0].receipt_kind,
                "verification_contract_digest": contract.digest,
                "requested_by_event_id": started_id,
                "verification_result": receipt,
            },
            {
                "kind": "status",
                "source": "system",
                "seq": terminal_seq,
                "id": f"evt_terminal_{base}",
                "status": "FINISHED",
                "agent_view_id": "view-fixture",
            },
        ]
    )
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
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    receipt = HostVerificationResult.model_validate(verdict["verification_result"])
    changed = receipt.model_copy(update={**updates, "effect_receipt": None})
    anchored = with_verification_effect_receipt(changed)
    verdict["verification_result"] = anchored.model_dump(mode="json")
    return anchored


def test_current_web_segment_with_exact_receipt_passes() -> None:
    assert _result(_segment()).passed


def test_preview_oracle_normalizes_only_the_sandbox_workspace_root() -> None:
    def identity(serve_dir: str) -> dict[str, Any] | None:
        intent = {"launch_kind": "static", "serve_dir": serve_dir}
        payload = {
            "command": "python3 -m http.server 8000 -d /workspace",
            "exec_dir": "/workspace",
            "intent": intent,
            "name": "site",
            "port": 8000,
        }
        call_id = f"call-{hashlib.sha256(serve_dir.encode()).hexdigest()[:8]}"
        action = {
            "kind": "action",
            "seq": 7,
            "id": f"evt-action-{call_id}",
            "tool_call": {
                "tool_name": "preview_start",
                "call_id": call_id,
                "arguments": {"serve_dir": serve_dir},
            },
        }
        observation = {
            "kind": "observation",
            "seq": 8,
            "id": f"evt-observation-{call_id}",
            "action_id": action["id"],
            "tool_result": {
                "tool_name": "preview_start",
                "call_id": call_id,
                "success": True,
                "structured": {
                    **payload,
                    "status": "running",
                    "url": "http://preview.test",
                    "projection_id": "pv_" + "a" * 32,
                    "sandbox_instance_id": "sandbox-1",
                    "sandbox_generation": 1,
                    "launch_kind": "static",
                    "intent_digest": hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                },
            },
        }
        return _preview_identity_from_pair(action, observation)

    root = identity("/workspace/")
    nested = identity("/workspace/site")

    assert root is not None and root["static_serve_dir"] == "."
    assert nested is not None and nested["static_serve_dir"] == "site"
    assert identity("/workspace/../srv/site") is None
    assert identity("/srv/site") is None
    assert identity("../site") is None
    assert identity("site/../../outside") is None


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


def test_current_appkit_owned_preview_with_exact_receipt_passes() -> None:
    events = _segment()
    _use_appkit_owned_preview(events)

    assert _result(events).passed


def test_appkit_preview_reobservation_retains_same_operational_handoff() -> None:
    events = _segment()
    _use_appkit_owned_preview(events)
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
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = events[-1]
    verdict["seq"] = 12
    terminal["seq"] = 13
    events[events.index(verdict) : events.index(verdict)] = [
        repeated_action,
        repeated_observation,
    ]

    assert _result(events).passed


def test_composed_checks_share_execution_generation_but_retain_distinct_modalities() -> None:
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
def test_appkit_owned_preview_requires_exact_successful_source_pair(corrupt: str) -> None:
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


def test_target_specific_nonweb_policy_needs_no_preview() -> None:
    assert _result(_segment(native=True), native=True).passed


def test_output_producing_nonweb_target_requires_post_start_artifact_seal() -> None:
    assert _result(
        _segment(native=True, sealed_output=True),
        native=True,
    ).passed


def test_output_producing_target_without_artifact_identity_fails() -> None:
    events = _segment(native=True, sealed_output=True)
    _replace_receipt(events, artifact_identity=None)

    assert _result(events, native=True).failed


def test_output_producing_target_with_unadmitted_identity_scheme_fails() -> None:
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


def test_final_continue_segment_is_authoritative() -> None:
    events = [*_segment(base=0, passed=True), *_segment(base=20, passed=False)]
    result = _result(events)
    assert result.failed
    assert result.code == GOVERNED_ADMISSION_BYPASSED


def test_latest_same_segment_failure_overrides_earlier_pass() -> None:
    events = _segment()
    later = dict(events[-2])
    later["seq"] = 11
    later["id"] = "evt_later_fail"
    later["verified"] = False
    later["verdict"] = "fail"
    later["verification_result"] = None
    events[-1]["seq"] = 12
    events.insert(-1, later)
    assert _result(events).failed


def test_mutation_after_observed_authority_rejects_stale_receipt() -> None:
    events = _segment()
    events[-2]["seq"] = 11
    events[-1]["seq"] = 12
    events.insert(
        -2,
        {
            "kind": "action",
            "seq": 10,
            "id": "evt_mutation",
            "tool_call": {"tool_name": "file_write", "call_id": "mut", "arguments": {}},
        },
    )
    assert _result(events).failed


def test_receipt_after_mutation_capable_observation_uses_observation_authority() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = events[-1]
    started["seq"] = 11
    started["workspace_revision"] = 9
    started["observed_after_seq"] = 10
    verdict["seq"] = 12
    terminal["seq"] = 13
    events[events.index(started) : events.index(started)] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 9,
            "id": "evt_capability_shell",
            "tool_call": {
                "tool_name": "shell",
                "call_id": "capability-shell",
                "arguments": {"command": "inspect-artifact"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 10,
            "id": "evt_capability_shell_result",
            "action_id": "evt_capability_shell",
            "tool_result": {
                "tool_name": "shell",
                "call_id": "capability-shell",
                "success": True,
                "action_profile": {"capabilities": ["workspace.mutate"]},
                "effect_receipts": [],
            },
        },
    ]
    _replace_receipt(events, workspace_revision=9, observed_after_seq=10)

    assert _result(events).passed


def test_mutation_after_pass_rejects_stale_receipt() -> None:
    events = _segment()
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_post_verdict_write",
            "tool_call": {
                "tool_name": "file_write",
                "call_id": "post-write",
                "arguments": {"path": "index.html"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 12,
            "id": "evt_post_verdict_write_result",
            "action_id": "evt_post_verdict_write",
            "tool_result": {
                "tool_name": "file_write",
                "call_id": "post-write",
                "success": True,
            },
        },
    ]
    assert _result(events).failed


def test_preview_stop_after_pass_rejects_stale_receipt() -> None:
    events = _segment()
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_post_verdict_stop",
            "tool_call": {
                "tool_name": "preview_stop",
                "call_id": "post-stop",
                "arguments": {"name": "web"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 12,
            "id": "evt_post_verdict_stop_result",
            "action_id": "evt_post_verdict_stop",
            "tool_result": {
                "tool_name": "preview_stop",
                "call_id": "post-stop",
                "success": True,
                "structured": {"stopped": ["web"]},
            },
        },
    ]
    assert _result(events).failed


def test_future_target_mutation_capability_after_pass_rejects_stale_receipt() -> None:
    events = _segment(native=True)
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_device_patch",
            "action_profile": {"capabilities": ["workspace.mutate"]},
            "tool_call": {
                "tool_name": "device_apply_patch",
                "call_id": "device-patch",
                "arguments": {"bundle_id": "dev.fixture"},
            },
        },
        {
            "kind": "observation",
            "source": "environment",
            "seq": 12,
            "id": "evt_device_patch_result",
            "action_id": "evt_device_patch",
            "tool_result": {
                "tool_name": "device_apply_patch",
                "call_id": "device-patch",
                "success": True,
                "action_profile": {"capabilities": ["workspace.mutate"]},
            },
        },
    ]
    assert _result(events, native=True).failed


def test_failed_partial_future_mutation_after_pass_rejects_stale_receipt() -> None:
    events = _segment(native=True)
    events[-1]["seq"] = 13
    events[-1:-1] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 11,
            "id": "evt_partial_device_patch",
            "action_profile": {"capabilities": ["workspace.mutate"]},
            "tool_call": {
                "tool_name": "device_apply_patch",
                "call_id": "partial-device-patch",
                "arguments": {"bundle_id": "dev.fixture"},
            },
        },
        {
            "kind": "agent_error",
            "source": "environment",
            "seq": 12,
            "id": "evt_partial_device_patch_result",
            "action_id": "evt_partial_device_patch",
            "error": "device update failed after applying part of the patch",
            "action_profile": {"capabilities": ["workspace.mutate"]},
            "effect_receipts": [
                {
                    "kind": "mutation",
                    "capability": "workspace.mutate",
                }
            ],
        },
    ]
    assert _result(events, native=True).failed


def test_foreign_preview_pair_rejects_receipt() -> None:
    events = _segment()
    preview_action = next(
        event
        for event in events
        if event.get("kind") == "action"
        and event.get("tool_call", {}).get("tool_name") == "preview_start"
    )
    preview_action["id"] = "evt_foreign_preview"
    assert _result(events).failed


def test_self_anchored_receipt_from_another_conversation_is_rejected() -> None:
    events = _segment()
    _replace_receipt(events, conversation_id="conv_foreign")
    assert _result(events).failed


def test_foreign_agent_view_is_rejected_even_when_receipt_is_self_anchored() -> None:
    events = _segment()
    deliverable = next(event for event in events if event.get("kind") == "deliverable")
    started = next(event for event in events if event.get("kind") == "verifier_started")
    deliverable["agent_view_id"] = "view_foreign"
    started["agent_view_id"] = "view_foreign"
    _replace_receipt(events, agent_view_id="view_foreign")
    assert _result(events).failed


def test_same_intent_handoff_survives_a_later_verifier_view() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = next(event for event in events if event.get("kind") == "status")
    started["seq"] = 10
    started["agent_view_id"] = "view-later"
    verdict["seq"] = 11
    terminal["seq"] = 12
    events.append(
        {
            "kind": "workspace_mutation",
            "source": "system",
            "seq": 9,
            "id": "evt_view_later",
            "operation": "agent.view-admitted",
            "run_intent_id": "evt_intent_0",
            "agent_view_id": "view-later",
        }
    )
    _replace_receipt(events, agent_view_id="view-later")

    assert _result(events).passed


def test_foreign_observed_url_is_rejected_even_when_receipt_is_self_anchored() -> None:
    events = _segment()
    _replace_receipt(events, observed_url="http://127.0.0.1:9999/")
    assert _result(events).failed


def test_zero_workspace_revision_is_rejected_against_durable_start_authority() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    started["workspace_revision"] = 0
    _replace_receipt(events, workspace_revision=0)
    assert _result(events).failed


def test_future_observation_order_is_rejected_against_durable_start_authority() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    started["observed_after_seq"] = 99
    _replace_receipt(events, observed_after_seq=99)
    assert _result(events).failed


def test_native_policy_may_omit_workspace_epoch_when_not_required() -> None:
    events = _segment(native=True)
    started = next(event for event in events if event.get("kind") == "verifier_started")
    started["workspace_epoch"] = None
    _replace_receipt(events, workspace_epoch=None)
    assert _result(events, native=True).passed


def test_recognized_pre_execution_rejection_does_not_stale_receipt() -> None:
    events = _segment()
    started = next(event for event in events if event.get("kind") == "verifier_started")
    verdict = next(event for event in events if event.get("kind") == "verifier_verdict")
    terminal = events[-1]
    started["seq"] = 11
    verdict["seq"] = 12
    terminal["seq"] = 13
    events[events.index(started) : events.index(started)] = [
        {
            "kind": "action",
            "source": "agent",
            "seq": 9,
            "id": "evt_gate_rejected_write",
            "tool_call": {
                "tool_name": "file_write",
                "call_id": "gate-write",
                "arguments": {"path": "index.html"},
            },
        },
        {
            "kind": "agent_error",
            "source": "environment",
            "seq": 10,
            "id": "evt_gate_rejected_write_result",
            "action_id": "evt_gate_rejected_write",
            "error": (
                "<system-reminder>\nREFUSED: `file_write` is not available in "
                "PLANNING mode. No workspace mutation or execution is allowed "
                "before plan approval. Call `submit_plan`.\n</system-reminder>"
            ),
        },
    ]
    assert _result(events).passed


def test_unsuperseded_external_requirement_from_prior_segment_cannot_be_dropped() -> None:
    events = [
        {
            "kind": "message",
            "source": "user",
            "seq": 0,
            "id": "evt_external",
            "message": "The installed application must launch.",
            "verification_requirements": {
                "supersedes_event_id": None,
                "claims": [
                    {
                        "claim_id": "external.launch",
                        "kind": "target_specific",
                        "required": True,
                        "expected": "installed application launches",
                    }
                ],
                "reference_images": [],
            },
        },
        *_segment(),
    ]
    assert _result(events).failed


def test_weakened_contract_claim_floor_fails() -> None:
    events = _segment()
    contract = events[1]["verification_contract"]
    contract["checks"][0]["claims"] = contract["checks"][0]["claims"][:1]
    events[1]["verification_claims"] = events[1]["verification_claims"][:1]
    assert _result(events).failed


def test_relay_and_browser_flags_do_not_implicitly_activate_governance() -> None:
    scenario = {
        "requires_relay_ledger": True,
        "assertions": {"browser_verification": {"required": True}},
    }
    result = GovernedAdmissionOracle().check([], scenario=scenario)[0]
    assert result.skipped


def test_appkit_also_requires_the_common_typed_receipt_oracle() -> None:
    scenario = _scenario()
    scenario["appkit"] = True
    result = GovernedAdmissionOracle().check([], scenario=scenario)[0]
    assert result.failed


def test_governed_scenario_policy_must_be_exact_and_typed() -> None:
    scenario = _scenario()
    scenario["assertions"]["governed_verification"]["required_receipt_kinds"] = []
    result = ContractOracle().check(scenario)[0]
    assert result.failed


@pytest.mark.parametrize(
    "modalities",
    [
        [],
        ["managed_preview", "managed_preview"],
        ["managed_preview", ""],
    ],
)
def test_multi_adapter_policy_requires_a_nonempty_unique_modality_set(
    modalities: list[str],
) -> None:
    scenario = _scenario()
    policy = scenario["assertions"]["governed_verification"]
    del policy["required_execution_modality"]
    policy["required_execution_modalities"] = modalities

    assert ContractOracle().check(scenario)[0].failed


def test_governed_policy_cannot_mix_singular_and_multi_adapter_modalities() -> None:
    scenario = _scenario()
    policy = scenario["assertions"]["governed_verification"]
    policy["required_execution_modalities"] = ["managed_preview"]

    assert ContractOracle().check(scenario)[0].failed


def test_appkit_scenarios_replace_the_web_verification_policy_atomically() -> None:
    scenarios = load_scenarios("harness/build_soak/scenarios_phase4.yaml")
    appkit = [scenario for scenario in scenarios.values() if scenario.get("appkit")]

    assert appkit
    for scenario in appkit:
        policy = scenario["assertions"]["governed_verification"]
        assert policy["required_receipt_kinds"] == [
            "disco.appkit_strict@1",
            "disco.web_functional@1",
        ]
        assert policy["required_claim_kinds"] == {
            "target_specific": 1,
            "artifact_identity": 1,
            "http_ready": 1,
            "rendered_content": 1,
            "console_clean": 1,
            "network_clean": 1,
        }
        assert policy["required_execution_modalities"] == [
            "appkit_strict_runtime",
            "managed_preview",
        ]
        assert (
            ContractOracle()
            .check(
                scenario,
                available_evidence={
                    "events",
                    "product_evidence",
                    "workspace",
                    "preview",
                    "tool_scope",
                },
            )[0]
            .passed
        )
