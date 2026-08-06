"""P1B-LIVE-1: the TS evidence-assembly mirror (frontend/e2e-live/support/productEvidence.ts
SLICE_FIELDS) must match the Python _SLICE_FIELDS EXACTLY (cross-language drift guard), and
_SLICE_FIELDS must cover every field the 8 HARN-2 browser oracles actually adjudicate."""

from __future__ import annotations

import re
from pathlib import Path

from harness.build_soak.product_evidence import _SLICE_FIELDS

_TS = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "src"
    / "lib"
    / "harness"
    / "productEvidence.ts"
)

# Every field each oracle READS — audited from oracles/browser_evidence.py and, for the
# five PKG-03-EDIT-EVIDENCE slices, from oracles/targeted_edit.py +
# manual_edit_preservation.py. The edit rows are here for the same reason the browser rows
# are: without them the coverage guard below silently does not apply to the new oracles,
# which is how a field an oracle adjudicates can go untyped by the writer.
_ORACLE_READS: dict[str, set[str]] = {
    "browser_ws": {"connections"},
    "lifecycle": {"terminal", "statuses"},
    "sidecar": {"stopped_at_terminal", "provider_calls_after_terminal"},
    "preview": {"owner", "manual_port"},
    "shown": {"artifact_shown", "preview_shown"},
    "verification": {"ready_for_verification_called", "passed"},
    "export": {"requested", "download_present", "download_bytes", "workspace_match"},
    "cleanup": {
        "orphans",
        "workspace_released",
        "scope",
        "container_orphans",
        "volume_orphans",
        "volume_scope",
    },
    "targeted_edit": {"edited_files", "expected_files"},
    "rewrite_avoidance": {"edit_scope", "changed_lines", "total_lines", "max_churn_ratio"},
    "manual_edit": {"overrides", "final_files"},
    "comment_anchors": {"before", "after"},
    "screen_labels": {"edited_sections", "before", "after"},
}


def _parse_ts_slice_fields() -> dict[str, set[str]]:
    text = _TS.read_text(encoding="utf-8")
    block = re.search(r"SLICE_FIELDS[^{]*\{(.*?)\n\};", text, re.DOTALL)
    assert block, "could not find SLICE_FIELDS object in productEvidence.ts"
    out: dict[str, set[str]] = {}
    for key, body in re.findall(r"(\w+):\s*\[([^\]]*)\]", block.group(1)):
        out[key] = set(re.findall(r'"([^"]+)"', body))
    return out


def test_schema_covers_every_oracle_read_field() -> None:
    # the hardened _SLICE_FIELDS must type-validate every field an oracle adjudicates
    for slice_name, reads in _ORACLE_READS.items():
        have = set(_SLICE_FIELDS[slice_name])
        assert reads <= have, f"{slice_name}: oracle reads {reads - have} not in _SLICE_FIELDS"


def test_export_download_fields_now_validated() -> None:
    # the specific P1A gap gpt-5.5 found: download_present/download_bytes are now typed
    assert _SLICE_FIELDS["export"]["download_present"] is bool
    assert _SLICE_FIELDS["export"]["download_bytes"] is int
    assert _SLICE_FIELDS["export"]["workspace_match"] is bool


def test_bool_download_bytes_now_fails_validation() -> None:
    # direct regression for the gap: a bool where download_bytes (int) is expected is caught
    from harness.build_soak.product_evidence import validate_product_evidence

    problems = validate_product_evidence(
        {"export": {"requested": True, "download_present": True, "download_bytes": True}}
    )
    assert any("download_bytes" in p and "bool" in p for p in problems), problems


def test_ts_mirror_matches_python_slice_fields_exactly() -> None:
    ts = _parse_ts_slice_fields()
    py = {k: set(v) for k, v in _SLICE_FIELDS.items()}
    assert ts == py, (
        f"TS↔Python product_evidence drift — TS-only: {set(ts) ^ set(py)}; {ts} vs {py}"
    )
