"""Commit/tree-scoped evidence ledger for reliability promotion.

The central invariant is intentionally unforgiving: once a product failure is
observed on a revision, no pass from that same revision counts.  A code/config
change creates a new revision fingerprint and starts the post-fix streak over.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .matrix import ReliabilityMatrix

PASS = "PASS"
FAIL = "FAIL"
INVALID = "INVALID"
INFRA = "INFRA"
STATUSES = frozenset({PASS, FAIL, INVALID, INFRA})


def source_revision(repo: str | Path) -> tuple[str, str, bool]:
    """Return ``(revision fingerprint, HEAD commit, dirty)``.

    The dirty fingerprint includes the tracked diff and untracked file bytes, so
    fixing a failure without committing still starts a genuinely new streak.
    """

    root = Path(repo)
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    status = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
        ],
    )
    if not status:
        return commit, commit, False

    digest = hashlib.sha256()
    digest.update(
        subprocess.check_output(["git", "-C", str(root), "diff", "--binary", "HEAD", "--", "."])
    )
    entries = status.decode("utf-8", errors="surrogateescape").split("\0")
    for entry in entries:
        if not entry or not entry.startswith("?? "):
            continue
        relative = entry[3:]
        path = root / relative
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            digest.update(path.read_bytes())
    return f"{commit}+dirty.{digest.hexdigest()[:16]}", commit, True


def empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "revisions": {}, "campaign_ids": []}


def load_state(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return empty_state()
    state = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("unsupported reliability state schema")
    if not isinstance(state.get("revisions"), dict):
        raise ValueError("reliability state revisions must be a mapping")
    if not isinstance(state.get("campaign_ids"), list):
        raise ValueError("reliability state campaign_ids must be a list")
    return state


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(target)


@contextmanager
def state_transaction(path: str | Path) -> Iterator[dict[str, Any]]:
    """Serialize a read-modify-write of the shared campaign ledger.

    Campaign suites intentionally run in parallel and operators may launch
    multiple campaign processes. Atomic rename alone protects JSON integrity but
    not against lost updates (two readers can each overwrite the other's newly
    appended campaign). The sibling lock covers load, mutation, save, and any
    promotion calculation the caller performs before yielding back.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = load_state(target)
            yield state
            save_state(target, state)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def record_campaign(
    state: dict[str, Any],
    *,
    campaign_id: str,
    revision: str,
    commit: str,
    dirty: bool,
    started_at: str,
    finished_at: str,
    results: list[dict[str, Any]],
) -> None:
    if campaign_id in state["campaign_ids"]:
        raise ValueError(f"campaign {campaign_id!r} is already recorded")
    for result in results:
        status = result.get("status")
        if status not in STATUSES:
            raise ValueError(f"suite result has unsupported status {status!r}")
        if not isinstance(result.get("units_passed"), int) or result["units_passed"] < 0:
            raise ValueError("suite result units_passed must be a nonnegative integer")

    revision_state = state["revisions"].setdefault(
        revision,
        {
            "commit": commit,
            "dirty": dirty,
            "tainted": False,
            "tainted_by": [],
            "campaigns": [],
        },
    )
    failures = [result for result in results if result["status"] == FAIL]
    if failures:
        revision_state["tainted"] = True
        revision_state["tainted_by"].extend(
            {
                "campaign_id": campaign_id,
                "suite_id": result.get("suite_id"),
                "reason": result.get("reason"),
            }
            for result in failures
        )
    revision_state["campaigns"].append(
        {
            "campaign_id": campaign_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "results": results,
        }
    )
    state["campaign_ids"].append(campaign_id)


def promotion_report(
    state: dict[str, Any],
    matrix: ReliabilityMatrix,
    *,
    revision: str,
    claim_ids: set[str] | None = None,
) -> dict[str, Any]:
    revision_state = state["revisions"].get(revision)
    tainted = bool(revision_state and revision_state.get("tainted"))
    counts: dict[str, int] = defaultdict(int)
    fresh_devices: dict[str, set[str]] = defaultdict(set)
    if revision_state and not tainted:
        for campaign in revision_state.get("campaigns") or []:
            for result in campaign.get("results") or []:
                if result.get("status") != PASS:
                    continue
                for claim_id in result.get("claims") or []:
                    claim = matrix.claims.get(str(claim_id))
                    if claim is None:
                        continue
                    if claim.proof == "fresh_device":
                        device_id = result.get("fresh_device_id")
                        if isinstance(device_id, str) and device_id:
                            fresh_devices[claim.id].add(device_id)
                    else:
                        counts[claim.id] += int(result.get("units_passed") or 0)

    selected = claim_ids or set(matrix.claims)
    claims: list[dict[str, Any]] = []
    for claim_id in sorted(selected):
        claim = matrix.claims[claim_id]
        observed = (
            len(fresh_devices[claim_id]) if claim.proof == "fresh_device" else counts[claim_id]
        )
        claims.append(
            {
                "id": claim_id,
                "surface": claim.surface,
                "proof": claim.proof,
                "observed": observed,
                "target": claim.target,
                "remaining": max(0, claim.target - observed),
                "complete": not tainted and observed >= claim.target,
            }
        )
    return {
        "revision": revision,
        "known_revision": revision_state is not None,
        "tainted": tainted,
        "tainted_by": (revision_state or {}).get("tainted_by") or [],
        "eligible": bool(claims) and all(item["complete"] for item in claims),
        "claims": claims,
    }
