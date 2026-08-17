"""The ordered oracle fold — first-broken-link wins (guidelines §16).

Classification is a pure function of frozen evidence. The pipeline runs the
oracle layers in the §16 order; the FIRST broken link wins. This module owns
the fold itself, keeping the orchestrator thin and the per-oracle evaluators
independent.
"""

from __future__ import annotations

from typing import Any

from .. import failure_codes as fc
from ..events import NormalizationError, normalize_events
from ..oracles import (
    BROWSER_EVIDENCE_ORACLES,
    TARGETED_EDIT_ORACLES,
    ContextPressureOracle,
    ContractOracle,
    EventChainOracle,
    GovernedAdmissionOracle,
    HarnessValidityOracle,
    OutputTruthOracle,
    ProviderLedgerOracle,
    RevisionOracle,
    ScenarioBrowserVerificationOracle,
    ScenarioLifecycleOracle,
    ThrashOracle,
    ToolScopeOracle,
)
from ..oracles.schema import OracleResult
from ._browser_proof import successful_browser_verification_paths
from ._tool_scope_proof import tool_scope_from_inspect

# Default "likely files" for the most common Build chain breaks (§16 example).
# Advisory only — surfaced to the repair loop, never adjudicated on.
_LIKELY_FILES: dict[str, list[str]] = {
    fc.NO_REPLAN_AFTER_REVISION: [
        "current/packages/core/src/disco/core/loop/plans.py",
        "current/packages/core/src/disco/core/loop/engine.py",
        "current/packages/agent-server/src/disco/agent_server/control_ops.py",
    ],
    fc.WRITE_TOOL_ATTEMPTED_IN_PLANNING: [
        "current/packages/core/src/disco/core/loop/engine.py",
        "current/packages/core/src/disco/core/loop/driver.py",
    ],
    fc.WRITE_TOOL_ALLOWED_IN_PLANNING: [
        "current/packages/core/src/disco/core/loop/engine.py",
        "current/packages/core/src/disco/core/loop/driver.py",
    ],
    fc.APPROVE_PLAN_NO_EXECUTION: [
        "current/packages/core/src/disco/core/loop/finish.py",
        "current/packages/core/src/disco/core/loop/engine.py",
    ],
    fc.ACTION_NO_OBSERVATION: [
        "current/packages/core/src/disco/core/loop/observe.py",
    ],
}


def _first_fail(results: list[OracleResult]) -> OracleResult | None:
    for r in results:
        if r.failed:
            return r
    return None


