"""Bounded Build Soak classification owner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..adapters.disco_api import (
    CollectedRun,
)
from ..classify import CLASSIFICATION_NAME, classify
from .ledger import (
    _provider_ledger_for_run,
)


def classify_dossier(
    base: Path,
    scenario: dict[str, Any],
    run: CollectedRun,
    *,
    autonomous: bool,
    commit: str = "",
    seed: int | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the deterministic classifier on the collected run, passing workspace +
    preview + autonomy DIRECTLY (codex #2 — so OutputTruth/preview actually run),
    and write classification.json into the dossier."""
    # [Lane A A-M4] Wire the parsed relay ledger so the INDEPENDENT ProviderLedgerOracle runs
    # alongside the sidecar slice (defense-in-depth: two after-terminal checks, not one). Scope to
    # THIS run's window [run_start, ...] and stamp after_terminal on records past the build-terminal
    # epoch; ran AFTER _collect's grace+release so any straggler post-terminal call is already
    # logged. Live-mode only (relay env set) so deterministic unit tests are unaffected.
    if provider_ledger is None:
        provider_ledger = _provider_ledger_for_run(run)
    classification = classify(
        run.events,
        scenario=scenario,
        run_id=base.name,
        conversation_id=run.conversation_id,
        commit=commit,
        seed=seed,
        workspace_manifest=run.workspace_manifest,
        preview=run.preview,
        autonomous=autonomous,
        provider_ledger=provider_ledger,
        inspect_trace=run.inspect_trace,
        # HARN-2: browser product-harness evidence (None until HARN-1b populates it on
        # CollectedRun; the browser oracles SKIP without it, so headless runs are unaffected).
        product_evidence=getattr(run, "product_evidence", None),
        browser_evidence_paths=set(run.browser_evidence),
        revision_meta={
            "declared_followup_seqs": list(run.declared_followup_seqs),
            "declared_followup_requires_revision": list(run.declared_followup_requires_revision),
            "harness_injected_user_seqs": list(run.harness_injected_user_seqs),
        },
    )
    # Part B traceability: a PASS that REQUIRED auto-resolving an AWAITING_USER_DECISION gate
    # must be DISTINGUISHABLE from a clean PASS — surface the count + the picked options so
    # monitoring can detect "the model asked for choices unexpectedly".
    classification["auto_resolved_decisions"] = len(run.decision_resolutions)
    if run.decision_resolutions:
        classification["decision_resolutions"] = list(run.decision_resolutions)
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(classification, indent=2, sort_keys=True), encoding="utf-8"
    )
    return classification
