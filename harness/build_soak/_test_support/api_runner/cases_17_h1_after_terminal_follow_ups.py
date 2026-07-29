"""Moved h1 after terminal follow ups collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    FOLLOWUP_PICKED_UP,
    FOLLOWUP_PICKUP_TIMEOUT,
    FOLLOWUP_REPLANNED,
    DiscoApiClient,
    action,
    asyncio,
    clean_smoke_log,
    drive_scenario,
    load_scenarios,
    msg,
    plan,
    pytest,
    run_once,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _CancelAtRecoveryTransport,
    _CancelMissedWindowTransport,
    _client,
    _DeadWindowTransport,
    _insert_event,
    _observation_for_action,
    _ScriptStateTransport,
    _seed_db,
    _smoke_scenario,
)


@pytest.mark.asyncio
async def _impl_test_wait_for_first_file_write_requires_successful_write_family_observation(
    tmp_path,
):
    db = tmp_path / "disco.db"
    shell = action(2, "shell", args={"cmd": "touch index.html"}, action_id="act_shell")
    verify = action(4, "verify_web_app", action_id="act_verify")
    preview = action(6, "preview", action_id="act_preview")
    failed_write = action(
        8,
        "file_write",
        args={"path": "index.html", "content": "bad"},
        action_id="act_failed_write",
    )
    good_write = action(
        10,
        "exact_replace",
        args={"path": "index.html", "old": "bad", "new": "good"},
        action_id="act_good_write",
    )
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            shell,
            _observation_for_action(3, shell),
            verify,
            _observation_for_action(5, verify),
            preview,
            _observation_for_action(7, preview),
            failed_write,
            _observation_for_action(9, failed_write, success=False),
            good_write,
            _observation_for_action(11, good_write),
        ],
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["RUNNING"]),
        db_path=str(db),
        poll_interval_s=0.0,
    )

    assert await client.wait_for_first_file_write(_CID, timeout_s=0.1) == 10


@pytest.mark.asyncio
async def _impl_test_wait_for_followup_pickup_detects_terminal_exit(tmp_path):
    # Pickup #1 signal: the build LEAVES its prior terminal status. The wait must NOT return
    # on the stale FINISHED (status == baseline) — it holds until the status actually changes.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _ScriptStateTransport(db, states=["FINISHED", "FINISHED", "RUNNING"])
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    baseline = await client.capture_followup_baseline(_CID)  # status=FINISHED (1 read)
    assert baseline["status"] == "FINISHED"
    result = await client.wait_for_followup_pickup(_CID, baseline, timeout_s=5.0)
    assert result == FOLLOWUP_PICKED_UP
    # It WAITED through the stale-FINISHED reads instead of returning on the first one.
    assert transport.state_reads >= 3


@pytest.mark.asyncio
async def _impl_test_wait_for_followup_pickup_replan_bump_not_user_append(tmp_path):
    # Pickup #2 signal: a re-plan (a NEW plan event whose revision bumps above baseline).
    # The RED HERRING the bug rode on — a new event seq from the user-message APPEND — must
    # NOT count as pickup: the append at read 1 bumps the seq but is not processing, so the
    # wait keeps polling until the genuine plan-revision bump lands at read 3.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())  # latest plan revision == 1

    def on_read(n):
        if n == 1:
            _insert_event(db, _CID, msg(11, "user", "now revise it"))  # seq bump ONLY
        elif n == 3:
            _insert_event(db, _CID, plan(12, revision=2))  # the real re-plan

    transport = _ScriptStateTransport(db, states=["FINISHED"], on_read=on_read)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    baseline = {"status": "FINISHED", "plan_revision": 1, "max_seq": 10}
    result = await client.wait_for_followup_pickup(_CID, baseline, timeout_s=5.0)
    assert result == FOLLOWUP_REPLANNED
    # The user-append at read 1 did NOT short-circuit it; it waited for the rev bump at read 3.
    assert transport.state_reads >= 3


@pytest.mark.asyncio
async def _impl_test_wait_for_followup_pickup_fires_on_progress_event_status_stuck_finished(
    tmp_path,
):
    # THE V2 DEAD-WINDOW (why V1 failed): a follow-up appended during run finalization causes
    # NO status change — the status STAYS FINISHED the whole time. V1 watched only the status,
    # saw nothing, and timed out. V2 detects pickup from the EVENT LOG: the first non-user
    # progress event (an action here, NOT a re-plan) past the baseline seq is pickup, even
    # though the status never leaves FINISHED. The user-message append at read 1 must NOT count.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())  # max seq 10, no re-plan

    def on_read(n):
        if n == 1:
            _insert_event(db, _CID, msg(11, "user", "now revise it"))  # the append — NOT pickup
        elif n == 3:
            _insert_event(db, _CID, action(12, "shell"))  # real progress; status STILL FINISHED

    transport = _ScriptStateTransport(db, states=["FINISHED"], on_read=on_read)  # never changes
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    baseline = {"status": "FINISHED", "plan_revision": 1, "max_seq": 10}
    result = await client.wait_for_followup_pickup(_CID, baseline, timeout_s=5.0)
    assert result == FOLLOWUP_PICKED_UP  # detected from the event seq, not a status change
    # It did NOT short-circuit on the bare user-append at read 1; it waited for the real
    # progress event at read 3 — proving the append red herring is excluded.
    assert transport.state_reads >= 3


@pytest.mark.asyncio
async def _impl_test_wait_for_followup_pickup_is_bounded_and_returns_on_timeout(tmp_path):
    # No pickup ever (status frozen at the prior FINISHED, no re-plan): the wait is BOUNDED
    # and RETURNS the timeout sentinel rather than hanging — the caller then proceeds.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _ScriptStateTransport(db, states=["FINISHED"])  # never leaves terminal
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    baseline = await client.capture_followup_baseline(_CID)
    # asyncio.wait_for is the anti-hang guard: a real hang would raise TimeoutError here.
    result = await asyncio.wait_for(
        client.wait_for_followup_pickup(_CID, baseline, timeout_s=0.05), timeout=5.0
    )
    assert result == FOLLOWUP_PICKUP_TIMEOUT


@pytest.mark.asyncio
async def _impl_test_drive_does_not_return_on_stale_terminal_below_min_seq(tmp_path):
    # THE PIECE V1 MISSED (V2 min_seq guard): the drive must NOT return on the STALE
    # pre-follow-up terminal (its event seq <= min_seq). The status reads FINISHED the whole
    # time and the stale terminal sits at seq 10; only when the follow-up's OWN new terminal
    # event (seq 20 > min_seq) lands does the wait return. (a) in the V2 plan.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())  # stale terminal: FINISHED at seq 10

    def on_read(n):
        if n == 4:
            # the follow-up's OWN new terminal lands only at read 4, at a higher seq
            _insert_event(db, _CID, status(20, "FINISHED"))

    transport = _ScriptStateTransport(db, states=["FINISHED"], on_read=on_read)  # never changes
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    result = await client.poll_until_terminal(
        _CID, inactivity_s=5.0, hard_cap_s=10.0, min_terminal_seq=10
    )
    assert result == "FINISHED"
    # It WAITED past the stale-terminal reads (1-3, seq 10 <= min) and returned only once the
    # follow-up's own new terminal (seq 20 > min) appeared at read 4 — never on the stale one.
    assert transport.state_reads >= 4


@pytest.mark.asyncio
async def _impl_test_drive_with_no_min_seq_returns_on_first_terminal(tmp_path):
    # The default (non-follow-up) drive is UNCHANGED: with min_terminal_seq=None any terminal
    # ends the wait immediately — the guard only engages for after-terminal follow-ups.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _ScriptStateTransport(db, states=["FINISHED"])
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    result = await client.poll_until_terminal(_CID, inactivity_s=5.0, hard_cap_s=10.0)
    assert result == "FINISHED"
    assert transport.state_reads == 1  # returned on the very first terminal read


@pytest.mark.asyncio
async def _impl_test_after_terminal_followups_are_serialized(tmp_path):
    # THE H1/V2 pin: revise_after_finish has TWO after_terminal follow-ups. With the FAITHFUL
    # dead-window fake (status stuck FINISHED; pickup + the new terminal only show as higher-seq
    # EVENTS), the runner must (1) detect pickup of follow-up 1 from the event seq, (2) drive it
    # to its OWN new terminal (seq > baseline), and only THEN (3) send follow-up 2 — else they
    # pile up and collapse into a single plan revision (false PLAN_REVISION_NOT_INCREMENTED).
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _DeadWindowTransport(db, stale_reads=2, work_reads=2)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = load_scenarios()["revise_after_finish"]
    assert sum(f.get("trigger") == "after_terminal" for f in scenario["followups"]) == 2

    await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)

    # Both follow-ups were sent, in order, exactly once each (no re-kick needed — pickup fired).
    sends = [f for f in transport.ws_frames if f.get("type") == "send_message"]
    assert len(sends) == 2
    assert len(transport.send_log) == 2
    assert len(transport.pickup_log) == 2
    assert len(transport.terminal_log) == 2
    send1_at = transport.send_log[0][1]
    pickup1_at = transport.pickup_log[0][1]
    terminal1_at = transport.terminal_log[0][1]
    send2_at = transport.send_log[1][1]
    # Serialization with the V2 min_seq guard: follow-up 1 was PICKED UP, then reached its OWN
    # new terminal, BOTH strictly BEFORE follow-up 2 was sent.
    assert send1_at < pickup1_at < terminal1_at < send2_at
    # And the runner genuinely WAITED through the stale-FINISHED reads (didn't return on the
    # first stale poll) — proof it never read the prior terminal as instant pickup/terminal.
    assert pickup1_at - send1_at > 1
    # The two re-plans were detected from distinct higher-seq plan events (revisions 2 then 3).
    assert client._latest_plan_revision(_CID) == 3


def _cancel_at_recovery_scenario():
    return {
        "id": "disconnect_cancel_recovery",
        "mode": "api",
        "prompt": (
            "Create index.html for 'Beacon Status' with a services table containing "
            "the exact cell text 'All systems nominal'."
        ),
        "cancel_at": {"trigger": "after_first_file_write"},
        "followups": [
            {
                "text": "Continue and finish the page exactly as originally requested.",
                "requires_plan_revision": True,
                "trigger": "after_terminal",
            }
        ],
        "assertions": {
            "workspace": {
                "files": [
                    {
                        "path": "index.html",
                        "must_contain": ["Beacon Status", "All systems nominal"],
                    }
                ]
            },
            "terminal_status_in": ["FINISHED", "VERIFIED"],
        },
    }


def _assert_cancel_recovery_effects(transport) -> None:
    kills = [p for p in transport.posts if p[0] == f"/conversations/{_CID}/kill"]
    sends = [f for f in transport.ws_frames if f.get("type") == "send_message"]
    assert len(kills) == 1
    assert len(sends) == 1
    assert transport.idle_reads_before_followup >= 2
    assert transport.kill_log[0] < transport.send_log[0] < transport.pickup_log[0]
    assert transport.pickup_log[0] < transport.terminal_log[0]


def _assert_cancel_recovery_result(run) -> None:
    assert run.state_final["execution_status"] == "FINISHED"
    assert run.workspace_manifest["index.html"]["present"] is True
    assert run.declared_followup_seqs
    assert run.declared_followup_requires_revision == [True]
    assert any("cancel_at fired at after_first_file_write" in t for t in run.timeline)
    assert any("cancel_at settled to stable IDLE" in t for t in run.timeline)


@pytest.mark.asyncio
async def _impl_test_cancel_at_after_first_file_write_kills_then_followup_recovers(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build Beacon Status"),
            status(2, "RUNNING"),
            plan(3, revision=1),
            status(4, "AWAITING_PLAN_APPROVAL", "evt_3"),
            status(5, "RUNNING", "plan_approved"),
        ],
    )
    transport = _CancelAtRecoveryTransport(db)
    client = _client(transport, tmp_path)
    run = await drive_scenario(
        client,
        _cancel_at_recovery_scenario(),
        model="m",
        autonomous=False,
        timeout_s=5,
    )
    _assert_cancel_recovery_effects(transport)
    _assert_cancel_recovery_result(run)


@pytest.mark.asyncio
async def _impl_test_cancel_at_after_first_file_write_terminal_race_is_invalid_run(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            status(2, "RUNNING"),
            plan(3, revision=1),
            status(4, "RUNNING", "plan_approved"),
            status(5, "RUNNING"),
        ],
    )
    transport = _CancelMissedWindowTransport(db)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = {**_smoke_scenario(), "cancel_at": {"trigger": "after_first_file_write"}}

    record = await run_once(
        client,
        scenario,
        run_id="run_cancel_missed_window_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=1.0,
        hard_cap_s=5.0,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "CANCEL_MISSED_WINDOW"
    assert record["facts"]["terminal_seq"] > record["facts"]["trigger_seq"]
    # One kill is still expected from final runner hygiene; a second one would be the
    # invalid post-finish cancel trigger this test forbids.
    kills = [p for p in transport.posts if p[0] == f"/conversations/{_CID}/kill"]
    assert len(kills) == 1


@pytest.mark.asyncio
async def _impl_test_followup_pickup_timeout_hard_fails_invalid_run(tmp_path, monkeypatch):
    # (b) in the REVISED V2 plan: when the dead-window NEVER resolves (status stuck FINISHED, no
    # progress event EVER), the runner must NOT silently proceed to drive on the stale terminal
    # — that is exactly what let follow-up 2 collapse into follow-up 1 in V1. It HARD-FAILS as a
    # sequencing failure → INVALID_RUN, never a (false) product PASS, and critically it sends the
    # follow-up EXACTLY ONCE (NO re-send — a second send would DUPLICATE the user turn, the live
    # bug this revision removes) and sends NO subsequent follow-up (no pile-up). Bound via env.
    monkeypatch.setenv("DISCO_SOAK_PICKUP_TIMEOUT_S", "0.02")
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(  # status forever FINISHED, no events ever appended → no pickup
        db,
        states=["FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = load_scenarios()["revise_after_finish"]  # TWO after_terminal follow-ups
    record = await run_once(
        client,
        scenario,
        run_id="run_pickup_to_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=2,
    )
    assert record["status"] == "INVALID_RUN"  # sequencing failure, NOT a silent proceed/PASS
    assert record["code"] == "RUN_INTERRUPTED"
    # EXACTLY ONE send for the FIRST follow-up: no re-send (no duplicate turn) AND the run
    # hard-failed before the SECOND follow-up was ever sent (no pile-up) — both follow-ups
    # would be 2+ frames if either the re-send or a subsequent send had fired.
    sends = [f for f in transport.ws_frames if f.get("type") == "send_message"]
    assert len(sends) == 1


@pytest.mark.asyncio
async def _impl_test_followup_picked_up_within_bound_sends_exactly_once(tmp_path):
    # (a) in the REVISED V2 plan: a follow-up whose pickup signal arrives LATE (the ~37s M3
    # finalize+replan latency) but WITHIN the (default ~75s) bound → the SINGLE bounded wait
    # succeeds and the runner drives it to terminal. send_followup must fire EXACTLY ONCE (the
    # late-but-present pickup must NOT trigger a re-send). diag_revise has ONE after_terminal
    # follow-up — the clean single-follow-up case. The dead-window fake holds the status at
    # FINISHED and only reveals pickup as a higher-seq event, exactly like the live latency.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _DeadWindowTransport(db, stale_reads=3, work_reads=2)  # late pickup, within bound
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = load_scenarios()["diag_revise"]
    assert sum(f.get("trigger") == "after_terminal" for f in scenario["followups"]) == 1

    # No timeout_s override → uses the policy-driven default bound (~75s); the fake picks up
    # well within it. poll_interval_s=0.0 keeps the test instant in wall-clock.
    await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)

    sends = [f for f in transport.ws_frames if f.get("type") == "send_message"]
    assert len(sends) == 1  # EXACTLY ONCE — the late-but-present pickup did not trigger a re-send
    assert len(transport.pickup_log) == 1
    assert len(transport.terminal_log) == 1  # it was driven to its OWN new terminal


def _impl_test_followup_pickup_timeout_env_override_is_honored(monkeypatch):
    # (c) in the REVISED V2 plan: the bound is POLICY-DRIVEN, not a brittle literal — the
    # DISCO_SOAK_PICKUP_TIMEOUT_S env var overrides the default, with a SAFE float parse (bad /
    # blank / non-positive values fall back to the documented default, never disabling the bound).
    from harness.build_soak.adapters.disco_api import (
        _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S,
        _followup_pickup_timeout_s,
    )

    monkeypatch.delenv("DISCO_SOAK_PICKUP_TIMEOUT_S", raising=False)
    assert _followup_pickup_timeout_s() == _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S
    assert _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S >= 75.0  # covers the measured ~37s with margin
    monkeypatch.setenv("DISCO_SOAK_PICKUP_TIMEOUT_S", "120.5")
    assert _followup_pickup_timeout_s() == 120.5
    for bad in ("", "not-a-number", "0", "-5"):  # all fall back safely
        monkeypatch.setenv("DISCO_SOAK_PICKUP_TIMEOUT_S", bad)
        assert _followup_pickup_timeout_s() == _FOLLOWUP_PICKUP_TIMEOUT_DEFAULT_S


@pytest.mark.asyncio
async def _impl_test_followup_pickup_env_override_bounds_the_wait(tmp_path, monkeypatch):
    # The env override actually drives the live wait bound: a tiny override makes a never-picked-up
    # follow-up return the timeout sentinel quickly (bounded), proving the override is consumed by
    # wait_for_followup_pickup (resolved per-call), not just the resolver helper.
    monkeypatch.setenv("DISCO_SOAK_PICKUP_TIMEOUT_S", "0.03")
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _ScriptStateTransport(db, states=["FINISHED"])  # never leaves terminal, no events
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    baseline = await client.capture_followup_baseline(_CID)
    result = await asyncio.wait_for(
        client.wait_for_followup_pickup(_CID, baseline),
        timeout=5.0,  # default bound = env (0.03s)
    )
    assert result == FOLLOWUP_PICKUP_TIMEOUT
