"""HARN-1b (evidence side) — the validated producer of the product-harness dossier.

The HARN-2 oracles READ a ``product_evidence`` dict + a provider-call ledger. This
module is the verified WRITER: it validates a captured dossier against the schema the
oracles expect BEFORE writing it, so a harness bug can't emit malformed evidence that
an oracle would silently mis-adjudicate. The live Playwright harness captures the raw
browser data and calls these to serialize ``product-evidence.json`` and
``provider-call-ledger.jsonl`` into the run folder.

Two producers feed it: the live browser spec (``frontend/e2e-live/``) for the eight
browser/lifecycle slices, and ``development/harness/product_build/edit_evidence.py`` for the five
P8D edit slices (PKG-03-EDIT-EVIDENCE).

This is pure + unit-tested; it does not drive a browser (that is HARN-1b's live spec).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The expected slices and their field types. Each slice is optional (a run that did
# not export has no "export" slice → the ExportDownload oracle SKIPs), but when a slice
# IS present its fields must be well-typed so the oracle reads it fail-closed-correctly.
_SLICE_FIELDS: dict[str, dict[str, type]] = {
    "browser_ws": {"connections": int},
    "lifecycle": {"terminal": str, "statuses": list},
    "sidecar": {"stopped_at_terminal": bool, "provider_calls_after_terminal": int},
    "preview": {"owner": str, "manual_port": bool},
    "shown": {"artifact_shown": bool, "preview_shown": bool},
    "verification": {"ready_for_verification_called": bool, "passed": bool},
    # download_present/download_bytes ARE adjudicated by ExportDownloadOracle — they must be
    # type-validated here too, else e.g. a bool download_bytes capture bug evades validation.
    "export": {
        "requested": bool,
        "download_present": bool,
        "download_bytes": int,
        "workspace_match": bool,
    },
    "cleanup": {
        "orphans": int,
        "workspace_released": bool,
        "scope": str,
        "container_orphans": int,
        "volume_orphans": int,
        "volume_scope": str,
    },
    # PKG-03-EDIT-EVIDENCE — the five P8D edit slices. Admitted together with their
    # producer (development/harness/product_build/edit_evidence.py) and never ahead of it: a slice the
    # writer accepts but nothing produces is a false affordance, and it is what let these
    # five oracles SKIP unnoticed across 2082 frozen classifications.
    "targeted_edit": {"edited_files": list, "expected_files": list},
    # max_churn_ratio is float here though the oracle also accepts an int: the writer is the
    # stricter gate, and the producer emits an explicit float.
    "rewrite_avoidance": {
        "edit_scope": str,
        "changed_lines": int,
        "total_lines": int,
        "max_churn_ratio": float,
    },
    "manual_edit": {"overrides": dict, "final_files": dict},
    "comment_anchors": {"before": list, "after": list},
    "screen_labels": {"edited_sections": list, "before": dict, "after": dict},
}

PRODUCT_EVIDENCE_NAME = "product-evidence.json"
PROVIDER_LEDGER_NAME = "provider-call-ledger.jsonl"


def validate_product_evidence(ev: dict[str, Any]) -> list[str]:
    """Return a list of schema problems (empty = valid). Unknown top-level keys are
    allowed (forward-compat); a known slice must be a dict whose declared fields, when
    present, carry the right type. ``bool`` is rejected where ``int`` is required (a
    bool count is a capture bug)."""
    problems: list[str] = []
    if not isinstance(ev, dict):
        return ["product_evidence is not a dict"]
    for slice_name, fields in _SLICE_FIELDS.items():
        if slice_name not in ev:
            continue
        slice_val = ev[slice_name]
        if not isinstance(slice_val, dict):
            problems.append(f"{slice_name}: not a dict")
            continue
        for field, typ in fields.items():
            if field not in slice_val:
                continue
            val = slice_val[field]
            if typ is int and isinstance(val, bool):
                problems.append(f"{slice_name}.{field}: bool where int expected")
            elif not isinstance(val, typ):
                problems.append(
                    f"{slice_name}.{field}: {type(val).__name__} where {typ.__name__} expected"
                )
    return problems


def write_product_evidence(folder: str | Path, ev: dict[str, Any], *, strict: bool = True) -> Path:
    """Validate then write ``product-evidence.json`` into ``folder``. With
    ``strict=True`` (default) a schema problem raises ValueError BEFORE writing, so the
    harness never persists evidence the oracles can't trust."""
    problems = validate_product_evidence(ev)
    if problems and strict:
        raise ValueError(f"product_evidence failed schema validation: {problems}")
    out = Path(folder) / PRODUCT_EVIDENCE_NAME
    out.write_text(json.dumps(ev, indent=2, sort_keys=True), encoding="utf-8")
    return out


def write_provider_ledger(folder: str | Path, records: list[dict[str, Any]]) -> Path:
    """Write ``provider-call-ledger.jsonl`` (one JSON record per line). Each record
    should carry at least a ``host``; the ProviderLedgerOracle fail-closes on a hostless
    record, so a non-dict / hostless entry here would surface as an evidence gap."""
    out = Path(folder) / PROVIDER_LEDGER_NAME
    lines = [json.dumps(r, sort_keys=True) for r in records]
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out
