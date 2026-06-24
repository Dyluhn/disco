"""S3 live-API runner — deterministic tests against a FAKE transport (no live model
spend). Covers: orchestration drives the gate (approve), dossier assembly + evidence
lock, classify() receives workspace + preview (codex #2), the infra gate fires ONLY
pre-create (codex #3), and the run-folder shape (§5)."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from _eventlog import clean_smoke_log

from harness.build_soak.adapters.disco_api import DiscoApiClient
from harness.build_soak.evidence import load_manifest, verify_evidence_unchanged
from harness.build_soak.run import (
    assemble_dossier,
    classify_dossier,
    drive_scenario,
    load_scenarios,
    run_once,
)

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
"""

_CID = "conv_fake123"


def _seed_db(path, cid, events):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_EVENTS_DDL)
        for e in events:
            conn.execute(
                "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    cid,
                    e["seq"],
                    e.get("id", f"evt_{e['seq']}"),
                    e["kind"],
                    e["source"],
                    e.get("timestamp", ""),
                    json.dumps(e),
                ),
            )
        conn.commit()
    finally:
        conn.close()


class FakeTransport:
    """Scripts the live surfaces. `states` is the status sequence GET /state yields
    (clamped at the last). `workspace`/`preview_html` back the preview-proxy reads."""

    def __init__(
        self,
        db_path,
        *,
        states,
        workspace=None,
        preview_html="",
        health_status=200,
        health_exc=None,
        cid=_CID,
    ):
        self.db_path = db_path
        self._states = list(states)
        self._idx = 0
        self.workspace = workspace or {}
        self.preview_html = preview_html
        self.health_status = health_status
        self.health_exc = health_exc
        self.cid = cid
        self.ws_frames = []
        self.posts = []

    async def health(self):
        if self.health_exc is not None:
            raise self.health_exc
        return self.health_status, {"ok": True}

    async def post_json(self, path, body):
        self.posts.append((path, body))
        if path == "/conversations":
            return 200, {"conversation_id": self.cid, "surface": "build"}
        return 200, {"event_id": "e", "seq": 1}

    async def get_json(self, path):
        if path.endswith("/state"):
            st = self._states[min(self._idx, len(self._states) - 1)]
            self._idx += 1
            return 200, {"execution_status": st}
        if path.endswith("/preview"):
            return 200, {"available": True}
        return 200, {}

    async def get_text(self, path):
        if path.endswith("/preview-app/"):
            return 200, self.preview_html, {}
        for rel, html in self.workspace.items():
            if path.endswith(f"/preview-app/{rel}"):
                return 200, html, {}
        return 404, "", {}

    async def ws_control(self, conversation_id, frame):
        self.ws_frames.append(frame)


def _smoke_scenario():
    return load_scenarios()["static_html_minimal"]


def _client(transport, tmp_path):
    return DiscoApiClient(transport, db_path=str(tmp_path / "disco.db"), poll_interval_s=0.0)


# ---- scenarios.yaml loads + shapes -----------------------------------------


def test_scenarios_yaml_parses_all_15_scenarios():
    scen = load_scenarios()
    assert {"static_html_minimal", "must_plan_before_tool", "revise_after_finish"} <= set(scen)
    # the §15.4 steer scenario carries the after_first_file_write trigger
    steer = scen["steer_while_running_requires_plan_update_or_clear_execution_note"]
    assert steer["followups"][0]["trigger"] == "after_first_file_write"


# ---- happy-path: drive + dossier + classify -> PASS -------------------------


