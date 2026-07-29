"""Moved timeout unhandled gate fail closed collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    clean_smoke_log,
    pytest,
    run_once,
)
from .helpers_01 import (
    FakeTransport,
    _seed_db,
    _smoke_scenario,
)


@pytest.mark.asyncio
async def _impl_test_poll_timeout_is_not_a_silent_pass(tmp_path):
    # The run never reaches a terminal/gate within the deadline -> poll TIMEOUT -> the
    # collected non-finished run classifies BUILD_DID_NOT_FINISH (not PASS).
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])  # no FINISHED
    transport = FakeTransport(
        db,
        states=["RUNNING"] * 6,  # never terminal
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_timeout_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=0.2,
    )
    assert record["status"] == "FAIL"
    assert record["code"] == "BUILD_DID_NOT_FINISH"


@pytest.mark.asyncio
async def _impl_test_unhandled_gate_does_not_hang_and_fails_closed(tmp_path):
    # A gate the runner does NOT act on (e.g. AWAITING_USER_DECISION — a gate the runner
    # has no scripted answer for) must NOT hang — the drive returns it, and the
    # non-finished run is judged (not a pass). (AWAITING_USER_QUESTION / WAITING_FOR_
    # CONFIRMATION are now HANDLED by Bug 17, covered by the clarify tests below.)
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    transport = FakeTransport(
        db,
        states=["RUNNING", "AWAITING_USER_DECISION", "AWAITING_USER_DECISION"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_gate_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=2,
    )
    assert record["status"] != "PASS"
    assert record["code"] == "BUILD_DID_NOT_FINISH"
