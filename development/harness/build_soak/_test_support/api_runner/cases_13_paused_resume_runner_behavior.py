"""Moved paused resume runner behavior collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    assemble_dossier,
    classify_dossier,
    clean_smoke_log,
    drive_scenario,
    pytest,
    run_once,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _seed_db,
    _smoke_scenario,
)


@pytest.mark.asyncio
async def _impl_test_paused_then_finished_resumes_to_terminal(tmp_path):
    # The runner ACTS AS THE USER: a PAUSED (actionless) run is RESUMABLE, so the drive
    # resumes it and proceeds to the real terminal.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["RUNNING", "PAUSED", "RUNNING", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=2)
    # it issued a resume (POST /resume)
    assert any(p[0].endswith("/resume") for p in transport.posts)
    base = assemble_dossier(
        tmp_path / "out", "run_resume_001", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification


@pytest.mark.asyncio
async def _impl_test_paused_forever_is_bounded_then_build_did_not_finish(tmp_path):
    # A build that just keeps actionless-pausing is resumed a BOUNDED number of times,
    # then the non-finished run classifies BUILD_DID_NOT_FINISH — never a silent pass,
    # never an infinite resume spin.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])  # no FINISHED status
    transport = FakeTransport(
        db,
        states=["RUNNING"] + ["PAUSED"] * 12,
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = _smoke_scenario()
    record = await run_once(
        client,
        scenario,
        run_id="run_paused_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=0.3,
    )
    resumes = sum(1 for p in transport.posts if p[0].endswith("/resume"))
    assert resumes == 3  # _MAX_RESUMES — bounded
    assert record["status"] == "FAIL"
    assert record["code"] == "BUILD_DID_NOT_FINISH"
