"""Moved policy contracts collection implementations."""

from __future__ import annotations

from ._shared import (
    ContractOracle,
    GovernedAdmissionOracle,
    load_scenarios,
    pytest,
)
from .helpers_01 import (
    _result,
    _scenario,
    _segment,
)


def _impl_test_unsuperseded_external_requirement_from_prior_segment_cannot_be_dropped() -> None:
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


def _impl_test_weakened_contract_claim_floor_fails() -> None:
    events = _segment()
    contract = events[1]["verification_contract"]
    contract["checks"][0]["claims"] = contract["checks"][0]["claims"][:1]
    events[1]["verification_claims"] = events[1]["verification_claims"][:1]
    assert _result(events).failed


def _impl_test_relay_and_browser_flags_do_not_implicitly_activate_governance() -> None:
    scenario = {
        "requires_relay_ledger": True,
        "assertions": {"browser_verification": {"required": True}},
    }
    result = GovernedAdmissionOracle().check([], scenario=scenario)[0]
    assert result.skipped


def _impl_test_appkit_also_requires_the_common_typed_receipt_oracle() -> None:
    scenario = _scenario()
    scenario["appkit"] = True
    result = GovernedAdmissionOracle().check([], scenario=scenario)[0]
    assert result.failed


def _impl_test_governed_scenario_policy_must_be_exact_and_typed() -> None:
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
def _impl_test_multi_adapter_policy_requires_a_nonempty_unique_modality_set(
    modalities: list[str],
) -> None:
    scenario = _scenario()
    policy = scenario["assertions"]["governed_verification"]
    del policy["required_execution_modality"]
    policy["required_execution_modalities"] = modalities

    assert ContractOracle().check(scenario)[0].failed


def _impl_test_governed_policy_cannot_mix_singular_and_multi_adapter_modalities() -> None:
    scenario = _scenario()
    policy = scenario["assertions"]["governed_verification"]
    policy["required_execution_modalities"] = ["managed_preview"]

    assert ContractOracle().check(scenario)[0].failed


def _impl_test_appkit_scenarios_replace_the_web_verification_policy_atomically() -> None:
    scenarios = load_scenarios("development/harness/build_soak/scenarios_phase4.yaml")
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