def _resolve_tool_scope(
    scenario: dict[str, Any] | None,
    tool_scope: list[dict[str, Any]] | None,
    inspect_trace: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """Resolve the tool-scope evidence, extracting from inspect if asserted."""
    tool_scope_asserted = bool(((scenario or {}).get("assertions") or {}).get("tool_scope"))
    if tool_scope is None and tool_scope_asserted:
        return tool_scope_from_inspect(inspect_trace)
    return tool_scope


def _normalize_event_log(
    events_raw: list[Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Normalize the event log; a parse failure is a harness-validity defect."""
    try:
        events = normalize_events(events_raw)
        return events, True
    except (NormalizationError, ValueError, TypeError):
        return [], False


def _derive_present_evidence(
    scenario: dict[str, Any] | None,
    available_evidence: set[str] | None,
    workspace_manifest: dict[str, Any] | None,
    preview: dict[str, Any] | None,
    tool_scope: list[dict[str, Any]] | None,
    product_evidence: dict[str, Any] | None,
) -> set[str]:
    """The evidence categories actually captured for this run."""
    present: set[str] = set(available_evidence or set())
    present.discard("browser_verification")  # H191 proof is oracle-derived
    present.add("events")
    if workspace_manifest is not None:
        present.add("workspace")
    if preview is not None and preview.get("source") == "isolated_path_capability":
        present.add("preview")
    if tool_scope is not None:
        present.add("tool_scope")
    if product_evidence is not None:
        present.add("product_evidence")
    return present


def _run_governed_admission(
    events: list[dict[str, Any]],
    scenario: dict[str, Any] | None,
    conversation_id: str,
) -> tuple[
    list[OracleResult], list[dict[str, Any]] | None, list[dict[str, Any]] | None, list[str] | None
]:
    """Run the governed-admission oracle and extract verified claim facts."""
    governed_results = GovernedAdmissionOracle().check(
        events,
        scenario=scenario,
        conversation_id=conversation_id,
    )
    verified_claim_results: list[dict[str, Any]] | None = None
    verified_observed_facts: list[dict[str, Any]] | None = None
    verified_artifact_paths: list[str] | None = None
    for result in governed_results:
        claims = result.facts.get("verified_claim_results")
        if result.passed and isinstance(claims, list):
            verified_claim_results = [dict(claim) for claim in claims if isinstance(claim, dict)]
            facts = result.facts.get("verified_observed_facts")
            verified_observed_facts = (
                [dict(fact) for fact in facts if isinstance(fact, dict)]
                if isinstance(facts, list)
                else []
            )
            artifact_paths = result.facts.get("verified_artifact_paths")
            verified_artifact_paths = (
                [path for path in artifact_paths if isinstance(path, str)]
                if isinstance(artifact_paths, list)
                else []
            )
            break
    return (
        governed_results,
        verified_claim_results,
        verified_observed_facts,
        verified_artifact_paths,
    )


def _run_browser_and_edit_oracles(
    product_evidence: dict[str, Any] | None,
) -> tuple[list[OracleResult], OracleResult | None]:
    """Run browser product-harness and targeted-edit oracles (SKIP-safe)."""
    results: list[OracleResult] = []
    first_fail: OracleResult | None = None
    for _oracle_cls in BROWSER_EVIDENCE_ORACLES:
        results += _oracle_cls().check(product_evidence=product_evidence)
        first_fail = _first_fail(results)
        if first_fail is not None:
            break
    if first_fail is None:
        for _oracle_cls in TARGETED_EDIT_ORACLES:
            results += _oracle_cls().check(product_evidence=product_evidence)
            first_fail = _first_fail(results)
            if first_fail is not None:
                break
    return results, first_fail


def _run_early_oracles(
    events: list[dict[str, Any]],
    *,
    scenario: dict[str, Any] | None,
    conversation_id: str,
    evidence_intact: bool,
    parse_ok: bool,
    present: set[str],
    tool_scope: list[dict[str, Any]] | None,
    inspect_trace: dict[str, Any] | None,
) -> tuple[
    list[OracleResult],
    OracleResult | None,
    list[dict[str, Any]] | None,
    list[dict[str, Any]] | None,
    list[str] | None,
]:
    """Run the early ordered oracle stages.

    Returns results, first failure, and the governed verified claim, fact, and
    artifact-path evidence used by later stages.
    """
    results: list[OracleResult] = []
    results += HarnessValidityOracle().check(
        events, evidence_intact=evidence_intact, parse_ok=parse_ok
    )
    first_fail = _first_fail(results)

    if first_fail is None:
        results += ContractOracle().check(scenario, available_evidence=present)
        first_fail = _first_fail(results)

    if first_fail is None:
        results += EventChainOracle().check(events, scenario=scenario)
        first_fail = _first_fail(results)

    if first_fail is None:
        results += ThrashOracle().check(events, scenario=scenario, inspect_trace=inspect_trace)
        first_fail = _first_fail(results)

    verified_claim_results: list[dict[str, Any]] | None = None
    verified_observed_facts: list[dict[str, Any]] | None = None
    verified_artifact_paths: list[str] | None = None
    if first_fail is None:
        governed_results, vcr, vof, vap = _run_governed_admission(events, scenario, conversation_id)
        results += governed_results
        verified_claim_results = vcr
        verified_observed_facts = vof
        verified_artifact_paths = vap
        first_fail = _first_fail(results)

    if first_fail is None:
        results += ToolScopeOracle().check(events, scenario=scenario, tool_scope=tool_scope)
        first_fail = _first_fail(results)

    return (
        results,
        first_fail,
        verified_claim_results,
        verified_observed_facts,
        verified_artifact_paths,
    )


def _run_late_oracles(
    events: list[dict[str, Any]],
    *,
    scenario: dict[str, Any] | None,
    conversation_id: str,
    workspace_manifest: dict[str, Any] | None,
    preview: dict[str, Any] | None,
    revision_meta: dict[str, Any] | None,
    provider_ledger: list[dict[str, Any]] | None,
    product_evidence: dict[str, Any] | None,
    verification_paths: set[str],
    captured_browser_paths: set[str],
    verified_claim_results: list[dict[str, Any]] | None,
    verified_observed_facts: list[dict[str, Any]] | None,
    verified_artifact_paths: list[str] | None,
) -> tuple[list[OracleResult], OracleResult | None]:
    """Run oracles 6–12: revision, lifecycle, context, output truth, provider, browser, edit."""
    results: list[OracleResult] = []
    results += RevisionOracle().check(events, scenario=scenario, meta=revision_meta)
    first_fail = _first_fail(results)

    if first_fail is None:
        results += ScenarioLifecycleOracle().check(
            events, scenario=scenario, product_evidence=product_evidence
        )
        first_fail = _first_fail(results)

    if first_fail is None:
        results += ContextPressureOracle().check(events, scenario=scenario)
        first_fail = _first_fail(results)

    if first_fail is None:
        results += OutputTruthOracle().check(
            events,
            scenario=scenario,
            workspace_manifest=workspace_manifest,
            preview=preview,
            verified_claim_results=verified_claim_results,
            verified_observed_facts=verified_observed_facts,
            verified_artifact_paths=verified_artifact_paths,
        )
        first_fail = _first_fail(results)

    if first_fail is None:
        results += ProviderLedgerOracle().check(
            events,
            scenario=scenario,
            provider_ledger=provider_ledger,
            conversation_id=conversation_id,
        )
        first_fail = _first_fail(results)

    if first_fail is None:
        results += ScenarioBrowserVerificationOracle().check(
            scenario=scenario,
            verification_paths=verification_paths,
            browser_evidence_paths=captured_browser_paths,
        )
        first_fail = _first_fail(results)

    if first_fail is None:
        browser_edit_results, bf = _run_browser_and_edit_oracles(product_evidence)
        results += browser_edit_results
        first_fail = bf

    return results, first_fail


def run_oracle_pipeline(
    events_raw: list[Any],
    *,
    scenario: dict[str, Any] | None = None,
    conversation_id: str = "",
    workspace_manifest: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
    tool_scope: list[dict[str, Any]] | None = None,
    autonomous: bool | None = None,
    revision_meta: dict[str, Any] | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
    inspect_trace: dict[str, Any] | None = None,
    product_evidence: dict[str, Any] | None = None,
    browser_evidence_paths: set[str] | None = None,
    available_evidence: set[str] | None = None,
    evidence_intact: bool = True,
) -> tuple[list[OracleResult], OracleResult | None, dict[str, Any]]:
    """Run the ordered oracle fold and return (results, first_fail, context).

    The context dict carries the derived evidence set and verified-claim facts
    that the classifier needs to build the final classification dict.
    """
    if autonomous is not None:
        scenario = {**(scenario or {}), "autonomous": autonomous}

    tool_scope = _resolve_tool_scope(scenario, tool_scope, inspect_trace)
    events, parse_ok = _normalize_event_log(events_raw)
    present = _derive_present_evidence(
        scenario, available_evidence, workspace_manifest, preview, tool_scope, product_evidence
    )
    verification_paths = successful_browser_verification_paths(events)
    captured_browser_paths = set(browser_evidence_paths or set())

    early_results, first_fail, vcr, vof, vap = _run_early_oracles(
        events,
        scenario=scenario,
        conversation_id=conversation_id,
        evidence_intact=evidence_intact,
        parse_ok=parse_ok,
        present=present,
        tool_scope=tool_scope,
        inspect_trace=inspect_trace,
    )
    results = early_results

    if first_fail is None:
        late_results, lf = _run_late_oracles(
            events,
            scenario=scenario,
            conversation_id=conversation_id,
            workspace_manifest=workspace_manifest,
            preview=preview,
            revision_meta=revision_meta,
            provider_ledger=provider_ledger,
            product_evidence=product_evidence,
            verification_paths=verification_paths,
            captured_browser_paths=captured_browser_paths,
            verified_claim_results=vcr,
            verified_observed_facts=vof,
            verified_artifact_paths=vap,
        )
        results += late_results
        first_fail = lf

    context: dict[str, Any] = {
        "present": present,
        "verification_paths": verification_paths,
    }
    return results, first_fail, context


def likely_files_for_code(code: str | None) -> list[str] | None:
    """Advisory likely-files list for a failure code, or None."""
    if code and code in _LIKELY_FILES:
        return list(_LIKELY_FILES[code])
    return None
