"""Failure capsules — bind an existing dossier to a safe replay boundary.

A capsule makes a focused diagnostic rerun cheaper. It does **not** turn a
stochastic model call into a deterministic test, and it never overwrites the
original verdict.

This module creates no new store and no new lifecycle. Every fact it binds
already exists in the dossier written by ``assemble_dossier``: the evidence
manifest, the classification, the workspace manifest, and (for a hard-cap stop)
the ``workspace_freeze`` disclosure produced by the Epic-1 freeze path. The
capsule's job is to bind those facts together, prove they still agree, and
resolve the one exact immutable workspace version a replay may read.

What a capsule deliberately cannot promise
------------------------------------------
Replay creates a **new isolated diagnostic run**. Provider randomness and hidden
model state are not reproduced, and saying otherwise would be the exact kind of
false affordance the standards forbid. Every capsule therefore carries::

    diagnostic_replay: true
    counts_toward_promotion: false
    exact_model_hidden_state_reproduced: false

Safe boundaries only
--------------------
A capsule is taken at a **quiescent** boundary — an accepted event horizon where
the workspace was durably versioned and no authority moved. There is no attempt
to snapshot mid-provider, interpreter memory, a browser process, or a live
sandbox. If no safe boundary exists, capsule creation fails closed rather than
claiming a boundary it cannot honour.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .evidence import load_manifest, verify_evidence_unchanged

CAPSULE_SCHEMA_VERSION = 1
CAPSULE_NAME = "failure-capsule.json"

# Stated on every capsule and every replay dossier. Not decoration: a reader
# must never have to infer that a replay is non-promoting.
DISCLOSURE: dict[str, bool] = {
    "diagnostic_replay": True,
    "counts_toward_promotion": False,
    "exact_model_hidden_state_reproduced": False,
}


class CapsuleError(ValueError):
    """A capsule could not be built, verified, or restored. Always fail closed."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise CapsuleError(f"{label} is missing at {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CapsuleError(f"{label} is unreadable at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CapsuleError(f"{label} must be a JSON object at {path}")
    return value


# --------------------------------------------------------------------------
# Safe boundary
# --------------------------------------------------------------------------


def accepted_boundary(product_evidence: dict[str, Any]) -> dict[str, Any]:
    """The one quiescent boundary a capsule may bind to, or refuse.

    The Epic-1 freeze already establishes exactly this: a durable PAUSED, a
    ``WorkspaceVersionEvent`` that follows it, an authority fence proving no user
    turn / resume / run-intent / agent-view change crossed it, and a freshly
    verified immutable version. A landed freeze IS a safe boundary. A
    ``FREEZE_TIMEOUT`` is not, and must not be dressed up as one.
    """
    freeze = (product_evidence.get("diagnostic_stop") or {}).get("workspace_freeze") or {}
    if freeze.get("status") != "frozen":
        raise CapsuleError(
            "no safe boundary: the pre-kill freeze did not land "
            f"(status={freeze.get('status')!r}, reason={freeze.get('reason')!r}). "
            "A capsule must never claim a boundary the run did not reach."
        )
    horizon = freeze.get("horizon_seq")
    version_seq = freeze.get("version_seq")
    if not isinstance(horizon, int) or version_seq is None:
        raise CapsuleError(
            "no safe boundary: the freeze disclosure lacks an accepted event "
            f"horizon or immutable version (horizon_seq={horizon!r}, "
            f"version_seq={version_seq!r})"
        )
    return {
        "kind": "accepted_workspace_version_event",
        "accepted_event_horizon_seq": horizon,
        "paused_seq": freeze.get("paused_seq"),
        "workspace_version_seq": version_seq,
        "workspace_tree_digest": freeze.get("tree_digest"),
        "workspace_file_count": freeze.get("file_count"),
        "workspace_total_bytes": freeze.get("total_bytes"),
    }


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_capsule(dossier: Path, conversation_id: str) -> dict[str, Any]:
    """Bind an existing dossier into a capsule. Reads only; writes nothing."""
    dossier = Path(dossier)
    conv = dossier / "conversations" / conversation_id
    if not conv.is_dir():
        raise CapsuleError(f"conversation directory does not exist: {conv}")

    manifest = load_manifest(dossier)
    integrity = verify_evidence_unchanged(dossier, manifest)
    if not integrity.intact:
        raise CapsuleError(
            "parent dossier evidence is not intact; a capsule may not be built "
            f"over drifted evidence: {integrity}"
        )

    classification = _read_json(dossier / "classification.json", "classification.json")
    product_evidence = _read_json(conv / "product-evidence.json", "product-evidence.json")
    workspace_manifest = _read_json(conv / "workspace-manifest.json", "workspace-manifest.json")
    boundary = accepted_boundary(product_evidence)

    # Browser references are admitted ONLY at or before the accepted horizon.
    # The Epic-1 collection path already clipped them; recording the horizon here
    # means a tampered capsule cannot widen it later.
    browser_root = conv / "browser-evidence"
    browser_refs: dict[str, str] = {}
    if browser_root.is_dir():
        for path in sorted(browser_root.rglob("*")):
            if path.is_symlink():
                raise CapsuleError(f"browser evidence contains a symlink: {path}")
            if path.is_file():
                browser_refs[path.relative_to(browser_root).as_posix()] = _sha256_bytes(
                    path.read_bytes()
                )

    body: dict[str, Any] = {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        **DISCLOSURE,
        "parent": {
            "run_id": manifest.run_id,
            "conversation_id": conversation_id,
            "dossier_path": str(dossier),
        },
        "verdict": {
            "status": classification.get("status"),
            "code": classification.get("code"),
            "first_broken_link": classification.get("first_broken_link"),
        },
        "scenario": {
            "id": manifest.scenario_id,
            "sha256": manifest.scenario_sha256,
            "seed": manifest.seed,
            "surface": manifest.surface,
        },
        "binding": {
            "model": manifest.model,
            "provider": manifest.provider,
            "kernel": manifest.kernel,
            "mode": manifest.mode,
            "assist": manifest.assist,
            "autonomous": manifest.autonomous,
            "repo_commit": manifest.repo_commit,
            "repo_revision": manifest.repo_revision,
            "repo_dirty": manifest.repo_dirty,
        },
        "boundary": boundary,
        "workspace_byte_manifest": {
            path: entry.get("sha256")
            for path, entry in sorted(workspace_manifest.items())
            if isinstance(entry, dict) and entry.get("sha256")
        },
        "browser_refs": browser_refs,
        "evidence_hashes": dict(manifest.evidence_hashes),
        "cleanup": product_evidence.get("cleanup") or {},
    }
    body["capsule_digest"] = _sha256_bytes(_canonical(body))
    return body


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------


def _verify_capsule_digest(capsule: dict[str, Any]) -> None:
    """Refuse a capsule whose own bytes have moved."""
    recorded = capsule.get("capsule_digest")
    if not isinstance(recorded, str) or not recorded:
        raise CapsuleError("capsule has no digest")
    body = {k: v for k, v in capsule.items() if k != "capsule_digest"}
    actual = _sha256_bytes(_canonical(body))
    if actual != recorded:
        raise CapsuleError(
            f"capsule digest mismatch: recorded {recorded}, actual {actual} — "
            "a bound field was altered after the capsule was sealed"
        )


def _verify_capsule_dossier_binding(capsule: dict[str, Any], dossier: Path) -> None:
    """Refuse a capsule whose parent dossier has moved."""
    manifest = load_manifest(dossier)
    scenario = capsule.get("scenario") or {}
    if manifest.scenario_id != scenario.get("id"):
        raise CapsuleError("capsule scenario id no longer matches the parent dossier")
    if manifest.scenario_sha256 != scenario.get("sha256"):
        raise CapsuleError("capsule scenario hash no longer matches the parent dossier")
    binding = capsule.get("binding") or {}
    for field_name, observed in (
        ("model", manifest.model),
        ("provider", manifest.provider),
        ("kernel", manifest.kernel),
        ("repo_revision", manifest.repo_revision),
    ):
        if binding.get(field_name) != observed:
            raise CapsuleError(f"capsule {field_name} binding no longer matches the parent dossier")
    integrity = verify_evidence_unchanged(dossier, manifest)
    if not integrity.intact:
        raise CapsuleError("parent dossier evidence changed since the capsule was sealed")


def verify_capsule(capsule: dict[str, Any], dossier: Path | None = None) -> None:
    """Refuse a capsule whose own bytes, or whose parent dossier, have moved.

    Every check here runs BEFORE any replay spend.
    """
    if not isinstance(capsule, dict):
        raise CapsuleError("capsule must be a JSON object")
    if capsule.get("schema_version") != CAPSULE_SCHEMA_VERSION:
        raise CapsuleError(f"unsupported capsule schema_version {capsule.get('schema_version')!r}")
    for key, expected in DISCLOSURE.items():
        if capsule.get(key) != expected:
            raise CapsuleError(
                f"capsule disclosure {key!r} must be {expected!r}, got {capsule.get(key)!r}"
            )

    _verify_capsule_digest(capsule)

    if dossier is None:
        return

    _verify_capsule_dossier_binding(capsule, Path(dossier))


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


def restore_workspace(capsule: dict[str, Any], projects_root: Path, dest: Path) -> Path:
    """Materialize the capsule's exact immutable version into a disposable dir.

    Reads **only** the immutable ProjectStore version the capsule names, through
    the store's own verification. It never reads the mutable ProjectStore head
    and never follows a symlink: substituting either would restore bytes the
    capsule does not vouch for.
    """
    from disco.tools.projects.store import ProjectStore, StorageStatus

    verify_capsule(capsule)
    boundary = capsule.get("boundary") or {}
    version_seq = boundary.get("workspace_version_seq")
    if version_seq is None:
        raise CapsuleError("capsule names no immutable workspace version")
    conversation_id = (capsule.get("parent") or {}).get("conversation_id")
    if not conversation_id:
        raise CapsuleError("capsule names no parent conversation")

    store = ProjectStore(str(projects_root))
    if store.status() != StorageStatus.OK:
        raise CapsuleError(f"project store is not usable at {projects_root}")

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    expected = capsule.get("workspace_byte_manifest") or {}
    written: dict[str, str] = {}

    try:
        with store.open_verified_version(conversation_id, int(version_seq)) as verified:
            for entry, data in verified.iter_bytes():
                target = dest / entry.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                written[entry.path] = _sha256_bytes(data)
    except CapsuleError:
        raise
    except Exception as exc:  # noqa: BLE001 — an unverifiable version restores nothing
        raise CapsuleError(
            f"the capsule's immutable version could not be freshly verified: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Every byte the capsule vouched for must be present and identical.
    for path, digest in expected.items():
        if path not in written:
            raise CapsuleError(f"restored workspace is missing {path!r}")
        if written[path] != digest:
            raise CapsuleError(
                f"restored {path!r} does not match the capsule byte manifest "
                f"(capsule {digest}, restored {written[path]})"
            )
    return dest


def write_capsule(capsule: dict[str, Any], out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / CAPSULE_NAME
    path.write_text(json.dumps(capsule, indent=2, sort_keys=True), encoding="utf-8")
    return path
