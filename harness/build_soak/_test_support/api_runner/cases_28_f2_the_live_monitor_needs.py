"""Moved f2 the live monitor needs collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    INACTIVE_TIMEOUT,
    LIVE_THRASH_STOP,
    UTC,
    _run_mod,
    action,
    assemble_dossier,
    classify_dossier,
    clean_smoke_log,
    datetime,
    drive_scenario,
    msg,
    observation,
    plan,
    pytest,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _seed_db,
    _smoke_scenario,
)
from .helpers_02 import (
    _same_signature_refusals,
    _thrash_smoke_scenario,
)


def _impl_test_live_thrash_monitor_kills_only_on_current_no_progress_evidence(tmp_path):
    """k6g F2.4: a finding superseded by trusted progress cannot kill the run;
    the same finding with no later progress still can."""
    scenario = _thrash_smoke_scenario()

    current = _same_signature_refusals()
    client = _client(FakeTransport(tmp_path / "current.db", states=["RUNNING"]), tmp_path)
    client.enable_live_thrash_monitor(scenario)
    assert not client.observe_live_thrash_snapshot(current, None, terminal_status="RUNNING")
    assert client.observe_live_thrash_snapshot(current, None, terminal_status="RUNNING")
    finding = client.live_thrash_monitor["findings"][0]["oracle_results"][0]
    assert finding["code"] == "TOOL_ERROR_THRASH"
    assert finding["facts"]["action_seqs"] == [11, 13]

    # Identical history + a trusted changed-state receipt AFTER the crossing
    # (the k6g resume shape): the historical finding is no longer current.
    resumed = list(current)
    write_id = "a15"
    resumed.append(action(15, "file_edit", action_id=write_id, args={"path": "index.html"}))
    write_result = observation(16, write_id, tool="file_edit", success=True)
    write_result["tool_result"]["structured"] = {"path": "index.html", "sha256": "b" * 64}
    resumed.append(write_result)
    stale_client = _client(FakeTransport(tmp_path / "stale.db", states=["RUNNING"]), tmp_path)
    stale_client.enable_live_thrash_monitor(scenario)
    assert not stale_client.observe_live_thrash_snapshot(resumed, None, terminal_status="RUNNING")
    assert not stale_client.observe_live_thrash_snapshot(resumed, None, terminal_status="RUNNING")
    assert stale_client.live_thrash_monitor["findings"] == []


@pytest.mark.asyncio
async def _impl_test_superseded_stuck_terminal_does_not_force_strict_snapshot(
    tmp_path, monkeypatch
):
    """k6g F2 terminal ownership: a STUCK that was answered and superseded
    (question -> user answer -> RUNNING -> monitor kill) is NOT the run's work
    terminal; the confirmed live-thrash carve-out must engage and preserve the
    nonterminal evidence slice instead of raising WORKSPACE_SNAPSHOT_NOT_READY."""
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    events = [
        msg(1, "user", "build a page"),
        status(2, "RUNNING"),
        plan(3, revision=1),
        status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
        status(5, "RUNNING", "plan_approved"),
        status(6, "STUCK", "stuck_escape_tool_quarantine"),
        status(7, "AWAITING_USER_QUESTION", "evt_q"),
        msg(8, "user", "Use your best judgment and proceed."),
        status(9, "RUNNING"),
        status(10, "IDLE", "killed"),
    ]
    for offset, event in enumerate(events):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    transport = FakeTransport(db, states=["IDLE"])
    client = _client(transport, tmp_path)
    scenario = _thrash_smoke_scenario()

    async def thrash_drive(*_args, **_kwargs):
        # drive_scenario re-enables (resets) the monitor before driving, so the
        # confirmed finding is primed exactly where the real monitor records
        # it: during the drive, before the LIVE_THRASH_STOP return.
        client._live_thrash_samples = 2  # noqa: SLF001 - primed strict monitor state
        client._live_thrash_findings = [  # noqa: SLF001
            {
                "detected_at_epoch": started.timestamp() + 8.5,
                "terminal_status": "RUNNING",
                "event_count": 9,
                "max_event_seq": 9,
                "confirmation_samples": 2,
                "oracle_results": [
                    {
                        "oracle": "ThrashOracle",
                        "status": "FAIL",
                        "code": "TOOL_ERROR_THRASH",
                    }
                ],
            }
        ]
        return LIVE_THRASH_STOP

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", thrash_drive)
    run = await drive_scenario(client, scenario, model="m", autonomous=False)

    capture = run.workspace_manifest.get("_capture") or {}
    assert capture.get("status") == "not_collected_nonterminal"
    assert capture.get("drive_status") == LIVE_THRASH_STOP


@pytest.mark.asyncio
async def _impl_test_current_stuck_terminal_preserves_product_failure_without_finished_seal(
    tmp_path, monkeypatch
):
    """A durable STUCK is a product outcome, not a snapshot-flush invalidation.

    Strict workspace collection requires an exact FINISHED seal, so invoking it
    for STUCK deterministically waits and masks the real failure as
    WORKSPACE_SNAPSHOT_NOT_READY.
    """
    events = clean_smoke_log()
    events[-1] = status(10, "STUCK", "verifier_no_progress")
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    transport = FakeTransport(db, states=["STUCK"])
    client = _client(transport, tmp_path, require_workspace_commit=True)

    async def strict_workspace_must_not_run(*_args, **_kwargs):
        raise AssertionError("STUCK has no successful final workspace seal")

    monkeypatch.setattr(client, "collect_workspace", strict_workspace_must_not_run)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False)

    assert run.workspace_manifest["_capture"] == {
        "drive_status": "STUCK",
        "reason": "strict final workspace seal exists only for successful completion",
        "status": "not_collected_failed_terminal",
    }
    base = assemble_dossier(
        tmp_path / "out", "run_current_stuck", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "FAIL", classification
    # A11 §2: the stuck valve is its own shape, not the shared TOOL_CALL_THRASH
    # name it used to share with the three repetition shapes.
    assert classification["code"] == "TOOL_CALL_THRASH_STUCK_VALVE"
    assert classification["first_broken_link"] == "model_turns -> bounded_stuck_valve"
    assert classification["shape"] == "bounded_stuck_valve"


@pytest.mark.asyncio
async def _impl_test_v4_bare_idle_after_late_finished_keeps_the_strict_path(tmp_path, monkeypatch):
    """Verifier V4: a resting/stop IDLE (any detail) is not product
    supersession — a genuine late FINISHED still routes to strict capture."""
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    events = clean_smoke_log()
    events.append(status(11, "IDLE"))
    for offset, event in enumerate(events):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    content = "<html><h1>Build Smoke OK</h1></html>"
    transport = FakeTransport(
        db, states=["IDLE"], workspace={"index.html": content}, preview_html=content
    )
    client = _client(transport, tmp_path)

    workspace_calls = 0

    async def counting_workspace(_conversation_id, _declared_paths):
        nonlocal workspace_calls
        workspace_calls += 1
        return {"files": {}}

    async def inactive_drive(*_args, **_kwargs):
        return INACTIVE_TIMEOUT

    monkeypatch.setattr(client, "collect_workspace", counting_workspace)
    monkeypatch.setattr(_run_mod, "_drive_to_terminal", inactive_drive)
    run = await drive_scenario(client, _smoke_scenario(), model="m", autonomous=False)

    assert workspace_calls == 1
    assert (run.workspace_manifest or {}).get("_capture") is None
