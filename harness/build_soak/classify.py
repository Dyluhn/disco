"""The deterministic classifier (guidelines §13, §16).

Runs the oracle layers in the §16 order; the FIRST broken link wins. The classifier
is a pure function of durable evidence — it MUST NOT call an LLM, and it ignores any
agent/Claude/Codex prose ("agent_comments_ignored_for_adjudication": true, §13).

Outcome mapping:
  * a HarnessValidity / Contract failure (an evidence/contract defect, not a product
    defect) -> INVALID_RUN (§8): the harness couldn't adjudicate; never a pass.
  * any product-oracle failure -> FAIL with the code's severity (§12).
  * no failure -> PASS.

It also implements the §17 no-fluke replay policy as a pure function: a run that
failed once then passed on exact replay is recorded as FAIL /
INTERMITTENT_<original_code> (same severity), never PASS — "fluke" has no
operational meaning.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import failure_codes as fc
from .events import NormalizationError, normalize_events
from .evidence import EvidenceManifest, load_manifest, verify_evidence_unchanged
from .oracles import (
    BROWSER_EVIDENCE_ORACLES,
    TARGETED_EDIT_ORACLES,
    ContractOracle,
    EventChainOracle,
    HarnessValidityOracle,
    OutputTruthOracle,
    ProviderLedgerOracle,
    RevisionOracle,
    ThrashOracle,
    ToolScopeOracle,
)
from .oracles.schema import OracleResult

CLASSIFICATION_NAME = "classification.json"

# Default "likely files" for the most common Build chain breaks (§16 example).
# Advisory only — surfaced to the repair loop, never adjudicated on.
_LIKELY_FILES: dict[str, list[str]] = {
    fc.NO_REPLAN_AFTER_REVISION: [
        "packages/core/src/disco/core/loop/plans.py",
        "packages/core/src/disco/core/loop/engine.py",
        "packages/agent-server/src/disco/agent_server/control_ops.py",
    ],
    fc.WRITE_TOOL_ATTEMPTED_IN_PLANNING: [
        "packages/core/src/disco/core/loop/engine.py",  # _gate_planning_mode
        "packages/core/src/disco/core/loop/driver.py",
    ],
    fc.WRITE_TOOL_ALLOWED_IN_PLANNING: [
        "packages/core/src/disco/core/loop/engine.py",
        "packages/core/src/disco/core/loop/driver.py",
    ],
    fc.APPROVE_PLAN_NO_EXECUTION: [
        "packages/core/src/disco/core/loop/finish.py",
        "packages/core/src/disco/core/loop/engine.py",
    ],
    fc.ACTION_NO_OBSERVATION: [
        "packages/core/src/disco/core/loop/observe.py",
    ],
}


def classify(
    events_raw: list[Any],
    *,
    scenario: dict[str, Any] | None = None,
    run_id: str = "",
    conversation_id: str = "",
    commit: str = "",
    seed: int | None = None,
    evidence_intact: bool = True,
    available_evidence: set[str] | None = None,
    workspace_manifest: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
    tool_scope: list[dict[str, Any]] | None = None,
    autonomous: bool | None = None,
    revision_meta: dict[str, Any] | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
    inspect_trace: dict[str, Any] | None = None,
    product_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify one run from its raw event log (full-event dicts OR DB rows) plus
    optional captured evidence. Returns the §13 classification dict.

    `autonomous`, when given (e.g. from the run manifest), overrides the scenario's
    autonomy: an autonomous build legitimately skips the AWAITING_PLAN_APPROVAL gate
    (it auto-approves inline), so the event-chain approval-ordering check relaxes the
    awaiting link for it."""
    # An explicit autonomous flag (manifest) overrides the scenario declaration.
    if autonomous is not None:
        scenario = {**(scenario or {}), "autonomous": autonomous}

    # Normalize the event log; a parse failure is a harness-validity defect.
    parse_ok = True
    try:
        events = normalize_events(events_raw)
    except (NormalizationError, ValueError, TypeError):
        events, parse_ok = [], False

    # The evidence categories actually captured for this run — derived from the
    # supplied kwargs so the ContractOracle judges what we really have (a caller
    # may also pass an explicit set, which we union in).
    present: set[str] = set(available_evidence or set())
    present.add("events")
    if workspace_manifest is not None:
        present.add("workspace")
    if preview is not None:
        present.add("preview")
    if tool_scope is not None:
        present.add("tool_scope")

    results: list[OracleResult] = []

    # 1. harness validity (evidence integrity + parse + presence).
    results += HarnessValidityOracle().check(
        events, evidence_intact=evidence_intact, parse_ok=parse_ok
    )
    first_fail = _first_fail(results)
    if first_fail is None:
        # 2. scenario contract.
        results += ContractOracle().check(scenario, available_evidence=present)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 3. event chain.
        results += EventChainOracle().check(events, scenario=scenario)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 4. tool scope.
        results += ToolScopeOracle().check(events, scenario=scenario, tool_scope=tool_scope)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 5. model/tool thrash. Cost valves may eventually stop or recover a
        # repeated bad call; the soak still treats that interaction as broken.
        results += ThrashOracle().check(
            events, scenario=scenario, inspect_trace=inspect_trace
        )
        first_fail = _first_fail(results)
    if first_fail is None:
        # 6. revision.
        results += RevisionOracle().check(events, scenario=scenario, meta=revision_meta)
        first_fail = _first_fail(results)
    if first_fail is None:
        # 7. output truth.
        results += OutputTruthOracle().check(
            events, scenario=scenario, workspace_manifest=workspace_manifest, preview=preview
        )
        first_fail = _first_fail(results)
    if first_fail is None:
        # 8. provider ledger (HARN-1a) — MiniMax-only / no-OpenRouter / zero-calls-after-terminal.
        results += ProviderLedgerOracle().check(
            events,
            scenario=scenario,
            provider_ledger=provider_ledger,
            conversation_id=conversation_id,
        )
        first_fail = _first_fail(results)
    if first_fail is None:
        # 9. browser product-harness oracles (HARN-2). Each SKIPs without its evidence
        # slice, so a headless run (product_evidence is None) is unaffected; a product-
        # harness run enforces the real UI path.
        for _oracle_cls in BROWSER_EVIDENCE_ORACLES:
            results += _oracle_cls().check(product_evidence=product_evidence)
            first_fail = _first_fail(results)
            if first_fail is not None:
                break
    if first_fail is None:
        # 10. targeted/manual-edit oracles (P8D). SKIP-safe: each SKIPs without its
        # product_evidence slice, so non-edit runs are unaffected; an edit-harness run
        # enforces targeted-edit + manual-preservation discipline. Producer = P1B-LIVE.
        for _oracle_cls in TARGETED_EDIT_ORACLES:
            results += _oracle_cls().check(product_evidence=product_evidence)
            first_fail = _first_fail(results)
            if first_fail is not None:
                break

    required_evidence_present = not any(
        r.failed and r.code in fc.HARNESS_VALIDITY_CODES for r in results
    )

    if first_fail is None:
        status, severity, code, broken, facts = fc.PASS, fc.NONE, None, None, {}
    elif first_fail.code in fc.HARNESS_VALIDITY_CODES:
        status, severity = fc.INVALID_RUN, fc.NONE
        code, broken, facts = first_fail.code, first_fail.first_broken_link, dict(first_fail.facts)
    else:
        status = fc.FAIL
        code = first_fail.code or fc.UNKNOWN_FAILURE
        severity = fc.severity_for(code)
        broken, facts = first_fail.first_broken_link, dict(first_fail.facts)

    classification: dict[str, Any] = {
        "status": status,
        "severity": severity,
        "code": code,
        "first_broken_link": broken,
        "scenario_id": (scenario or {}).get("id") if scenario else None,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "commit": commit,
        "seed": seed,
        "facts": facts,
        "oracle_results": [r.to_dict() for r in results],
        "required_evidence_present": required_evidence_present,
        "replay": {"attempted": False, "result": "not_attempted", "run_id": None},
        "accepted_by": "oracle",
        "agent_comments_ignored_for_adjudication": True,
    }
    if code and code in _LIKELY_FILES:
        classification["likely_files"] = list(_LIKELY_FILES[code])
    return classification


