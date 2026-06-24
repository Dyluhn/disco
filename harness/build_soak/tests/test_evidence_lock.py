"""Evidence lock: manifest round-trip, hashing, tamper detection (guidelines §6, PR S1)."""

from __future__ import annotations

import json

from harness.build_soak.evidence import (
    EvidenceManifest,
    compute_evidence_hashes,
    load_manifest,
    sha256_text,
    verify_evidence_unchanged,
    write_manifest,
)


def _write_run(folder, events_text="{}\n"):
    (folder / "events.jsonl").write_text(events_text, encoding="utf-8")
    (folder / "state.final.json").write_text('{"status": "FINISHED"}', encoding="utf-8")
    files = {"events.jsonl": "events.jsonl", "state.final.json": "state.final.json"}
    hashes = compute_evidence_hashes(folder, files)
    manifest = EvidenceManifest(
        run_id="build_soak_2026_06_23_001",
        scenario_id="static_html_minimal",
        seed=12345,
        repo_commit="deadbeef",
        evidence_files=files,
        evidence_hashes=hashes,
    )
    write_manifest(folder, manifest)
    return manifest


def test_manifest_round_trips(tmp_path):
    manifest = _write_run(tmp_path)
    loaded = load_manifest(tmp_path)
    assert loaded == manifest
    # the on-disk JSON is the §6 shape
    raw = json.loads((tmp_path / "manifest.json").read_text())
    assert raw["run_id"] == "build_soak_2026_06_23_001"
    assert raw["seed"] == 12345
    assert set(raw["evidence_hashes"]) == {"events.jsonl", "state.final.json"}


def test_evidence_hashes_are_sha256_of_content():
    assert sha256_text("hello") == sha256_text("hello")
    assert sha256_text("hello") != sha256_text("world")
    assert sha256_text("hello").startswith("sha256:")


def test_intact_evidence_verifies(tmp_path):
    manifest = _write_run(tmp_path)
    result = verify_evidence_unchanged(tmp_path, manifest)
    assert result.intact
    assert bool(result) is True
    assert result.mismatches == {}


def test_tampered_evidence_is_detected(tmp_path):
    manifest = _write_run(tmp_path)
    # Mutate a hashed evidence file AFTER the freeze.
    (tmp_path / "events.jsonl").write_text('{"tampered": true}\n', encoding="utf-8")
    result = verify_evidence_unchanged(tmp_path, manifest)
    assert not result.intact
    assert "events.jsonl" in result.mismatches
    mm = result.mismatches["events.jsonl"]
    assert mm["expected"] != mm["actual"]


def test_deleted_evidence_is_detected(tmp_path):
    manifest = _write_run(tmp_path)
    (tmp_path / "state.final.json").unlink()
    result = verify_evidence_unchanged(tmp_path, manifest)
    assert not result.intact
    assert result.mismatches["state.final.json"]["actual"] == "MISSING"


def test_missing_file_hashes_to_sentinel(tmp_path):
    hashes = compute_evidence_hashes(tmp_path, {"absent": "nope.json"})
    assert hashes["absent"] == "MISSING"
