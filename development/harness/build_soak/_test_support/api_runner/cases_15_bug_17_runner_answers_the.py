"""Moved bug 17 runner answers the collection implementations."""

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
async def _impl_test_clarify_question_answered_then_build_proceeds(tmp_path):
    # The §17 no-fluke repro at the unit level: a build that asks a clarifying question
    # (AWAITING_USER_QUESTION) BEFORE planning is ANSWERED by the runner (the generic safe
    # default), then proceeds to a real terminal — NOT a false NO_PLAN stall.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["RUNNING", "AWAITING_USER_QUESTION", "FINISHED", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=2)
    # the runner sent the GENERIC clarification answer over the send_message path
    answers = [f["content"] for f in transport.ws_frames if f.get("type") == "send_message"]
    assert len(answers) == 1
    assert "do not ask further" in answers[0].lower()
    # and the build proceeded to a clean terminal
    base = assemble_dossier(
        tmp_path / "out", "run_clarify_001", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification


@pytest.mark.asyncio
async def _impl_test_clarify_uses_scenario_provided_answer(tmp_path):
    # A scenario that declares `clarification_answer` → that EXACT answer is sent.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["RUNNING", "AWAITING_USER_QUESTION", "FINISHED", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = dict(_smoke_scenario())
    scenario["clarification_answer"] = "Make it a single dark-mode page titled Acme."
    await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=2)
    answers = [f["content"] for f in transport.ws_frames if f.get("type") == "send_message"]
    assert answers == ["Make it a single dark-mode page titled Acme."]


@pytest.mark.asyncio
async def _impl_test_confirmation_gate_confirmed_then_build_proceeds(tmp_path):
    # A WAITING_FOR_CONFIRMATION gate → the runner clears it via the REAL `confirm` control
    # frame (the confirm analogue of approve_plan), NOT a no-op free-text message, and the
    # build continues to a terminal.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["RUNNING", "WAITING_FOR_CONFIRMATION", "FINISHED", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=2)
    # the runner cleared the gate with the REAL confirm frame — NOT a no-op send_message
    assert {"type": "confirm"} in transport.ws_frames
    assert not any(f.get("type") == "send_message" for f in transport.ws_frames)
    base = assemble_dossier(
        tmp_path / "out", "run_confirm_001", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification


@pytest.mark.asyncio
async def _impl_test_endless_clarify_is_bounded_then_classified(tmp_path):
    # A model that keeps asking clarifying questions FOREVER is answered up to the cap
    # (_MAX_CLARIFY) and then LET GO to a real terminal — the non-finished run is
    # classified honestly (BUILD_DID_NOT_FINISH), NEVER an infinite answer-loop.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])  # no FINISHED
    transport = FakeTransport(
        db,
        states=["RUNNING"] + ["AWAITING_USER_QUESTION"] * 12,
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_clarify_bounded_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=0.2,
    )
    answers = sum(1 for f in transport.ws_frames if f.get("type") == "send_message")
    assert answers == 3  # _MAX_CLARIFY — bounded, not infinite
    assert record["status"] == "FAIL"
    assert record["code"] == "BUILD_DID_NOT_FINISH"