def _first_fail(results: list[OracleResult]) -> OracleResult | None:
    for r in results:
        if r.failed:
            return r
    return None


def classify_run_folder(
    folder: str | Path, *, scenario: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Classify a frozen run folder: load the manifest, verify evidence integrity
    (a mismatch -> INVALID_RUN), read the event log, classify, and write
    classification.json into the folder. Returns the classification dict."""
    base = Path(folder)
    manifest: EvidenceManifest | None = None
    evidence_intact = True
    try:
        manifest = load_manifest(base)
        integrity = verify_evidence_unchanged(base, manifest)
        evidence_intact = integrity.intact
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        manifest = None

    events_raw: list[Any] = []
    events_rel = (manifest.evidence_files.get("events") if manifest else None) or "events.jsonl"
    events_path = base / events_rel
    if not events_path.is_file():
        # try the conventional conversation-scoped path
        for candidate in base.rglob("events.jsonl"):
            events_path = candidate
            break
    if events_path.is_file():
        events_raw = _read_jsonl(events_path)
    folder_conversation_id = ""
    if events_path.name == "events.jsonl" and events_path.parent.parent.name == "conversations":
        folder_conversation_id = events_path.parent.name

    # HARN-1a: the provider-call ledger (MiniMax-only / no-OpenRouter enforcement).
    # Read it if present anywhere in the run folder; absent → the oracle SKIPs (or
    # fails closed if the scenario requires it). A malformed line is NOT silently
    # dropped (that would under-report a forbidden call) nor crashes classification —
    # it becomes a hostless record the ProviderLedgerOracle fails on as an evidence gap.
    provider_ledger: list[dict[str, Any]] | None = None
    ledger_path = base / "provider-call-ledger.jsonl"
    if not ledger_path.is_file():
        ledger_path = next(iter(base.rglob("provider-call-ledger.jsonl")), ledger_path)
    if ledger_path.is_file():
        provider_ledger = _read_ledger(ledger_path)

    inspect_trace: dict[str, Any] | None = None
    trace_path = base / "inspect-trace.json"
    if not trace_path.is_file():
        trace_path = next(iter(base.rglob("inspect-trace.json")), trace_path)
    if trace_path.is_file():
        try:
            loaded_trace = json.loads(trace_path.read_text(encoding="utf-8"))
            inspect_trace = loaded_trace if isinstance(loaded_trace, dict) else None
        except (json.JSONDecodeError, ValueError):
            inspect_trace = None

    # HARN-2: the browser product-harness evidence dossier (a single JSON object).
    # Absent → the browser oracles SKIP (headless run unaffected).
    product_evidence: dict[str, Any] | None = None
    pe_path = base / "product-evidence.json"
    if not pe_path.is_file():
        pe_path = next(iter(base.rglob("product-evidence.json")), pe_path)
    if pe_path.is_file():
        try:
            loaded = json.loads(pe_path.read_text(encoding="utf-8"))
            product_evidence = loaded if isinstance(loaded, dict) else None
        except (json.JSONDecodeError, ValueError):
            product_evidence = None

    classification = classify(
        events_raw,
        scenario=scenario,
        run_id=manifest.run_id if manifest else base.name,
        conversation_id=folder_conversation_id,
        commit=manifest.repo_commit if manifest else "",
        seed=manifest.seed if manifest else None,
        evidence_intact=evidence_intact,
        autonomous=manifest.autonomous if manifest else None,
        provider_ledger=provider_ledger,
        inspect_trace=inspect_trace,
        product_evidence=product_evidence,
    )
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(classification, indent=2, sort_keys=True), encoding="utf-8"
    )
    return classification


def _read_jsonl(path: str | Path) -> list[Any]:
    out: list[Any] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _read_ledger(path: str | Path) -> list[dict[str, Any]]:
    """Tolerant provider-call-ledger reader: never crashes on a malformed line and
    never silently drops one. A non-JSON / non-dict line becomes a hostless
    ``{"__malformed__": ...}`` record so the ProviderLedgerOracle surfaces it as an
    evidence gap (rather than under-reporting a possibly-forbidden call)."""
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            out.append({"__malformed__": line[:160]})
            continue
        out.append(obj if isinstance(obj, dict) else {"__malformed__": str(obj)[:160]})
    return out


# ---- §17 no-fluke replay policy (pure function) -----------------------------


def intermittent_classification(
    original: dict[str, Any],
    *,
    replay_status: str,
    replay_code: str | None = None,
    replay_run_id: str | None = None,
) -> dict[str, Any]:
    """Apply the no-fluke replay policy (§17) to an originally-FAILED run.

    A failed run that PASSES on exact replay is NOT a pass — it is an intermittent
    failure: status stays FAIL, code becomes INTERMITTENT_<original_code> with the
    SAME severity. A replay that fails the same way is a deterministic failure
    (unchanged). The word "fluke" never yields PASS.

    Returns a NEW classification dict; the input is not mutated."""
    if original.get("status") != fc.FAIL:
        raise ValueError("no-fluke policy only applies to an originally-FAILED run")
    original_code = str(original.get("code"))
    updated = dict(original)

    if replay_status == fc.PASS:
        intermittent_code = fc.INTERMITTENT_PREFIX + original_code
        updated["status"] = fc.FAIL
        updated["code"] = intermittent_code
        updated["severity"] = fc.severity_for(intermittent_code)
        updated["replay"] = {
            "attempted": True,
            "result": "passed",
            "run_id": replay_run_id,
        }
        return updated

    if replay_status == fc.FAIL and replay_code == original_code:
        updated["replay"] = {"attempted": True, "result": "same_failure", "run_id": replay_run_id}
        return updated

    if replay_status == fc.FAIL:
        updated["replay"] = {
            "attempted": True,
            "result": "different_failure",
            "run_id": replay_run_id,
        }
        return updated

    # INVALID_RUN / INFRA_FAILURE on replay: cannot prove a pass; keep the failure
    # and record the inconclusive replay.
    updated["replay"] = {"attempted": True, "result": "not_attempted", "run_id": replay_run_id}
    return updated
