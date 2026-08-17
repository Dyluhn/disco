"""Moved part b awaiting user decision collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    _run_mod,
    assemble_dossier,
    classify_dossier,
    drive_scenario,
    pytest,
    status,
)
from .helpers_01 import (
    _alternatives_event,
    _DecisionTransport,
    _seed_db,
)


@pytest.mark.asyncio
async def _impl_test_resolve_decision_picks_recommended_and_sends_pick_alternative(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            _alternatives_event(
                5,
                "alt1",
                [
                    {"id": "opt_a", "title": "A"},
                    {"id": "opt_b", "title": "B", "recommended": True},
                ],
            )
        ],
    )
    transport = _DecisionTransport(
        db, states=["AWAITING_USER_DECISION"], pending_alternatives_id="alt1"
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    resolved = await client.resolve_decision(_CID)

    assert resolved == {"alternatives_id": "alt1", "option_id": "opt_b"}  # recommended wins
    assert {"type": "pick_alternative", "option_id": "opt_b"} in transport.ws_frames


@pytest.mark.asyncio
async def _impl_test_resolve_decision_first_valid_when_no_recommendation(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            _alternatives_event(
                5,
                "alt1",
                [
                    {"id": "opt_a", "title": "A"},
                    {"id": "opt_b", "title": "B"},
                ],
            )
        ],
    )
    transport = _DecisionTransport(
        db, states=["AWAITING_USER_DECISION"], pending_alternatives_id="alt1"
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    resolved = await client.resolve_decision(_CID)
    assert resolved is not None
    assert resolved["option_id"] == "opt_a"  # first valid


@pytest.mark.asyncio
async def _impl_test_resolve_decision_scenario_override(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            _alternatives_event(
                5,
                "alt1",
                [
                    {"id": "opt_a", "title": "A"},
                    {"id": "opt_b", "title": "B", "recommended": True},
                ],
            )
        ],
    )
    transport = _DecisionTransport(
        db, states=["AWAITING_USER_DECISION"], pending_alternatives_id="alt1"
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    resolved = await client.resolve_decision(_CID, preferred_option_id="opt_a")
    assert resolved is not None
    assert resolved["option_id"] == "opt_a"  # the scenario override is honored over recommended


@pytest.mark.asyncio
async def _impl_test_resolve_decision_stale_status_returns_none(tmp_path):
    # STATE-BIND: the gate is no longer live (status != AWAITING_USER_DECISION) → do NOT resolve.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [_alternatives_event(5, "alt1", [{"id": "o", "title": "x"}])])
    transport = _DecisionTransport(db, states=["FINISHED"], pending_alternatives_id="alt1")
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    assert await client.resolve_decision(_CID) is None
    assert not any(f.get("type") == "pick_alternative" for f in transport.ws_frames)


@pytest.mark.asyncio
async def _impl_test_resolve_decision_no_pending_id_returns_none(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [_alternatives_event(5, "alt1", [{"id": "o", "title": "x"}])])
    transport = _DecisionTransport(
        db, states=["AWAITING_USER_DECISION"], pending_alternatives_id=None
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    assert await client.resolve_decision(_CID) is None  # no live pending_alternatives_id


@pytest.mark.asyncio
async def _impl_test_resolve_decision_no_valid_options_returns_none(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [_alternatives_event(5, "alt1", [])])  # empty option list
    transport = _DecisionTransport(
        db, states=["AWAITING_USER_DECISION"], pending_alternatives_id="alt1"
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    assert await client.resolve_decision(_CID) is None


@pytest.mark.asyncio
async def _impl_test_drive_auto_resolves_user_decision_and_records(tmp_path):
    # The drive loop hits AWAITING_USER_DECISION, auto-resolves it, and the build FINISHES; the
    # run record flags `auto_resolved_decisions: 1` + the picked option (distinguishable from a
    # clean PASS).
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            _alternatives_event(
                5,
                "alt1",
                [
                    {"id": "opt_a", "title": "A"},
                    {"id": "opt_b", "title": "B", "recommended": True},
                ],
            ),
            status(10, "FINISHED"),
        ],
    )
    transport = _DecisionTransport(
        db,
        states=[
            "RUNNING",
            "AWAITING_USER_DECISION",
            "AWAITING_USER_DECISION",
            "FINISHED",
            "FINISHED",
            "FINISHED",
        ],
        pending_alternatives_id="alt1",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = {"id": "decision", "prompt": "build it", "assertions": {}}

    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)

    assert len(run.decision_resolutions) == 1
    assert run.decision_resolutions[0] == {
        "alternatives_id": "alt1",
        "option_id": "opt_b",
        "attempt": 1,
    }
    assert {"type": "pick_alternative", "option_id": "opt_b"} in transport.ws_frames
    assert any("auto-resolved user decision" in t for t in run.timeline)

    base = assemble_dossier(tmp_path / "out", "run_dec", scenario, run, model="m", autonomous=False)
    cls = classify_dossier(base, scenario, run, autonomous=False)
    assert cls["auto_resolved_decisions"] == 1
    assert cls["decision_resolutions"][0]["option_id"] == "opt_b"


@pytest.mark.asyncio
async def _impl_test_drive_invalid_decision_payload_is_hard_not_clean_pass(tmp_path):
    # An invalid/stale payload (no live pending_alternatives_id) → resolve_decision returns None →
    # the drive does NOT auto-finish: it releases on the unresolved gate (a hard signal).
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [_alternatives_event(5, "alt1", [{"id": "o", "title": "x"}])])
    transport = _DecisionTransport(
        db,
        states=["RUNNING", "AWAITING_USER_DECISION", "AWAITING_USER_DECISION"],
        pending_alternatives_id=None,  # gate present but NO live pending id → cannot resolve
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = {"id": "decision", "prompt": "build it", "assertions": {}}

    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)

    assert run.decision_resolutions == []  # nothing auto-resolved
    assert not any(f.get("type") == "pick_alternative" for f in transport.ws_frames)
    assert any("could NOT be auto-resolved" in t for t in run.timeline)


@pytest.mark.asyncio
async def _impl_test_drive_decision_cap_releases_hard(tmp_path):
    # A model that keeps re-proposing decisions is bounded: after _MAX_DECISION auto-resolutions
    # the next gate is RELEASED hard (not auto-finished, not an infinite resolve-loop).
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [_alternatives_event(5, "alt1", [{"id": "opt_a", "title": "A"}])])
    # Each cycle: poll sees AWAITING, resolve reads AWAITING, wait sees RUNNING (gate left); the
    # model re-proposes on the next poll. After _MAX_DECISION cycles the next AWAITING hits the cap.
    cycle = ["AWAITING_USER_DECISION", "AWAITING_USER_DECISION", "RUNNING"]
    states = ["RUNNING"] + cycle * _run_mod._MAX_DECISION + ["AWAITING_USER_DECISION"]
    transport = _DecisionTransport(db, states=states, pending_alternatives_id="alt1")
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = {"id": "decision", "prompt": "build it", "assertions": {}}

    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)

    assert len(run.decision_resolutions) == _run_mod._MAX_DECISION  # capped, not infinite
    assert any("cap" in t.lower() and "_MAX_DECISION" in t for t in run.timeline)
