from __future__ import annotations

import pytest
from disco.agent_server.verify.dispatcher import HostVerifierDispatcher
from disco.core.loop import HostVerificationDeliverable
from disco.core.verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    VerificationCheckContract,
    VerificationClaimKind,
    VerificationDeliveryContract,
)


def _deliverable() -> HostVerificationDeliverable:
    claim = HostVerificationClaim(
        claim_id="native.launch",
        kind=VerificationClaimKind.TARGET_SPECIFIC,
        expected="bundle launches",
        source_authority="target.native@1",
    )
    check = VerificationCheckContract(
        check_id="native",
        receipt_kind="synthetic.native@1",
        issuer_id="synthetic.native_verifier@1",
        operation="host.verify_native",
        accepted_claim_kinds=frozenset({VerificationClaimKind.TARGET_SPECIFIC}),
        claims=(claim,),
    )
    contract = AdmittedVerificationContract(
        target_id="synthetic.native@1",
        verifier_id=check.issuer_id,
        delivery=VerificationDeliveryContract(
            shape="native.bundle",
            entry_kind="bundle_id",
            entry_reference="dev.fixture",
        ),
        preview_modality="simulator",
        checks=(check,),
    )
    return HostVerificationDeliverable(
        conversation_id="conv",
        artifact_path="build/Fixture.app",
        artifact_kind="files",
        required_claims=(claim,),
        verification_contract=contract,
        verification_check=check,
    )


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, deliverable: HostVerificationDeliverable) -> dict:
        self.calls += 1
        return {"passed": True, "verdict": "pass", "path": deliverable.artifact_path}


@pytest.mark.asyncio
async def test_dispatches_only_to_exact_admitted_verifier_key() -> None:
    adapter = _Adapter()
    dispatcher = HostVerifierDispatcher(
        {
            (
                "synthetic.native_verifier@1",
                "synthetic.native@1",
                "host.verify_native",
            ): adapter
        }
    )

    result = await dispatcher.verify(_deliverable())

    assert result["passed"] is True
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_missing_adapter_returns_typed_non_authorizing_receipt() -> None:
    result = await HostVerifierDispatcher({}).verify(_deliverable())

    assert result["passed"] is False
    assert result["verdict"] == "unavailable"
    receipt = result["verification_result"]
    assert receipt["status"] == "unavailable"
    assert receipt["check_id"] == "native"
    assert receipt["receipt_kind"] == "synthetic.native@1"
    assert receipt["effect_receipt"]["passed"] is False
