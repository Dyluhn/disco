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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .matrix import KINDS, ReliabilityMatrix

PASS = "PASS"
FAIL = "FAIL"
INVALID = "INVALID"
INFRA = "INFRA"
STATUSES = frozenset({PASS, FAIL, INVALID, INFRA})

_PRODUCT_ROOT_FILES = frozenset(
    {
        ".env.example",
        "compose.yaml",
        "pyproject.toml",
        "uv.lock",
    }
)
_PRODUCT_FRONTEND_FILES = frozenset(
    {
        "frontend/.env.example",
        "frontend/Dockerfile",
        "frontend/index.html",
        "frontend/nginx.conf",
        "frontend/package-lock.json",
        "frontend/package.json",
        "frontend/vite.config.ts",
    }
)
_NONPRODUCT_PARTS = frozenset(
    {"__pycache__", "__unbiased_gate__", ".venv", "node_modules", "test", "tests"}
)


def _is_product_subject_path(path: str) -> bool:
    """Whether a worktree path can change the shipped runtime subject.

    Test, oracle, evidence, and governance bytes are deliberately excluded. Shared
    dependency and deployment inputs are included conservatively because changing
    them can change the runtime even when package source is untouched.
    """

    parts = Path(path).parts
    if not parts or any(part in _NONPRODUCT_PARTS for part in parts):
        return False
    if path in _PRODUCT_ROOT_FILES or path in _PRODUCT_FRONTEND_FILES:
        return True
    if path.startswith(("deploy/", "integrations/", "prompts/")):
        return True
    if path.startswith("packages/"):
        return "src" in parts or "scripts" in parts or Path(path).name == "pyproject.toml"
    if path.startswith("frontend/src/"):
        name = Path(path).name
        return not any(marker in name for marker in (".test.", ".spec."))
    return path.startswith(("frontend/public/", "frontend/docker-entrypoint.d/"))


def product_subject_identity(
    repo: str | Path,
    *,
    bindings: Mapping[str, str] | None = None,
) -> str:
    """Hash only shipped runtime bytes plus explicit model/provider bindings."""

    root = Path(repo)
    listed = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"]
    )
    paths = sorted(
        path
        for path in listed.decode("utf-8", errors="surrogateescape").split("\0")
        if path and _is_product_subject_path(path)
    )
    digest = hashlib.sha256()
    for relative in paths:
        path = root / relative
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"missing")
        digest.update(b"\n")
    for key, value in sorted((bindings or {}).items()):
        digest.update(b"binding\0")
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


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


def _validated_result_trial_ids(result: dict[str, Any]) -> list[str]:
    status = result.get("status")
    if status not in STATUSES:
        raise ValueError(f"suite result has unsupported status {status!r}")
    kind = result.get("kind")
    if kind not in KINDS:
        raise ValueError(f"suite result has unsupported kind {kind!r}")
    units_passed = result.get("units_passed")
    if not isinstance(units_passed, int) or units_passed < 0:
        raise ValueError("suite result units_passed must be a nonnegative integer")
    raw_trial_ids = result.get("trial_ids")
    if raw_trial_ids is None:
        if kind == "build_soak" and units_passed > 0:
            raise ValueError("counted build-soak evidence requires exact trial_ids")
        return []
    if not isinstance(raw_trial_ids, list) or not all(
        isinstance(item, str) and item.strip() == item and item for item in raw_trial_ids
    ):
        raise ValueError("suite result trial_ids must be a list of nonempty strings")
    if len(raw_trial_ids) != len(set(raw_trial_ids)):
        raise ValueError("suite result trial_ids must be unique")
    if len(raw_trial_ids) != units_passed:
        raise ValueError("suite result trial_ids must match units_passed")
    return raw_trial_ids


def _validated_campaign_trial_ids(results: list[dict[str, Any]]) -> list[str]:
    trial_ids = [trial_id for result in results for trial_id in _validated_result_trial_ids(result)]
    if len(trial_ids) != len(set(trial_ids)):
        raise ValueError("campaign repeats a build-soak trial identity")
    return trial_ids


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
    product_subject_identity: str | None = None,
    evaluator_identity: str | None = None,
) -> None:
    if campaign_id in state["campaign_ids"]:
        raise ValueError(f"campaign {campaign_id!r} is already recorded")
    trial_ids = _validated_campaign_trial_ids(results)

    subject_identity = product_subject_identity or revision
    exact_evaluator_identity = evaluator_identity or revision
    revision_state = state["revisions"].setdefault(
        subject_identity,
        {
            "commit": commit,
            "dirty": dirty,
            "product_subject_identity": subject_identity,
            "evaluator_identities": [],
            "trial_ids": [],
            "tainted": False,
            "tainted_by": [],
            "campaigns": [],
        },
    )
    prior_trial_ids = set(revision_state.get("trial_ids") or [])
    duplicated = sorted(prior_trial_ids.intersection(trial_ids))
    if duplicated:
        raise ValueError(f"trial identity already credited: {duplicated[0]}")
    revision_state.setdefault("trial_ids", []).extend(trial_ids)
    if exact_evaluator_identity not in revision_state.setdefault("evaluator_identities", []):
        revision_state["evaluator_identities"].append(exact_evaluator_identity)
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
            "evaluator_identity": exact_evaluator_identity,
            "commit": commit,
            "dirty": dirty,
            "started_at": started_at,
            "finished_at": finished_at,
            "results": results,
        }
    )
    state["campaign_ids"].append(campaign_id)


def _count_claim_evidence(
    revision_state: dict[str, Any] | None,
    matrix: ReliabilityMatrix,
) -> tuple[dict[str, int], dict[str, set[str]]]:
    """Count PASS evidence per claim (units for count proofs, devices for fresh_device)."""
    counts: dict[str, int] = defaultdict(int)
    fresh_devices: dict[str, set[str]] = defaultdict(set)
    if not revision_state:
        return counts, fresh_devices
    for campaign in revision_state.get("campaigns") or []:
        for result in campaign.get("results") or []:
            if result.get("status") == FAIL:
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
    return counts, fresh_devices


def promotion_report(
    state: dict[str, Any],
    matrix: ReliabilityMatrix,
    *,
    revision: str,
    claim_ids: set[str] | None = None,
) -> dict[str, Any]:
    revision_state = state["revisions"].get(revision)
    tainted = bool(revision_state and revision_state.get("tainted"))
    counts, fresh_devices = _count_claim_evidence(
        revision_state if revision_state and not tainted else None, matrix
    )

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
