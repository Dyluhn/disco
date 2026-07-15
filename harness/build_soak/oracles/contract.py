"""ContractOracle (guidelines §10 / §16 — the scenario-contract layer).

Runs after harness validity. It validates that the scenario's DECLARED
requirements are well-formed and that the evidence needed to adjudicate them is
present — i.e. that the run can be judged against the contract the scenario
states. It does NOT yet judge whether the product satisfied the contract (that is
the event-chain / revision / output-truth oracles); it guards against silently
"passing" a run whose contract we could never actually check.

Examples of an unsatisfiable contract → INVALID_RUN (SCENARIO_CONTRACT_UNSATISFIABLE):
  * the scenario asserts workspace file truth but no workspace manifest was captured;
  * the scenario asserts preview truth but no preview health/screenshot was captured;
  * the scenario declares followups requiring plan revision but no expected final
    revision (the revision oracle would have nothing to compare against).

The scenario is a plain machine-readable dict (guidelines §14) — never interpreted
by an LLM.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "ContractOracle"


class ContractOracle:
    def check(
        self,
        scenario: dict[str, Any] | None,
        *,
        available_evidence: set[str] | None = None,
    ) -> list[OracleResult]:
        """`available_evidence` names the evidence categories the run actually
        captured (e.g. {"events", "workspace", "preview", "tool_scope"}). When
        None, only "events" is assumed present (the deterministic, no-live-spend
        slice always has the event log)."""
        if not scenario:
            return [skipping(_ORACLE, reason="no scenario contract supplied")]
        present = available_evidence if available_evidence is not None else {"events"}
        assertions = scenario.get("assertions") or {}

        missing: list[str] = []

        # workspace truth requires a workspace manifest.
        workspace = assertions.get("workspace") or {}
        if workspace.get("files") and "workspace" not in present:
            missing.append("workspace_manifest (scenario asserts workspace.files)")

        # preview truth requires preview health/screenshot evidence.
        preview = assertions.get("preview") or {}
        if preview.get("required") and "preview" not in present:
            missing.append("preview_evidence (scenario asserts preview.required)")

        # tool-scope truth (§11.7 WRITE_TOOL_ALLOWED_IN_PLANNING — a disallowed tool
        # must not be CALLABLE, not merely un-advertised) needs the runner-captured
        # per-turn tool scope. A scenario that asserts tool_scope therefore REQUIRES
        # that evidence; if it is missing the run cannot be adjudicated against the
        # contract and must FAIL-CLOSED to INVALID_RUN — never silently PASS via a
        # SKIP. (The event-only WRITE_TOOL_ATTEMPTED_IN_PLANNING check is separate
        # and still runs.)
        tool_scope_assert = assertions.get("tool_scope") or {}
        if tool_scope_assert:
            planning_disallows = tool_scope_assert.get("planning_disallows")
            if (
                not isinstance(planning_disallows, list)
                or not planning_disallows
                or not all(isinstance(name, str) and name for name in planning_disallows)
                or len(set(planning_disallows)) != len(planning_disallows)
            ):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link=("scenario_contract -> tool_scope.planning_disallows"),
                        facts={
                            "reason": (
                                "assertions.tool_scope.planning_disallows must be a "
                                "non-empty unique list of tool names"
                            )
                        },
                    )
                ]
        if tool_scope_assert and "tool_scope" not in present:
            missing.append("tool_scope_capture (scenario asserts assertions.tool_scope)")

        # H191: validate the opt-in shape here.  Whether the product actually ran
        # a passing verifier is adjudicated later as a PRODUCT result; only missing
        # bytes for an observation that did run are a harness-validity INVALID.
        browser_verification = assertions.get("browser_verification")
        if browser_verification is not None:
            if (
                not isinstance(browser_verification, dict)
                or set(browser_verification) != {"required"}
                or (browser_verification.get("required") is not True)
            ):
                return [
                    failing(
                        _ORACLE,
                        fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                        first_broken_link="scenario_contract -> browser_verification.required",
                        facts={
                            "reason": (
                                "assertions.browser_verification must contain only required: true"
                            )
                        },
                    )
                ]
        if missing:
            return [
                failing(
                    _ORACLE,
                    fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                    first_broken_link="scenario_contract -> required_evidence",
                    facts={"missing_evidence": missing},
                )
            ]

        # Revision contract well-formedness: followups requiring a revision need an
        # expected final revision to compare against.
        followups = scenario.get("followups") or []
        revisions = assertions.get("revisions") or {}
        requires_rev = any(f.get("requires_plan_revision") for f in followups)
        if requires_rev and "expected_final_plan_revision" not in revisions:
            return [
                failing(
                    _ORACLE,
                    fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                    first_broken_link="scenario_contract -> revisions.expected_final_plan_revision",
                    facts={
                        "reason": (
                            "followups require_plan_revision but no "
                            "assertions.revisions.expected_final_plan_revision declared"
                        )
                    },
                )
            ]

        return [
            passing(
                _ORACLE,
                facts={
                    "scenario_id": scenario.get("id"),
                    "followup_count": len(followups),
                    "evidence_present": sorted(present),
                },
            )
        ]
