"""Finite replacement authority for the owner-requested provider-neutral migration.

Only the exact removed test identities and their source-pinned replacements
are authorized. Historical receipts remain byte-for-byte intact. No subsequent
landing can reuse this allowance to remove another test.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from . import _retirements

PACKAGE = "PKG-24-RESEARCH-RELIABILITY"
BASELINE_COMMIT = "849f556aeb20075af4e7393cda3841a426c48154"
BASELINE_SHA256 = "7888b310c7f282a4fad83edbd4c008df6222a9e5e91e01bd5538080d2822015f"
AUTHORITY_SHA256 = "437c7a6e3ae32bab827c484e7d9514c481a9808b721b025e024a530e53e1278e"
AUTHORITY_PATH = "development/architecture/reliability-test-replacements.json"


def authority(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    blob = (root / AUTHORITY_PATH).read_bytes()
    if hashlib.sha256(blob).hexdigest() != AUTHORITY_SHA256:
        raise RuntimeError("reliability replacement authority digest mismatch")
    record = json.loads(blob)
    old_blob = subprocess.check_output(
        ["git", "-C", str(root), "show", f"{BASELINE_COMMIT}:{_retirements.INVENTORY_PATH}"],
        stderr=subprocess.PIPE,
    )
    if hashlib.sha256(old_blob).hexdigest() != BASELINE_SHA256:
        raise RuntimeError("reliability replacement baseline digest mismatch")
    old = json.loads(old_blob)
    if (
        record["baseline_commit"],
        record["baseline_inventory_sha256"],
        record["source_identity_before"],
        record["package"],
    ) != (BASELINE_COMMIT, BASELINE_SHA256, old["source_identity"], PACKAGE):
        raise RuntimeError("reliability replacement baseline identity mismatch")
    return record, old


def regeneration_authorizations(
    root: Path,
    previous_identity: str,
    roots: dict[str, Any],
    mapping: dict[str, Any],
    relocated: dict[str, set[str]],
) -> dict[str, set[str]]:
    record, old = authority(root)
    if previous_identity != old["source_identity"]:
        raise RuntimeError("reliability replacement authority is single-use")
    _check_replacements(record, roots)
    return _retirements.exact_removals(record, old, roots, mapping, relocated)


def _check_replacements(record: dict[str, Any], roots: dict[str, Any]) -> None:
    live = set(roots["packages"])
    for row in record["dispositions"]:
        if not row["reason"] or not row["replacements"]:
            raise RuntimeError("replacement disposition needs a reason and coverage")
        if not set(row["replacements"]) <= live:
            raise RuntimeError(f"replacement coverage is missing for {row['retired_id']}")
    if sorted(row["retired_id"] for row in record["dispositions"]) != sorted(
        record["retired"]["collected.packages"]
    ):
        raise RuntimeError("replacement dispositions must cover the exact retired population")


def historical_inventory(
    root: Path,
    baseline: dict[str, Any],
    problems: list[str],
) -> dict[str, Any] | None:
    transitions = baseline.get("additive_transitions", [])
    closure = [row for row in transitions if row.get("package") == PACKAGE]
    if not closure:
        return None
    try:
        record, old = authority(root)
        if len(closure) != 1:
            raise RuntimeError("exactly one reliability replacement receipt is required")
        prior_rows = {row["package"]: row for row in old["additive_transitions"]}
        current_rows = {row["package"]: row for row in transitions}
        if any(current_rows.get(key) != row for key, row in prior_rows.items()):
            raise RuntimeError("reliability replacement changed a historical receipt")
        transition = closure[0]
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
            raise RuntimeError("reliability source must have the pinned baseline parent")
        source_blob = subprocess.check_output(
            ["git", "-C", str(root), "show", f"{source}:{_retirements.INVENTORY_PATH}"],
            stderr=subprocess.PIPE,
        )
        if hashlib.sha256(source_blob).hexdigest() != BASELINE_SHA256:
            raise RuntimeError("reliability source changed the prewrite inventory")
        if transition["source_identity_before"] != old["source_identity"]:
            raise RuntimeError("reliability before identity mismatch")
        _retirements._check_retired_counts(transition, record, old)
        _check_replacements(record, baseline["collected"]["roots"])
        if source == baseline.get("source_identity"):
            _retirements._check_initial_removals(root, record, old, baseline)
        return old
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        problems.append(f"reliability replacement authority: {error}")
        return None
