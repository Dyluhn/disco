"""Frozen run-folder loading and evidence assembly.

Reads the sealed dossier from disk, verifies evidence integrity, and assembles
the evidence kwargs that ``classify`` consumes. Tolerant by construction: a
missing or malformed slice yields ``None`` or sets ``evidence_intact = False``
rather than raising into the runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..evidence import EvidenceManifest, load_manifest, sha256_file, verify_evidence_unchanged
from ..product_evidence import PROVIDER_LEDGER_NAME

CLASSIFICATION_NAME = "classification.json"


def _read_jsonl(path: str | Path) -> list[Any]:
    out: list[Any] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _read_ledger(path: str | Path) -> list[dict[str, Any]]:
    """Tolerant provider-call-ledger reader: never crashes on a malformed line."""
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            out.append({"__malformed__": line[:160]})
            continue
        out.append(obj if isinstance(obj, dict) else {"__malformed__": str(obj)[:160]})
    return out


def _load_scenario_from_manifest(
    base: Path, manifest: EvidenceManifest
) -> tuple[dict[str, Any] | None, bool]:
    """Load and verify the scenario file referenced by the manifest."""
    evidence_intact = True
    scenario: dict[str, Any] | None = None
    scenario_rel = manifest.evidence_files.get("scenario.json")
    if not scenario_rel:
        return None, evidence_intact
    try:
        scenario_path = base / scenario_rel
        loaded_scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        if not isinstance(loaded_scenario, dict):
            raise ValueError("persisted scenario is not an object")
        if manifest.scenario_sha256 and sha256_file(scenario_path) != manifest.scenario_sha256:
            raise ValueError("persisted scenario hash does not match manifest")
        scenario = loaded_scenario
    except (OSError, json.JSONDecodeError, ValueError):
        evidence_intact = False
    return scenario, evidence_intact


def _load_workspace_manifest(
    base: Path, manifest: EvidenceManifest
) -> tuple[dict[str, Any] | None, bool]:
    """Load the workspace manifest referenced by the dossier."""
    evidence_intact = True
    workspace_manifest: dict[str, Any] | None = None
    workspace_rel = manifest.evidence_files.get("workspace-manifest.json")
    if not workspace_rel:
        return None, evidence_intact
    try:
        loaded_workspace = json.loads((base / workspace_rel).read_text(encoding="utf-8"))
        if not isinstance(loaded_workspace, dict):
            raise ValueError("workspace manifest is not an object")
        workspace_manifest = loaded_workspace
    except (OSError, json.JSONDecodeError, ValueError):
        evidence_intact = False
        workspace_manifest = None
    return workspace_manifest, evidence_intact


def _load_preview_evidence(
    base: Path, manifest: EvidenceManifest
) -> tuple[dict[str, Any] | None, bool]:
    """Load preview health/body/metadata evidence from the dossier."""
    evidence_intact = True
    preview: dict[str, Any] | None = None
    preview_health_rel = manifest.evidence_files.get("preview/health.json")
    preview_body_rel = manifest.evidence_files.get("preview/served.html")
    preview_meta_rel = manifest.evidence_files.get("preview/metadata.json")
    if not (preview_health_rel or preview_body_rel or preview_meta_rel):
        return None, evidence_intact
    try:
        if not preview_health_rel or not preview_body_rel:
            raise ValueError("preview evidence set is incomplete")
        loaded_health = json.loads((base / preview_health_rel).read_text(encoding="utf-8"))
        if not isinstance(loaded_health, dict):
            raise ValueError("preview health is not an object")
        metadata: dict[str, Any] = {"available": False, "source": "legacy_unrecorded"}
        if preview_meta_rel:
            loaded_metadata = json.loads((base / preview_meta_rel).read_text(encoding="utf-8"))
            if not isinstance(loaded_metadata, dict):
                raise ValueError("preview metadata is not an object")
            metadata = loaded_metadata
        preview = {
            **metadata,
            "health": loaded_health,
            "content": (base / preview_body_rel).read_text(encoding="utf-8"),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        evidence_intact = False
        preview = None
    return preview, evidence_intact


def _load_provider_ledger(
    base: Path, manifest: EvidenceManifest | None
) -> list[dict[str, Any]] | None:
    """Load the provider-call ledger if present in the run folder."""
    ledger_rel = (
        manifest.evidence_files.get(PROVIDER_LEDGER_NAME)
        or manifest.evidence_files.get("provider_ledger")
        if manifest
        else None
    )
    ledger_path = base / ledger_rel if ledger_rel else base / PROVIDER_LEDGER_NAME
    if manifest is None and not ledger_path.is_file():
        ledger_path = next(iter(base.rglob(PROVIDER_LEDGER_NAME)), ledger_path)
    if (manifest is None or ledger_rel is not None) and ledger_path.is_file():
        return _read_ledger(ledger_path)
    return None


def _load_inspect_trace(base: Path) -> dict[str, Any] | None:
    """Load the inspect trace if present in the run folder."""
    trace_path = base / "inspect-trace.json"
    if not trace_path.is_file():
        trace_path = next(iter(base.rglob("inspect-trace.json")), trace_path)
    if not trace_path.is_file():
        return None
    try:
        loaded_trace = json.loads(trace_path.read_text(encoding="utf-8"))
        return loaded_trace if isinstance(loaded_trace, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _load_product_evidence(base: Path) -> dict[str, Any] | None:
    """Load the browser product-harness evidence dossier if present."""
    pe_path = base / "product-evidence.json"
    if not pe_path.is_file():
        pe_path = next(iter(base.rglob("product-evidence.json")), pe_path)
    if not pe_path.is_file():
        return None
    try:
        loaded = json.loads(pe_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _load_browser_evidence_paths(
    base: Path,
    manifest: EvidenceManifest,
    folder_conversation_id: str,
) -> set[str]:
    """H191: only manifest-named browser evidence is admissible on frozen replay."""
    browser_evidence_paths: set[str] = set()
    prefix = "browser-evidence/"
    for label, rel in manifest.evidence_files.items():
        if not label.startswith(prefix) or label == prefix:
            continue
        proof_path = label.removeprefix(prefix)
        expected_rel = f"conversations/{folder_conversation_id}/browser-evidence/{proof_path}"
        if (
            folder_conversation_id
            and rel == expected_rel
            and label in manifest.evidence_hashes
            and manifest.evidence_hashes[label] != "MISSING"
        ):
            browser_evidence_paths.add(proof_path)
    return browser_evidence_paths


def _resolve_events_path(base: Path, manifest: EvidenceManifest | None) -> Path:
    """Find the events.jsonl path in the run folder."""
    events_rel = (manifest.evidence_files.get("events") if manifest else None) or "events.jsonl"
    events_path = base / events_rel
    if not events_path.is_file():
        for candidate in base.rglob("events.jsonl"):
            events_path = candidate
            break
    return events_path


def _load_manifest_evidence(
    base: Path,
    manifest: EvidenceManifest | None,
    folder_conversation_id: str,
    scenario: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, set[str], bool]:
    """Load manifest-gated evidence: scenario, workspace, preview, browser paths.

    Returns (scenario, workspace_manifest, preview, browser_evidence_paths, evidence_intact).
    """
    evidence_intact = True
    if manifest is not None:
        loaded_scenario, scenario_intact = _load_scenario_from_manifest(base, manifest)
        if loaded_scenario is not None:
            if scenario is not None and scenario != loaded_scenario:
                evidence_intact = False
            scenario = loaded_scenario
        evidence_intact = evidence_intact and scenario_intact

    workspace_manifest: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    if manifest is not None:
        wm, wm_intact = _load_workspace_manifest(base, manifest)
        workspace_manifest = wm
        evidence_intact = evidence_intact and wm_intact

        pv, pv_intact = _load_preview_evidence(base, manifest)
        preview = pv
        evidence_intact = evidence_intact and pv_intact

    browser_evidence_paths: set[str] = set()
    if manifest is not None:
        browser_evidence_paths = _load_browser_evidence_paths(
            base, manifest, folder_conversation_id
        )
    return scenario, workspace_manifest, preview, browser_evidence_paths, evidence_intact


def load_run_folder(
    folder: str | Path, *, scenario: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Load a frozen run folder and assemble all evidence for classification.

    Returns a dict with keys: ``events_raw``, ``scenario``, ``evidence_intact``,
    ``workspace_manifest``, ``preview``, ``provider_ledger``, ``inspect_trace``,
    ``product_evidence``, ``browser_evidence_paths``, ``run_id``,
    ``conversation_id``, ``commit``, ``seed``, ``autonomous``.
    """
    base = Path(folder)
    manifest: EvidenceManifest | None = None
    manifest_intact = True
    try:
        manifest = load_manifest(base)
        integrity = verify_evidence_unchanged(base, manifest)
        manifest_intact = integrity.intact
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        manifest = None

    events_path = _resolve_events_path(base, manifest)
    events_raw: list[Any] = []
    if events_path.is_file():
        events_raw = _read_jsonl(events_path)
    folder_conversation_id = ""
    if events_path.name == "events.jsonl" and events_path.parent.parent.name == "conversations":
        folder_conversation_id = events_path.parent.name

    scenario, workspace_manifest, preview, browser_evidence_paths, evidence_intact = (
        _load_manifest_evidence(base, manifest, folder_conversation_id, scenario)
    )
    evidence_intact = evidence_intact and manifest_intact

    provider_ledger = _load_provider_ledger(base, manifest)
    inspect_trace = _load_inspect_trace(base)
    product_evidence = _load_product_evidence(base)

    return {
        "events_raw": events_raw,
        "scenario": scenario,
        "evidence_intact": evidence_intact,
        "workspace_manifest": workspace_manifest,
        "preview": preview,
        "provider_ledger": provider_ledger,
        "inspect_trace": inspect_trace,
        "product_evidence": product_evidence,
        "browser_evidence_paths": browser_evidence_paths,
        "run_id": manifest.run_id if manifest else base.name,
        "conversation_id": folder_conversation_id,
        "commit": manifest.repo_commit if manifest else "",
        "seed": manifest.seed if manifest else None,
        "autonomous": manifest.autonomous if manifest else None,
    }


def write_classification(folder: str | Path, classification: dict[str, Any]) -> None:
    """Write the classification dict into the run folder."""
    base = Path(folder)
    (base / CLASSIFICATION_NAME).write_text(
        json.dumps(classification, indent=2, sort_keys=True), encoding="utf-8"
    )
