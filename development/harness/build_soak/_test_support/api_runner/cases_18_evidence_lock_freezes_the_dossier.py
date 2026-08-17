"""Moved evidence lock freezes the dossier collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    assemble_dossier,
    classify_dossier,
    classify_run_folder,
    clean_smoke_log,
    drive_scenario,
    json,
    load_manifest,
    pytest,
    verify_evidence_unchanged,
)
from .helpers_01 import (
    FakeTransport,
    _CanonicalPreviewTransport,
    _client,
    _plant_snapshot,
    _seed_db,
    _smoke_scenario,
)


@pytest.mark.asyncio
async def _impl_test_preview_dossier_is_evidence_locked(tmp_path):
    # codex P1#1: the PREVIEW dossier is adjudicated truth — it MUST be under the §6
    # hash lock, so tampered content, health, or provenance trips INVALID_RUN.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out", "run_pvlock_001", scenario, run, model="m", autonomous=False
    )
    manifest = load_manifest(base)
    # the preview files ARE in the locked set
    assert "preview/served.html" in manifest.evidence_hashes
    assert "preview/health.json" in manifest.evidence_hashes
    assert "preview/metadata.json" in manifest.evidence_hashes
    assert verify_evidence_unchanged(base, manifest).intact
    metadata_path = base / "conversations" / _CID / "preview" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["source"] == "isolated_path_capability"
    # A hidden-fallback provenance rewrite is evidence tampering and trips the lock.
    metadata["source"] = "snapshot_serve_probe"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    integrity = verify_evidence_unchanged(base, manifest)
    assert not integrity.intact
    assert "preview/metadata.json" in integrity.mismatches


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preview_status", "preview_body", "expected_status", "expected_code"),
    [
        (200, "<h1>Build Smoke OK</h1>", "PASS", None),
        (403, "preview capability required", "FAIL", "FALSE_FINISH_PREVIEW_BROKEN"),
        (200, "<h1>WRONG</h1>", "FAIL", "PREVIEW_TRUTH_MISMATCH"),
    ],
)
async def _impl_test_frozen_dossier_replays_workspace_preview_and_provenance(
    tmp_path,
    preview_status,
    preview_body,
    expected_status,
    expected_code,
):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _seed_db(db, _CID, clean_smoke_log())
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    transport = _CanonicalPreviewTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"],
        preview_status=preview_status,
        preview_body=preview_body,
    )
    client = DiscoApiClient(
        transport,
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
    )
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out",
        f"replay-{preview_status}-{expected_status}",
        scenario,
        run,
        model="m",
        autonomous=False,
    )

    original = classify_dossier(base, scenario, run, autonomous=False)
    replayed = classify_run_folder(base, scenario=scenario)

    assert original["status"] == expected_status
    assert replayed["status"] == expected_status
    assert replayed.get("code") == expected_code
