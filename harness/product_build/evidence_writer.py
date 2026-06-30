"""Assemble a classifiable run-folder dossier from CAPTURED product-harness data (P1B-LIVE-2).

`write_dossier` serializes ONLY what the live run captured — the assembled product_evidence
dict, the provider-call ledger, the build event log, and any raw artifacts — into a run
folder, locked with an EvidenceManifest (hashes) so the classifier loads it via the existing
evidence lock. It PREFLIGHTS the product_evidence against the schema BEFORE writing anything,
so a malformed capture never produces a partial/untrusted dossier, and it rejects unsafe
artifact paths. No fabricated defaults — an un-captured slice is simply absent (its oracle SKIPs).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.build_soak.evidence import EvidenceManifest, compute_evidence_hashes, write_manifest
from harness.build_soak.product_evidence import (
    PRODUCT_EVIDENCE_NAME,
    PROVIDER_LEDGER_NAME,
    validate_product_evidence,
    write_product_evidence,
    write_provider_ledger,
)

_EVENTS_NAME = "events.jsonl"


def _safe_rel(rel: str) -> str:
    """Reject an absolute path or one that escapes the run folder (no clobber outside)."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe artifact path: {rel!r}")
    return rel


def write_dossier(
    run_dir: str | Path,
    *,
    product_evidence: dict[str, Any],
    provider_records: list[dict[str, Any]],
    events: list[dict[str, Any]],
    artifacts: dict[str, str] | None = None,
    run_id: str = "",
    scenario_id: str = "",
    autonomous: bool = False,
) -> Path:
    """Write the dossier into ``run_dir`` and return it. Raises ValueError on malformed
    product_evidence (preflight, before any write) or an unsafe artifact path.

    ``autonomous`` is a RUN fact (was this build driven in autonomous mode, auto-approving
    its plan inline?) recorded on the manifest so the classifier's EventChainOracle relaxes
    the AWAITING_PLAN_APPROVAL link — an autonomous build legitimately emits no awaiting-
    approval status, so without this an otherwise-clean autonomous run fails
    PLAN_APPROVED_STATUS_MISSING. It is the disco-kernel's DEFAULT drive mode, so the live
    product harness MUST thread it; the synthetic green fixture is a non-autonomous drive
    (full awaiting+approval chain), which is why it passed without this."""
    # PREFLIGHT — validate before touching the filesystem (no partial untrusted dossier).
    problems = validate_product_evidence(product_evidence)
    if problems:
        raise ValueError(f"product_evidence failed schema validation: {problems}")
    artifacts = artifacts or {}
    for rel in artifacts:
        _safe_rel(rel)

    base = Path(run_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / _EVENTS_NAME).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8"
    )
    write_product_evidence(base, product_evidence, strict=True)
    write_provider_ledger(base, provider_records)
    # Raw artifacts go UNDER artifacts/ — so an artifact named "product-evidence.json" / "events
    # .jsonl" can never overwrite the strict-validated core dossier files.
    for rel, content in artifacts.items():
        out = base / "artifacts" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")

    # Lock the dossier: a manifest over every evidence file + its hash. Artifact labels are
    # "artifact:"-prefixed so they can't collide with a core label (which classify reads by).
    evidence_files: dict[str, str] = {
        "events": _EVENTS_NAME,
        "product_evidence": PRODUCT_EVIDENCE_NAME,
        "provider_ledger": PROVIDER_LEDGER_NAME,
        **{f"artifact:{rel}": f"artifacts/{rel}" for rel in artifacts},
    }
    manifest = EvidenceManifest(
        run_id=run_id,
        scenario_id=scenario_id,
        mode="ui",
        kernel="disco",
        autonomous=autonomous,
        evidence_files=evidence_files,
        evidence_hashes=compute_evidence_hashes(base, evidence_files),
    )
    write_manifest(base, manifest)
    return base