@pytest.mark.asyncio
async def test_smoke_run_assembles_dossier_and_classifies_pass(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["RUNNING", "AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<html><h1>Build Smoke OK</h1></html>",
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()

    run = await drive_scenario(client, scenario, model="fake-model", autonomous=False, timeout_s=5)
    # the runner ACTED AS THE USER: it approved the plan over the WS gate.
    assert {"type": "approve_plan"} in transport.ws_frames
    # events were collected race-free from the DB
    assert run.events and run.events[0]["kind"] == "message"

    base = assemble_dossier(
        tmp_path / "out", "run_smoke_001", scenario, run, model="fake-model", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification

    # §5 run-folder shape
    conv = base / "conversations" / _CID
    assert (base / "manifest.json").is_file()
    assert (base / "timeline.md").is_file()
    assert (base / "prompt.txt").is_file()
    assert (base / "classification.json").is_file()
    assert (conv / "events.jsonl").is_file()
    assert (conv / "state.final.json").is_file()
    assert (conv / "workspace-manifest.json").is_file()
    assert (conv / "preview" / "health.json").is_file()


@pytest.mark.asyncio
async def test_pre_kick_idle_does_not_abort_the_drive(tmp_path):
    # LIVE-SURFACED runner bug: POST /messages kicks the loop ASYNCHRONOUSLY, so a
    # freshly-created conversation reads IDLE for a beat before RUNNING. IDLE is a
    # terminal for adjudication but NOT a drive-stop until the run has gone active —
    # otherwise the runner bails before the agent plans and mis-reports
    # NO_PLAN_AFTER_USER_TURN against a product that actually planned. The drive must
    # wait through the pre-kick IDLE, catch the approval gate, approve, and finish.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=[
            "IDLE",  # state_initial (pre-kick)
            "IDLE",  # first poll: still settling — must NOT stop here
            "RUNNING",
            "AWAITING_PLAN_APPROVAL",  # the gate the runner must act on
            "RUNNING",  # after approval
            "FINISHED",
            "FINISHED",
        ],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    assert {"type": "approve_plan"} in transport.ws_frames  # gate was reached + approved
    base = assemble_dossier(
        tmp_path / "out", "run_idle_001", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification


@pytest.mark.asyncio
async def test_classify_receives_workspace_and_preview(tmp_path):
    # codex #2: OutputTruth MUST run — a missing preview needle is a real FAIL, not a
    # silent skip. Prove the runner threads workspace + preview into classify().
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<html>WRONG CONTENT</html>",  # missing the required needle
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out", "run_pv_001", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "FAIL"
    assert classification["code"] == "PREVIEW_TRUTH_MISMATCH"


@pytest.mark.asyncio
async def test_missing_workspace_file_is_false_finish(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED"],
        workspace={},  # the required index.html was never served
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out", "run_nf_001", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "FAIL"
    assert classification["code"] == "FALSE_FINISH_NO_OUTPUT"


# ---- infra gate fires ONLY pre-create (codex #3) ----------------------------


@pytest.mark.asyncio
async def test_infra_gate_fires_pre_create_on_unreachable_server(tmp_path):
    transport = FakeTransport(
        tmp_path / "disco.db",
        states=["FINISHED"],
        health_exc=ConnectionError("Connection refused"),
    )
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    record = await run_once(
        client,
        scenario,
        run_id="run_infra_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )
    assert record["status"] == "INFRA_FAILURE"
    assert record["code"] == "agent_server_unreachable_before_conversation"
    assert record["conversation_id"] is None
    assert record["stage"] == "before_conversation_creation"
    # NO conversation was ever created (the infra gate short-circuits before POST).
    assert transport.posts == []


@pytest.mark.asyncio
async def test_infra_gate_maps_5xx_health(tmp_path):
    transport = FakeTransport(tmp_path / "disco.db", states=["FINISHED"], health_status=503)
    client = _client(transport, tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_infra_002",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )
    assert record["status"] == "INFRA_FAILURE"
    assert record["code"] == "provider_5xx_before_conversation"


@pytest.mark.asyncio
async def test_post_create_error_status_is_product_not_infra(tmp_path):
    # codex #3: a StatusEvent(ERROR) AFTER create (driver preflight) is a PRODUCT
    # outcome, never infra. Health is fine pre-create; the run errors post-create.
    err_log = [
        {"id": "e1", "seq": 1, "kind": "message", "source": "user",
         "message": {"role": "user", "content": "build"}},
        {"id": "e2", "seq": 2, "kind": "status", "source": "system",
         "status": "ERROR", "detail": "Driver local-qwen unreachable"},
    ]
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, err_log)
    transport = FakeTransport(db, states=["ERROR", "ERROR"], workspace={}, preview_html="")
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    record = await run_once(
        client,
        scenario,
        run_id="run_err_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )
    # A product run outcome (NOT INFRA_FAILURE). The plan-gated scenario errored
    # before producing a plan -> NO_PLAN_AFTER_USER_TURN.
    assert record["status"] != "INFRA_FAILURE"
    assert record["status"] in ("FAIL", "INVALID_RUN")
    assert record["conversation_id"] == _CID


@pytest.mark.asyncio
async def test_mid_run_transport_loss_is_invalid_run(tmp_path):
    # LIVE-SURFACED: the shared agent-server can crash / become unreachable AFTER
    # conversation creation (a raw httpx.ConnectError mid-drive). That is NOT
    # adjudicable as a product outcome and NOT infra (infra is pre-create only, §9):
    # the runner must degrade to INVALID_RUN, not crash with a traceback.
    class _DyingTransport(FakeTransport):
        async def get_json(self, path):
            if path.endswith("/state"):
                raise ConnectionError("All connection attempts failed")
            return await super().get_json(path)

    transport = _DyingTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_dead_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )
    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_INTERRUPTED"
    # a conversation WAS created (the failure is post-create, so not INFRA_FAILURE)
    assert transport.posts and transport.posts[0][0] == "/conversations"


# ---- P1#2: pre-create infra gate catches the httpx hierarchy ----------------


@pytest.mark.asyncio
async def test_infra_gate_catches_httpx_connect_error(tmp_path):
    # codex P1#2: HttpTransport.health() raises httpx.ConnectError (NOT an OSError
    # subclass) for a dead server. The pre-create probe must catch the httpx hierarchy
    # -> INFRA_FAILURE, never a raw crash / PASS / FAIL.
    class _HttpxDownTransport(FakeTransport):
        async def health(self):
            raise httpx.ConnectError("All connection attempts failed")

    transport = _HttpxDownTransport(tmp_path / "disco.db", states=["FINISHED"])
    client = _client(transport, tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_httpx_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )
    assert record["status"] == "INFRA_FAILURE"
    assert record["code"] == "agent_server_unreachable_before_conversation"
    assert transport.posts == []  # never created a conversation


# ---- PAUSED-resume runner behavior ------------------------------------------


@pytest.mark.asyncio
async def test_paused_then_finished_resumes_to_terminal(tmp_path):
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
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
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
async def test_paused_forever_is_bounded_then_build_did_not_finish(tmp_path):
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


# ---- TIMEOUT + unhandled-gate fail-closed (no silent pass / no hang) ---------


@pytest.mark.asyncio
async def test_poll_timeout_is_not_a_silent_pass(tmp_path):
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
async def test_unhandled_gate_does_not_hang_and_fails_closed(tmp_path):
    # A gate the runner does not act on (e.g. AWAITING_USER_QUESTION on a bare-Build
    # scenario) must NOT hang — the drive returns it, and the non-finished run is judged
    # (not a pass).
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    transport = FakeTransport(
        db,
        states=["RUNNING", "AWAITING_USER_QUESTION", "AWAITING_USER_QUESTION"],
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


# ---- evidence lock freezes the dossier --------------------------------------


@pytest.mark.asyncio
async def test_preview_dossier_is_evidence_locked(tmp_path):
    # codex P1#1: the PREVIEW dossier is adjudicated truth — it MUST be under the §6
    # hash lock, so a tampered served.html / health.json trips INVALID_RUN.
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
    assert verify_evidence_unchanged(base, manifest).intact
    # tamper the served preview -> lock trips
    (base / "conversations" / _CID / "preview" / "served.html").write_text(
        "<h1>TAMPERED</h1>", encoding="utf-8"
    )
    integrity = verify_evidence_unchanged(base, manifest)
    assert not integrity.intact
    assert "preview/served.html" in integrity.mismatches


# ---- evidence lock (events) --------------------------------------------------


@pytest.mark.asyncio
async def test_dossier_evidence_lock_detects_tamper(tmp_path):
    from harness.build_soak.evidence import load_manifest, verify_evidence_unchanged

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
        tmp_path / "out", "run_lock_001", scenario, run, model="m", autonomous=False
    )

    manifest = load_manifest(base)
    assert verify_evidence_unchanged(base, manifest).intact
    # mutate a frozen evidence file
    (base / "conversations" / _CID / "events.jsonl").write_text("{}\n", encoding="utf-8")
    assert not verify_evidence_unchanged(base, manifest).intact
