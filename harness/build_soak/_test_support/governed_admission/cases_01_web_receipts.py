"""Moved web receipts collection implementations."""

from __future__ import annotations

from ._shared import (
    Any,
    HostVerificationObservedFact,
    VerificationClaimKind,
    VerificationEvidenceModality,
    _preview_identity_from_pair,
    hashlib,
    json,
)
from .helpers_01 import (
    _replace_receipt,
    _result,
    _segment,
)


def _impl_test_current_web_segment_with_exact_receipt_passes() -> None:
    result = _result(_segment())

    assert result.passed
    claims = result.facts["verified_claim_results"]
    assert {claim["kind"] for claim in claims} == {
        "artifact_identity",
        "console_clean",
        "http_ready",
        "network_clean",
        "rendered_content",
    }
    assert result.facts["verified_artifact_paths"] == ["index.html"]


def _impl_test_current_integrity_bound_observed_fact_is_projected() -> None:
    events = _segment()
    _replace_receipt(
        events,
        observed_facts=(
            HostVerificationObservedFact(
                fact_id="web.observed.visible_text",
                kind=VerificationClaimKind.VISIBLE_TEXT,
                value="Node Paused 403113",
                verifier_id="disco.host_web_verifier@1",
                capability_basis="configured target-specific host verifier",
                evidence_modalities=(VerificationEvidenceModality.DOM_ACCESSIBILITY,),
            ),
        ),
    )

    result = _result(events)

    assert result.passed
    assert result.facts["verified_observed_facts"][0]["value"] == "Node Paused 403113"


def _impl_test_target_neutral_native_entry_path_is_projected_after_full_adjudication() -> None:
    result = _result(_segment(native=True), native=True)

    assert result.passed
    assert result.facts["verified_artifact_paths"] == ["build/Fixture.app"]


def _impl_test_observed_fact_from_unadmitted_issuer_is_rejected() -> None:
    events = _segment()
    _replace_receipt(
        events,
        observed_facts=(
            HostVerificationObservedFact(
                fact_id="web.observed.visible_text",
                kind=VerificationClaimKind.VISIBLE_TEXT,
                value="forged",
                verifier_id="model_role.unadmitted@1",
                capability_basis="model prose",
                evidence_modalities=(VerificationEvidenceModality.DOM_ACCESSIBILITY,),
            ),
        ),
    )

    assert _result(events).failed


def _impl_test_host_revision_seal_is_not_a_completed_work_terminal() -> None:
    events = _segment()
    terminal_seq = max(event["seq"] for event in events)
    events.extend(
        [
            {
                "kind": "status",
                "source": "system",
                "seq": terminal_seq + 1,
                "id": "evt_restore_seal",
                "status": "FINISHED",
                "detail": "host_revision:version.restore",
            },
            {
                "kind": "status",
                "source": "system",
                "seq": terminal_seq + 2,
                "id": "evt_failed_recovery",
                "status": "STUCK",
                "detail": "verify_no_progress:fixture",
            },
        ]
    )

    # The governed oracle audits completed work. The later restore seal cannot
    # manufacture an empty completed-work segment and mask the recovery failure
    # as a missing-admission P0; output truth owns the STUCK adjudication.
    assert _result(events).passed


def _impl_test_preview_oracle_normalizes_only_the_sandbox_workspace_root() -> None:
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
