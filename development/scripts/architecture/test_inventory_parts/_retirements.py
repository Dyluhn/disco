"""Finite, source-pinned test retirements for the deep-research closeout.

This authority records removals, never replacements. Historical addition
receipts stay intact and are checked against the sealed inventory they owned.
Future removals remain forbidden by the ordinary regeneration gate.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from ._rows import canonical_row

PACKAGE = "PKG-35-DEEP-RESEARCH-CLOSEOUT"
BASELINE_COMMIT = "39117e8e2f094a0d5a3be502766fa0d5b48863a4"
BASELINE_SHA256 = "98c1ce4994d21a7c8e0a55ac76a5534ad25f3032cb7f7696ff83b4e906c004cf"
AUTHORITY_SHA256 = "0a96d69db8a979fb4103b25bb04c0fd90e7e5e74506bd30dd94d7d6de3c27e6f"
INVENTORY_PATH = "development/architecture/test-inventory.json"
AUTHORITY_PATH = "development/architecture/research-test-retirements.json"


def authority(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    blob = (root / AUTHORITY_PATH).read_bytes()
    if hashlib.sha256(blob).hexdigest() != AUTHORITY_SHA256:
        raise RuntimeError("retirement authority digest mismatch")
    record = json.loads(blob)
    ledger_blob = (
        root / "development/architecture/research-test-retirement-dispositions.json"
    ).read_bytes()
    if hashlib.sha256(ledger_blob).hexdigest() != record["behavior_ledger_sha256"]:
        raise RuntimeError("retirement disposition ledger digest mismatch")
    old_blob = subprocess.check_output(
        ["git", "-C", str(root), "show", f"{BASELINE_COMMIT}:{INVENTORY_PATH}"],
        stderr=subprocess.PIPE,
    )
    if hashlib.sha256(old_blob).hexdigest() != BASELINE_SHA256:
        raise RuntimeError("retirement baseline digest mismatch")
    old = json.loads(old_blob)
    if (
        record["baseline_commit"],
        record["baseline_inventory_sha256"],
        record["source_identity_before"],
        record["package"],
    ) != (BASELINE_COMMIT, BASELINE_SHA256, old["source_identity"], PACKAGE):
        raise RuntimeError("retirement authority source mismatch")
    for path, expected in record.get("marker_source_sha256", {}).items():
        if hashlib.sha256(_on_disk(root, path).read_bytes()).hexdigest() != expected:
            raise RuntimeError("approved environmental marker source changed")
    return record, old


def _on_disk(root: Path, path: str) -> Path:
    """The receipt names the path the file had when it was approved; the bytes
    are what the receipt pins, so a bucket move does not falsify it."""
    candidate = root / path
    if candidate.is_file():
        return candidate
    for bucket in ("current/", "development/"):
        if path.startswith(bucket):
            return root / path[len(bucket):]
    return candidate


def values(roots: dict[str, Any], mapping: dict[str, Any]) -> dict[str, list[Any]]:
    return {
        **{f"collected.{key}": value for key, value in roots.items()},
        **{
            f"mapping_static.{key}": mapping[key]
            for key in (
                "python_test_files",
                "python_static_test_ids",
                "typescript_test_files",
                "typescript_static_test_ids",
                "fixtures",
                "markers",
            )
        },
    }


def exact_removals(
    record: dict[str, Any],
    old: dict[str, Any],
    roots: dict[str, Any],
    mapping: dict[str, Any],
    relocated: dict[str, set[str]],
) -> dict[str, set[str]]:
    before = values(old["collected"]["roots"], old["mapping_static"])
    after = values(roots, mapping)
    retired = record["retired"]
    if set(retired) != set(before):
        raise RuntimeError("retirement fields must match the exact inventory roots")
    result: dict[str, set[str]] = {}
    for label, previous in before.items():
        removed = Counter(map(canonical_row, previous)) - Counter(map(canonical_row, after[label]))
        expected = Counter(
            {key: count for key, count in removed.items() if key not in relocated.get(label, set())}
        )
        declared = Counter(map(canonical_row, retired[label]))
        if declared != expected:
            raise RuntimeError(
                f"retirement set is not exact: {label}; "
                f"unrecorded={sorted((expected - declared).elements())[:8]}; "
                f"excess={sorted((declared - expected).elements())[:8]}"
            )
        result[label] = set(declared)
    return result


def regeneration_authorizations(
    root: Path,
    previous_identity: str,
    package: str,
    roots: dict[str, Any],
    mapping: dict[str, Any],
    relocated: dict[str, set[str]],
) -> dict[str, set[str]]:
    if package != PACKAGE:
        return {}
    record, old = authority(root)
    if previous_identity != old["source_identity"]:
        raise RuntimeError("retirement authority is single-use at its sealed baseline")
    return exact_removals(record, old, roots, mapping, relocated)


def _check_retired_count_owners(transitions: list[dict[str, Any]], problems: list[str]) -> None:
    for row in transitions:
        counts_by_root = row.get("collected_roots", {})
        if not isinstance(counts_by_root, dict) or row.get("package") == PACKAGE:
            continue
        if any(
            isinstance(counts, dict) and "retired_count" in counts
            for counts in counts_by_root.values()
        ):
            problems.append("retired_count requires the finite closeout authority")


def _check_closeout_source(root: Path, transition: dict[str, Any], old: dict[str, Any]) -> None:
    source = transition["source_identity_after"]
    parents = (
        subprocess.check_output(
            ["git", "-C", str(root), "rev-list", "--parents", "-n", "1", source],
            stderr=subprocess.PIPE,
        )
        .decode()
        .split()
    )
    if parents != [source, BASELINE_COMMIT]:
        raise RuntimeError("retirement closeout source must have the sealed baseline parent")
    source_blob = subprocess.check_output(
        ["git", "-C", str(root), "show", f"{source}:{INVENTORY_PATH}"],
        stderr=subprocess.PIPE,
    )
    if hashlib.sha256(source_blob).hexdigest() != BASELINE_SHA256:
        raise RuntimeError("retirement source changed the sealed prewrite inventory")
    if transition["source_identity_before"] != old["source_identity"]:
        raise RuntimeError("retirement closeout before identity mismatch")


def _check_retired_counts(
    transition: dict[str, Any], record: dict[str, Any], old: dict[str, Any]
) -> None:
    for name, retired in record["retired"].items():
        if not name.startswith("collected."):
            continue
        label = name.partition(".")[2]
        counts = transition["collected_roots"].get(label)
        if counts is None:
            if retired:
                raise RuntimeError(f"missing retirement counts: {label}")
            continue
        if counts.get("retired_count", 0) != len(retired) or counts.get("before_count") != len(
            old["collected"]["roots"][label]
        ):
            raise RuntimeError(f"retirement count authority mismatch: {label}")


def _check_initial_removals(
    root: Path, record: dict[str, Any], old: dict[str, Any], baseline: dict[str, Any]
) -> None:
    from ..test_inventory import _split_authorizations

    relocated, _markers, ledger = _split_authorizations(
        root,
        baseline.get("module_split_transitions", []),
        old["collected"]["roots"],
        baseline["collected"]["roots"],
        old["mapping_static"],
        baseline["mapping_static"],
    )
    if ledger.count_problems():
        raise RuntimeError("retirement closeout has inexact relocation counts")
    exact_removals(
        record, old, baseline["collected"]["roots"], baseline["mapping_static"], relocated
    )


def historical_inventory(
    root: Path, baseline: dict[str, Any], problems: list[str]
) -> dict[str, Any] | None:
    transitions = baseline.get("additive_transitions", [])
    if not isinstance(transitions, list):
        return None
    transitions = [row for row in transitions if isinstance(row, dict)]
    closure = [row for row in transitions if row.get("package") == PACKAGE]
    _check_retired_count_owners(transitions, problems)
    if not closure:
        return None
    try:
        record, old = authority(root)
        if len(closure) != 1:
            raise RuntimeError("exactly one retirement closeout receipt is required")
        prior_rows = {row["package"]: row for row in old["additive_transitions"]}
        current_rows = {row.get("package"): row for row in transitions}
        if any(current_rows.get(key) != row for key, row in prior_rows.items()):
            raise RuntimeError("retirement closeout changed a sealed historical receipt")
        transition = closure[0]
        _check_closeout_source(root, transition, old)
        _check_retired_counts(transition, record, old)
        if transition["source_identity_after"] == baseline.get("source_identity"):
            _check_initial_removals(root, record, old, baseline)
        return old
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        problems.append(f"test retirement authority: {error}")
        return None


def live_authority(
    root: Path, transitions: list[dict[str, Any]], package: str = ""
) -> tuple[list[str], list[str]] | None:
    """Require the exact approved expansion of the original live population."""
    if not isinstance(transitions, list):
        return None
    if package != PACKAGE and not any(
        isinstance(row, dict) and row.get("package") == PACKAGE for row in transitions
    ):
        return None
    record, old = authority(root)
    prior = old["frontend_real_collection"]["playwright_configs"]["playwright.live.config.ts"]
    ids, files = record["live_source_ids"], record["live_source_files"]
    if not set(prior["ids"]) <= set(ids) or not set(prior["files"]) <= set(files):
        raise RuntimeError("live browser expansion removed a sealed source")
    return ids, files


def marker_additions(root: Path, previous_identity: str, package: str) -> list[dict[str, Any]]:
    if package != PACKAGE:
        return []
    record, old = authority(root)
    if previous_identity != old["source_identity"]:
        raise RuntimeError("marker addition authority is single-use")
    return record.get("permitted_marker_additions", [])
