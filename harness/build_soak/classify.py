"""The deterministic classifier (guidelines §13, §16) — compatibility facade.

The actual classification logic now lives in :mod:`._classification`:
* :mod:`._classification._pipeline` — the ordered oracle fold.
* :mod:`._classification._browser_proof` — strict browser-verification paths.
* :mod:`._classification._tool_scope_proof` — frozen inspect-trace extraction.
* :mod:`._classification._dossier_loader` — frozen run-folder loading.
* :mod:`._classification._no_fluke` — the §17 no-fluke replay policy.

This module re-exports every public symbol so existing imports are unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import failure_codes as fc
from ._classification._browser_proof import (
    successful_browser_verification_paths as _successful_browser_verification_paths,
)
from ._classification._dossier_loader import (
    CLASSIFICATION_NAME,
    load_run_folder,
    write_classification,
)
from ._classification._no_fluke import intermittent_classification
from ._classification._pipeline import likely_files_for_code, run_oracle_pipeline
from ._classification._tool_scope_proof import (
    tool_scope_from_inspect as _tool_scope_from_inspect,
)

__all__ = [
    "CLASSIFICATION_NAME",
    "_successful_browser_verification_paths",
    "_tool_scope_from_inspect",
    "classify",
    "classify_run_folder",
    "intermittent_classification",
]


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
    browser_evidence_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Classify one run from its raw event log (full-event dicts OR DB rows) plus
    optional captured evidence. Returns the §13 classification dict.

    `autonomous`, when given (e.g. from the run manifest), overrides the scenario's
    autonomy: an autonomous build legitimately skips the AWAITING_PLAN_APPROVAL gate
    (it auto-approves inline), so the event-chain approval-ordering check relaxes the
    awaiting link for it."""
    results, first_fail, _context = run_oracle_pipeline(
        events_raw,
        scenario=scenario,
        conversation_id=conversation_id,
        workspace_manifest=workspace_manifest,
        preview=preview,
        tool_scope=tool_scope,
        autonomous=autonomous,
        revision_meta=revision_meta,
        provider_ledger=provider_ledger,
        inspect_trace=inspect_trace,
        product_evidence=product_evidence,
        browser_evidence_paths=browser_evidence_paths,
        available_evidence=available_evidence,
        evidence_intact=evidence_intact,
    )

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
    likely = likely_files_for_code(code)
    if likely is not None:
        classification["likely_files"] = likely
    return classification


def classify_run_folder(
    folder: str | Path, *, scenario: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Classify a frozen run folder: load the manifest, verify evidence integrity
    (a mismatch -> INVALID_RUN), read the event log, classify, and write
    classification.json into the folder. Returns the classification dict."""
    dossier = load_run_folder(folder, scenario=scenario)
    classification = classify(
        dossier["events_raw"],
        scenario=dossier["scenario"],
        run_id=dossier["run_id"],
        conversation_id=dossier["conversation_id"],
        commit=dossier["commit"],
        seed=dossier["seed"],
        evidence_intact=dossier["evidence_intact"],
        autonomous=dossier["autonomous"],
        provider_ledger=dossier["provider_ledger"],
        inspect_trace=dossier["inspect_trace"],
        product_evidence=dossier["product_evidence"],
        workspace_manifest=dossier["workspace_manifest"],
        preview=dossier["preview"],
        browser_evidence_paths=dossier["browser_evidence_paths"],
    )
    write_classification(folder, classification)
    return classification
