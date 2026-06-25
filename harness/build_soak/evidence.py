"""Evidence lock (guidelines §5, §6).

Every run writes an immutable `manifest.json` carrying run metadata + SHA256 hashes
of the durable evidence files. After classification the run folder is FROZEN: if any
hashed evidence changes, the run becomes INVALID_RUN (the repair loop may copy a
frozen folder but never mutate it).

This module is pure plumbing — hashing, manifest build/load, and a tamper check —
with no product dependency.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class EvidenceManifest:
    """The §6 manifest shape. Only `run_id`, `scenario_id`, and `evidence_hashes`
    are load-bearing for the tamper check; the rest is provenance metadata."""

    run_id: str
    scenario_id: str
    scenario_sha256: str = ""
    seed: int | None = None
    repo_commit: str = ""
    repo_dirty: bool = False
    model: str = ""
    provider: str = ""
    assist: bool = False
    autonomous: bool = False
    surface: str = "build"
    kernel: str = "disco"  # EPIC K bake-off: which Build kernel drove this run (disco | pi)
    mode: str = "fake_model"  # api | ui | fake_model
    started_at: str = ""
    finished_at: str = ""
    evidence_files: dict[str, str] = field(default_factory=dict)
    evidence_hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "scenario_sha256": self.scenario_sha256,
            "seed": self.seed,
            "repo_commit": self.repo_commit,
            "repo_dirty": self.repo_dirty,
            "model": self.model,
            "provider": self.provider,
            "assist": self.assist,
            "autonomous": self.autonomous,
            "surface": self.surface,
            "kernel": self.kernel,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "evidence_files": dict(self.evidence_files),
            "evidence_hashes": dict(self.evidence_hashes),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvidenceManifest:
        missing = {"run_id", "scenario_id"} - set(raw)
        if missing:
            raise ValueError(f"manifest missing required keys: {sorted(missing)}")
        return cls(
            run_id=str(raw["run_id"]),
            scenario_id=str(raw["scenario_id"]),
            scenario_sha256=str(raw.get("scenario_sha256", "")),
            seed=raw.get("seed"),
            repo_commit=str(raw.get("repo_commit", "")),
            repo_dirty=bool(raw.get("repo_dirty", False)),
            model=str(raw.get("model", "")),
            provider=str(raw.get("provider", "")),
            assist=bool(raw.get("assist", False)),
            autonomous=bool(raw.get("autonomous", False)),
            surface=str(raw.get("surface", "build")),
            kernel=str(raw.get("kernel", "disco")),
            mode=str(raw.get("mode", "fake_model")),
            started_at=str(raw.get("started_at", "")),
            finished_at=str(raw.get("finished_at", "")),
            evidence_files=dict(raw.get("evidence_files") or {}),
            evidence_hashes=dict(raw.get("evidence_hashes") or {}),
        )


def compute_evidence_hashes(folder: str | Path, files: dict[str, str]) -> dict[str, str]:
    """Hash each named evidence file (relative to `folder`). `files` maps a label
    (e.g. "events.jsonl") to a folder-relative path. A missing file hashes to the
    sentinel "MISSING" so the manifest still records its absence (which the tamper
    check and harness-validity oracle both treat as evidence-incomplete)."""
    base = Path(folder)
    out: dict[str, str] = {}
    for label, rel in files.items():
        p = base / rel
        out[label] = sha256_file(p) if p.is_file() else "MISSING"
    return out


def write_manifest(folder: str | Path, manifest: EvidenceManifest) -> Path:
    base = Path(folder)
    base.mkdir(parents=True, exist_ok=True)
    path = base / MANIFEST_NAME
    path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_manifest(folder: str | Path) -> EvidenceManifest:
    base = Path(folder)
    raw = json.loads((base / MANIFEST_NAME).read_text(encoding="utf-8"))
    return EvidenceManifest.from_dict(raw)


@dataclass(frozen=True)
class IntegrityResult:
    intact: bool
    mismatches: dict[str, dict[str, str]] = field(default_factory=dict)

    def __bool__(self) -> bool:  # truthy == intact
        return self.intact


def verify_evidence_unchanged(
    folder: str | Path, manifest: EvidenceManifest
) -> IntegrityResult:
    """Re-hash the manifest's evidence files and compare to the frozen hashes.

    Returns intact=False (with the per-file expected/actual) when ANY hashed file
    changed, went missing, or newly appeared — that is the §6 INVALID_RUN trigger.
    A manifest that recorded a hash for a label whose `evidence_files` path is now
    absent is a mismatch; a label hashed "MISSING" that is still missing is intact
    (the absence itself was frozen)."""
    base = Path(folder)
    mismatches: dict[str, dict[str, str]] = {}
    for label, expected in manifest.evidence_hashes.items():
        rel = manifest.evidence_files.get(label, label)
        p = base / rel
        actual = sha256_file(p) if p.is_file() else "MISSING"
        if actual != expected:
            mismatches[label] = {"expected": expected, "actual": actual}
    return IntegrityResult(intact=not mismatches, mismatches=mismatches)
