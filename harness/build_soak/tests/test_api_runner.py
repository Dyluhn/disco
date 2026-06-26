"""S3 live-API runner — deterministic tests against a FAKE transport (no live model
spend). Covers: orchestration drives the gate (approve), dossier assembly + evidence
lock, classify() receives workspace + preview (codex #2), the infra gate fires ONLY
pre-create (codex #3), and the run-folder shape (§5)."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from _eventlog import action, clean_smoke_log, msg, plan, status

import harness.build_soak.adapters.disco_api as _disco_mod
from harness.build_soak.adapters.disco_api import (
    FOLLOWUP_PICKED_UP,
    FOLLOWUP_PICKUP_TIMEOUT,
    FOLLOWUP_REPLANNED,
    DiscoApiClient,
    SnapshotNotReadyError,
)
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
        if path.endswith("/resume"):
            # The REAL resume body carries a STATE string under "status" — a regression
            # guard for the http-int/state-string key collision (Bug 11): merging this
            # under "status" used to clobber the HTTP code and crash int(resp["status"]).
            return 200, {"ok": True, "status": "RUNNING"}
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


# ---- Bug 9: authoritative workspace SNAPSHOT collection ---------------------


def _plant_snapshot(root, cid, files):
    """Write `files` ({relpath: text}) into the host ProjectStore layout
    <root>/<cid>/workspace/<relpath> the runner reads for collect_workspace."""
    ws = root / cid / "workspace"
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


@pytest.mark.asyncio
async def test_collect_workspace_reads_snapshot_when_preview_proxy_404s(tmp_path):
    # Bug 9: a build genuinely SUCCEEDED (index.html written + FINISHED) but the
    # dev-server preview proxy 404s, so the OLD collect produced an empty manifest →
    # the oracle false-FAILed (FALSE_FINISH_NO_OUTPUT). The authoritative host snapshot
    # has the file; collect_workspace must populate the manifest from it even though
    # the proxy is dead.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    # transport.get_text 404s for everything (the dead preview proxy — the root cause).
    transport = FakeTransport(db, states=["FINISHED"], workspace={})
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert "index.html" in manifest, manifest
    entry = manifest["index.html"]
    assert entry["present"] is True
    assert "Build Smoke OK" in entry["content"]
    assert entry["size"] == len(b"<h1>Build Smoke OK</h1>")
    assert len(entry["sha256"]) == 64
    # came from the snapshot, NOT the (dead) preview proxy
    assert entry.get("source") != "preview_proxy"


@pytest.mark.asyncio
async def test_collect_workspace_genuinely_missing_file_is_omitted(tmp_path):
    # Do NOT mask a real missing deliverable: a declared file absent from BOTH the
    # snapshot and the preview proxy must be OMITTED (no present:false key) so the
    # unchanged oracle still emits FALSE_FINISH_NO_OUTPUT.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"other.txt": "unrelated"})  # snapshot exists, no index.html
    transport = FakeTransport(db, states=["FINISHED"], workspace={})  # proxy 404s too
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert "index.html" not in manifest  # genuinely missing → omitted (FALSE_FINISH stays)
    assert manifest["other.txt"]["present"] is True  # the snapshot is faithfully reflected


@pytest.mark.asyncio
async def test_collect_workspace_falls_back_to_proxy_without_projects_root(tmp_path):
    # No projects_root (snapshot disabled) ⇒ byte-identical to the old behavior: the
    # preview proxy serves the file; the manifest carries it tagged source=preview_proxy.
    db = tmp_path / "disco.db"
    transport = FakeTransport(
        db, states=["FINISHED"], workspace={"index.html": "<h1>Build Smoke OK</h1>"}
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)  # projects_root=None

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["present"] is True
    assert manifest["index.html"]["source"] == "preview_proxy"
    assert "Build Smoke OK" in manifest["index.html"]["content"]


@pytest.mark.asyncio
async def test_snapshot_authoritative_does_not_proxy_mask_missing_required_file(tmp_path):
    # Anti-false-PASS hole #1: when the snapshot IS authoritative (its workspace dir exists)
    # but a DECLARED file is absent from it, the proxy must NOT be consulted — even though
    # the proxy WOULD serve a (served/stale) copy. The file stays OMITTED so a genuinely
    # missing required deliverable still trips FALSE_FINISH_NO_OUTPUT.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"other.txt": "snapshot exists, but no index.html"})
    # the proxy WOULD serve index.html (a served/stale version) — it must be ignored:
    transport = FakeTransport(
        db, states=["FINISHED"], workspace={"index.html": "<h1>STALE SERVED COPY</h1>"}
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert "index.html" not in manifest  # NOT proxy-masked → FALSE_FINISH_NO_OUTPUT preserved
    assert manifest["other.txt"]["present"] is True


# ---- snapshot READINESS gate: settle on the AGENT-FINAL state, not a stale early read ----
# RCA: `_maybe_snapshot` mirrors the workspace AFTER a terminal event; a multi-revision build
# reaches a terminal per revision, so rev-1's files are presence-complete BEFORE rev-2's bytes
# flush → the old presence-only gate settled on STALE rev-1 content (false ARTIFACT_TRUTH_MISMATCH).
# The gate now derives each declared file's agent-final identity from the durable event log and
# accepts the snapshot only when the on-disk bytes MATCH (sha for file_write, absence for an rm),
# fail-fast on timeout. A fake clock makes the poll loop deterministic (no real sleeps).


class _FakeClock:
    """Drives `_await_ready_snapshot`'s poll loop deterministically: monotonic() returns a
    virtual clock that only advances when the loop sleeps, and each sleep can mutate the
    on-disk snapshot (the rev-1 → rev-2 flush) via `on_poll(step)`."""

    def __init__(self, on_poll=None):
        self.t = 1000.0
        self.polls = 0
        self.on_poll = on_poll

    def monotonic(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll(self.polls)


def _install_clock(monkeypatch, clock):
    from harness.build_soak.adapters import disco_api as _mod

    monkeypatch.setattr(_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_mod.asyncio, "sleep", clock.sleep)


def _file_write_log(path, content, *, call="c1", first_seq=1):
    """A minimal user → file_write(action) → SUCCESSFUL observation → FINISHED log. The action
    and observation share `call` so the readiness gate correlates the write as successful."""
    return [
        {
            "id": "u1", "seq": first_seq, "kind": "message", "source": "user",
            "message": {"role": "user", "content": "build it"},
        },
        {
            "id": f"a{first_seq + 1}", "seq": first_seq + 1, "kind": "action", "source": "agent",
            "tool_call": {
                "tool_name": "file_write",
                "arguments": {"path": path, "content": content},
                "call_id": call,
            },
        },
        {
            "id": f"o{first_seq + 2}", "seq": first_seq + 2, "kind": "observation",
            "source": "environment",
            "tool_result": {"call_id": call, "tool_name": "file_write", "success": True,
                            "content": "wrote"},
        },
        {
            "id": f"s{first_seq + 3}", "seq": first_seq + 3, "kind": "status", "source": "system",
            "status": "FINISHED",
        },
    ]


@pytest.mark.asyncio
async def test_snapshot_waits_for_byte_change_rev1_to_rev2(tmp_path, monkeypatch):
    # (a) The agent's LAST write is rev-2; the snapshot still holds rev-1 bytes on the first
    # reads. The gate must NOT accept the stale rev-1 content — it waits until the on-disk
    # sha256 matches what the agent wrote (rev-2), then accepts rev-2.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    rev1 = "<h1>Contact sales</h1>"
    rev2 = "<h1>Book a Visit</h1>"
    ws = _plant_snapshot(proj, _CID, {"index.html": rev1})  # snapshot starts STALE (rev-1)
    _seed_db(db, _CID, _file_write_log("index.html", rev2))  # agent's final write = rev-2

    def flush(step):
        if step >= 2:  # rev-2 bytes land mid-flight
            (ws / "index.html").write_text(rev2, encoding="utf-8")

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    transport = FakeTransport(db, states=["FINISHED"], workspace={})
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj),
        snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == rev2  # rev-2 accepted, never the stale rev-1
    assert "Contact sales" not in manifest["index.html"]["content"]
    assert clock.polls >= 2  # it genuinely waited for the flush


@pytest.mark.asyncio
async def test_snapshot_waits_for_added_file_between_polls(tmp_path, monkeypatch):
    # (b) rev-2 CREATES a declared file absent from the early snapshot. Gate waits until it is
    # present AND matches the agent's written bytes.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "console.log('v2')\n"
    ws = _plant_snapshot(proj, _CID, {"index.html": "<h1>x</h1>"})  # app.js not there yet
    _seed_db(db, _CID, _file_write_log("app.js", content))

    def flush(step):
        if step >= 2:
            (ws / "app.js").write_text(content, encoding="utf-8")

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["app.js"])

    assert manifest["app.js"]["present"] is True
    assert manifest["app.js"]["content"] == content
    assert clock.polls >= 2


@pytest.mark.asyncio
async def test_snapshot_waits_for_removed_file_not_stale_present(tmp_path, monkeypatch):
    # (c) The agent file_write'd then `rm`'d a declared file (final state = ABSENT). The early
    # snapshot still HAS it; the gate must NOT accept the stale-present copy — it waits until
    # the file is gone, then OMITS it.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {"old.html": "<h1>doomed</h1>"})  # still present early
    log = _file_write_log("old.html", "<h1>doomed</h1>", call="w1")
    # Append a SUCCESSFUL shell `rm old.html` AFTER the write → agent-final state is absent.
    log += [
        {
            "id": "a9", "seq": 9, "kind": "action", "source": "agent",
            "tool_call": {"tool_name": "shell", "arguments": {"command": "rm old.html"},
                          "call_id": "r1"},
        },
        {
            "id": "o10", "seq": 10, "kind": "observation", "source": "environment",
            "tool_result": {"call_id": "r1", "tool_name": "shell", "success": True, "content": ""},
        },
    ]
    _seed_db(db, _CID, log)

    def flush(step):
        if step >= 2:
            (ws / "old.html").unlink()

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["old.html"])

    assert "old.html" not in manifest  # stale-present NOT accepted → absent → OMITTED
    assert clock.polls >= 2


@pytest.mark.asyncio
async def test_snapshot_non_declared_churn_does_not_block_declared_set(tmp_path, monkeypatch):
    # (d) A NON-declared snapshot file keeps changing; it must not block readiness. The declared
    # file already matches the agent's write, so the gate accepts PROMPTLY (gates only the
    # declared set, manifest-complete).
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>ready</h1>"
    ws = _plant_snapshot(proj, _CID, {"index.html": content, "scratch.log": "0"})
    _seed_db(db, _CID, _file_write_log("index.html", content))

    def churn(step):  # would never stabilize — but it is NOT declared, so it must not matter
        (ws / "scratch.log").write_text(str(step), encoding="utf-8")

    clock = _FakeClock(on_poll=churn)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == content
    assert manifest["scratch.log"]["present"] is True  # faithfully reflected, just not gated on
    assert clock.polls == 0  # declared signal already satisfied → no needless wait


@pytest.mark.asyncio
async def test_snapshot_timeout_fail_fast_when_never_ready(tmp_path, monkeypatch):
    # (e) The snapshot NEVER reaches the agent's final state (stays stale forever). The gate must
    # FAIL-FAST with SnapshotNotReadyError (→ INVALID_RUN WORKSPACE_SNAPSHOT_NOT_READY), bounded —
    # never a silent stale best-effort PASS, never a hang.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE rev-1</h1>"})  # never updated
    _seed_db(db, _CID, _file_write_log("index.html", "<h1>final rev-2</h1>"))

    clock = _FakeClock()  # no flush — disk stays stale
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=1.5,
    )

    with pytest.raises(SnapshotNotReadyError) as ei:
        await client.collect_workspace(_CID, ["index.html"])

    assert "index.html" in [u["path"] for u in ei.value.facts["unsatisfied"]]
    assert clock.polls <= 6  # bounded by snapshot_wait_s / poll cadence — never hangs


@pytest.mark.asyncio
async def test_snapshot_already_consistent_accepts_promptly(tmp_path, monkeypatch):
    # (f) The snapshot already holds the agent's final bytes → accept on the FIRST read with no
    # wait, even with a large snapshot_wait budget.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>Book a Visit</h1>"
    _plant_snapshot(proj, _CID, {"index.html": content})
    _seed_db(db, _CID, _file_write_log("index.html", content))

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == content
    assert clock.polls == 0  # already consistent → no needless wait


# ---- snapshot READINESS: readback-aware identity (partial-edit-last gap) -----------------
# Residual gap: when a declared file's LAST mutation is a PARTIAL edit (file_edit/str_replace),
# the agent's final bytes can't be reconstructed from the action — but a later FULL file_read
# readback carries the true post-edit bytes in file_read's RENDERED view. The gate promotes such
# a readback to a ("rendered", body) signal and accepts the snapshot only when the on-disk file,
# rendered with file_read's OWN numberer, equals the readback (closing the stale-intermediate
# hole). Reads that are paged/truncated or synthetic (F9 dedup) never promote; a stale readback
# before a later edit is seq-ignored. With no qualifying readback the file is present_unproven →
# extended content-stability (NOT bare presence, NOT fail-fast).


def _file_read_full_content(text):
    """The EXACT rendered content a FULL file_read returns for `text`: a `[lines 1-N of N]`
    header then file_read's `<line-no>\\t<line>` body (mirrors FileReadTool's full-read path)."""
    lines = text.splitlines()
    total = len(lines)
    width = len(str(total)) or 1
    body = "\n".join(f"{i + 1:>{width}}\t{lines[i]}" for i in range(total))
    return f"[lines 1-{total} of {total}]\n" + body


def _edit_then_read_log(path, final_text, *, read_content=None, include_read=True):
    """user → file_edit(action+obs) → [optional file_read(action+obs)] → FINISHED. The file_edit
    is a partial mutator (no reconstructable content); the file_read (when present) is the FULL
    readback carrying `final_text` unless `read_content` overrides it (paged/synthetic cases)."""
    log = [
        {
            "id": "u1", "seq": 1, "kind": "message", "source": "user",
            "message": {"role": "user", "content": "revise it"},
        },
        {
            "id": "a2", "seq": 2, "kind": "action", "source": "agent",
            "tool_call": {"tool_name": "file_edit", "arguments": {"path": path}, "call_id": "m1"},
        },
        {
            "id": "o3", "seq": 3, "kind": "observation", "source": "environment",
            "tool_result": {"call_id": "m1", "tool_name": "file_edit", "success": True,
                            "content": "edited"},
        },
    ]
    if include_read:
        rc = read_content if read_content is not None else _file_read_full_content(final_text)
        log += [
            {
                "id": "a4", "seq": 4, "kind": "action", "source": "agent",
                "tool_call": {"tool_name": "file_read", "arguments": {"path": path},
                              "call_id": "r1"},
            },
            {
                "id": "o5", "seq": 5, "kind": "observation", "source": "environment",
                "tool_result": {"call_id": "r1", "tool_name": "file_read", "success": True,
                                "content": rc},
            },
        ]
    log.append({"id": "s9", "seq": 9, "kind": "status", "source": "system", "status": "FINISHED"})
    return log


@pytest.mark.asyncio
async def test_snapshot_rendered_readback_waits_for_final_bytes(tmp_path, monkeypatch):
    # The live shape: index.html's LAST mutation is a partial file_edit; a later FULL readback
    # carries the true final bytes ('Grand Opening'). The snapshot still holds the stale pre-edit
    # copy → the gate must reject it and wait until the on-disk RENDERED form equals the readback.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    stale = "<h1>Contact sales</h1>\n<p>old</p>\n"
    final = "<h1>Grand Opening</h1>\n<p>Book a Visit</p>\n"
    ws = _plant_snapshot(proj, _CID, {"index.html": stale})  # stable but STALE intermediate
    _seed_db(db, _CID, _edit_then_read_log("index.html", final))

    def flush(step):
        if step >= 2:
            (ws / "index.html").write_text(final, encoding="utf-8")

    clock = _FakeClock(on_poll=flush)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["content"] == final  # the readback-confirmed final bytes
    assert "Contact sales" not in manifest["index.html"]["content"]  # stale never accepted
    assert clock.polls >= 2  # it genuinely waited past the stable-but-stale intermediate


@pytest.mark.asyncio
async def test_snapshot_rendered_readback_fail_fast_when_never_final(tmp_path, monkeypatch):
    # A ("rendered", …) signal is DEFINITE: if the on-disk file never matches the readback, the
    # gate FAILs FAST (WORKSPACE_SNAPSHOT_NOT_READY), never accepting the stale-but-stable copy.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE forever</h1>\n"})
    _seed_db(db, _CID, _edit_then_read_log("index.html", "<h1>Grand Opening</h1>\n"))

    clock = _FakeClock()  # no flush — disk stays stale
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=1.5,
    )

    with pytest.raises(SnapshotNotReadyError) as ei:
        await client.collect_workspace(_CID, ["index.html"])

    assert [u["path"] for u in ei.value.facts["unsatisfied"]] == ["index.html"]
    assert ei.value.facts["unsatisfied"][0]["expected"][0] == "rendered"
    assert clock.polls <= 6  # bounded — never hangs


@pytest.mark.asyncio
async def test_snapshot_stale_readback_before_later_edit_is_ignored(tmp_path, monkeypatch):
    # STALE-READBACK ordering: a FULL readback, THEN a later partial edit with NO subsequent
    # readback → the old read is seq-ignored; the path is present_unproven (extended stability),
    # NOT promoted on the stale read. We prove it does NOT fail-fast on a rendered mismatch.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    content = "<h1>read-at-seq4</h1>\n"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>different on disk</h1>\n"})
    # readback at seq5 (content X), THEN a later file_edit at seq6 (no later readback).
    log = _edit_then_read_log("index.html", content)  # edit@2, read@4/5
    log = [e for e in log if e["seq"] != 9]  # drop FINISHED, re-add after the late edit
    log += [
        {
            "id": "a6", "seq": 6, "kind": "action", "source": "agent",
            "tool_call": {"tool_name": "file_edit", "arguments": {"path": "index.html"},
                          "call_id": "m2"},
        },
        {
            "id": "o7", "seq": 7, "kind": "observation", "source": "environment",
            "tool_result": {"call_id": "m2", "tool_name": "file_edit", "success": True},
        },
        {"id": "s9", "seq": 9, "kind": "status", "source": "system", "status": "FINISHED"},
    ]
    _seed_db(db, _CID, log)

    clock = _FakeClock()  # disk never changes; would FAIL-FAST if the stale read were promoted
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=3.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])  # must NOT raise

    # present_unproven → accepted on extended stability (no rendered fail-fast against stale read)
    assert manifest["index.html"]["present"] is True
    assert clock.polls >= _disco_mod._SNAPSHOT_UNPROVEN_STABLE_POLLS - 1  # extended settle, bounded


@pytest.mark.asyncio
async def test_snapshot_paged_readback_not_promoted(tmp_path, monkeypatch):
    # A PAGED read (offset/limit, or a budget/pressure-truncated header) must NOT promote to a
    # rendered signal → present_unproven (extended stability), never a false NOT_READY.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    final = "<h1>x</h1>\n<p>y</p>\n"
    _plant_snapshot(proj, _CID, {"index.html": final})
    log = _edit_then_read_log("index.html", final)
    # Make the read PAGED: args carry offset, and the header says "read more".
    for e in log:
        if e.get("kind") == "action" and e["tool_call"]["tool_name"] == "file_read":
            e["tool_call"]["arguments"] = {"path": "index.html", "offset": 2}
        if e.get("kind") == "observation" and e["tool_result"]["tool_name"] == "file_read":
            e["tool_result"]["content"] = "[lines 2-2 of 2; read more with offset=3]\n2\t<p>y</p>"
    _seed_db(db, _CID, log)

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=3.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])  # must NOT raise NOT_READY

    assert manifest["index.html"]["present"] is True  # present_unproven path, extended stability


@pytest.mark.asyncio
async def test_snapshot_synthetic_f9_readback_not_promoted(tmp_path, monkeypatch):
    # An F9 read-dedup pointer ([F9 dedup: …]) carries NO real bytes and must NOT promote to a
    # rendered signal → present_unproven (extended stability), never used as the expected identity.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    final = "<h1>real</h1>\n"
    _plant_snapshot(proj, _CID, {"index.html": final})
    log = _edit_then_read_log(
        "index.html", final,
        read_content="[F9 dedup: file_read(index.html) identical to a recent read this turn "
                     "— see the earlier result; file_read again only if you suspect it changed]",
    )
    _seed_db(db, _CID, log)

    clock = _FakeClock()
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=3.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])  # must NOT raise

    assert manifest["index.html"]["present"] is True  # present_unproven, not promoted on synthetic


@pytest.mark.asyncio
async def test_snapshot_present_unproven_extended_stability_not_bare_present(tmp_path, monkeypatch):
    # No readback at all: a partial edit with no proof of final bytes → present_unproven. The gate
    # must NOT accept on bare presence — it requires EXTENDED consecutive-stable reads (more than
    # the change keeps happening), and only then accepts.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {"index.html": "<h1>v0</h1>"})
    _seed_db(db, _CID, _edit_then_read_log("index.html", "irrelevant", include_read=False))

    # Keep mutating the file until the gate has polled several times — proving it does NOT accept
    # the early (changing) bytes; it only settles once the content stops changing.
    def churn(step):
        if step < _disco_mod._SNAPSHOT_UNPROVEN_STABLE_POLLS:
            (ws / "index.html").write_text(f"<h1>v{step}</h1>", encoding="utf-8")

    clock = _FakeClock(on_poll=churn)
    _install_clock(monkeypatch, clock)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db), poll_interval_s=0.0, projects_root=str(proj), snapshot_wait_s=50.0,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert manifest["index.html"]["present"] is True
    # accepted only AFTER it stopped changing → needed the extended settle (not bare poll-1 present)
    assert clock.polls >= _disco_mod._SNAPSHOT_UNPROVEN_STABLE_POLLS - 1


# ---- Bug 10: durable PREVIEW collection (no ephemeral-proxy false-fail) ------


class _DeadPreviewTransport(FakeTransport):
    """The post-FINISH reality: the model's ephemeral static server is torn down, so the
    preview proxy 404s for the root AND every path (while /preview still says available)."""

    async def get_text(self, path):
        if "/preview-app/" in path:
            return 404, "", {}
        return await super().get_text(path)


def _verify_obs(seq, passed):
    """A verify_web_app observation carrying the in-run pass/fail in structured.passed."""
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "observation",
        "source": "environment",
        "tool_result": {
            "tool_name": "verify_web_app",
            "success": True,
            "structured": {"passed": passed, "url": "http://127.0.0.1:8080/"},
        },
    }


def _verify_exec_fail(seq):
    """A verify_web_app observation where the VERIFIER ITSELF failed to execute:
    tool_result.success is False with NO structured verdict (the hole #2 case)."""
    return {
        "id": f"evt_{seq}",
        "seq": seq,
        "kind": "observation",
        "source": "environment",
        "tool_result": {
            "tool_name": "verify_web_app",
            "success": False,
            "error": "verifier crashed",
        },
    }


@pytest.mark.asyncio
async def test_collect_preview_uses_durable_snapshot_when_proxy_404s(tmp_path):
    # Bug 10: the proxy 404s post-FINISH, but the build genuinely served + verified during
    # the run and the served-root file is durable in the snapshot → use it, classify alive.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    _seed_db(db, _CID, [_verify_obs(1, True)])  # in-run verify PASSED
    transport = _DeadPreviewTransport(db, states=["FINISHED"])
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 200
    assert "Build Smoke OK" in preview["content"]
    assert preview["source"] == "snapshot_serve_probe"


@pytest.mark.asyncio
async def test_collect_preview_no_verify_serves_and_probes_for_genuine_evidence(tmp_path):
    # Residual hole #2: a build that NEVER ran an in-run verify must NOT get a forged 200 from
    # mere absence-of-failure. Option A — the runner SERVES the snapshot static content itself
    # and PROBES it: a 200 here is GENUINE positive evidence the deliverable actually serves the
    # required content (no in-run verify needed). The probe body is the REAL served bytes.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    _seed_db(db, _CID, [])  # NO in-run verify at all
    transport = _DeadPreviewTransport(db, states=["FINISHED"])
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview["source"] == "snapshot_serve_probe"  # REAL serve+probe, not a forged 200
    assert preview["health"]["status"] == 200
    assert "Build Smoke OK" in preview["content"]


@pytest.mark.asyncio
async def test_collect_preview_no_verify_probe_carries_real_wrong_body(tmp_path):
    # The serve+probe returns the REAL served bytes — so a wrong-content snapshot cannot be
    # forged into a pass: the probe body genuinely lacks the required needle (the oracle's
    # must_contain then FAILs on it). No fabricated content.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>WRONG CONTENT</h1>"})
    _seed_db(db, _CID, [])  # NO in-run verify
    transport = _DeadPreviewTransport(db, states=["FINISHED"])
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert "Build Smoke OK" not in preview["content"]  # real body — cannot forge the needle
    assert "WRONG CONTENT" in preview["content"]


@pytest.mark.asyncio
async def test_collect_preview_does_not_mask_failing_in_run_verify(tmp_path):
    # A genuinely-broken preview is NOT masked: the build's LAST in-run verify FAILED, so
    # the durable snapshot is NOT substituted — the live 404 stands → FALSE_FINISH_PREVIEW_BROKEN.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    _seed_db(db, _CID, [_verify_obs(1, True), _verify_obs(2, False)])  # last verify FAILED
    transport = _DeadPreviewTransport(db, states=["FINISHED"])
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 404
    assert preview.get("source") != "snapshot_serve_probe"


@pytest.mark.asyncio
async def test_collect_preview_does_not_substitute_on_verifier_execution_failure(tmp_path):
    # Anti-false-PASS hole #2: the last in-run verify FAILED TO EXECUTE (success=False, no
    # structured verdict). That is a real failure — the durable snapshot must NOT be
    # substituted even though a served-root index.html exists; the dead-proxy 404 stands →
    # FALSE_FINISH_PREVIEW_BROKEN.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    _seed_db(db, _CID, [_verify_obs(1, True), _verify_exec_fail(2)])  # last verify ERRORED
    transport = _DeadPreviewTransport(db, states=["FINISHED"])
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 404  # NOT substituted — execution failure respected
    assert preview.get("source") != "snapshot_serve_probe"


@pytest.mark.asyncio
async def test_snapshot_served_root_skips_internal_dirs(tmp_path):
    # Anti-false-PASS hole #3: a .pmx/.disco/node_modules index.html must NEVER be picked as
    # the served root — mirror the product's _find_snapshot_index skip set.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    # (a) a real top-level index.html alongside a .pmx one → the REAL root wins.
    _plant_snapshot(
        proj, _CID, {"index.html": "<h1>REAL ROOT</h1>", ".pmx/index.html": "<h1>PMX JUNK</h1>"}
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"]),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
    )
    served = client._snapshot_served_root(_CID)
    assert served is not None and "REAL ROOT" in served and "PMX JUNK" not in served

    # (b) NO root index, only .pmx/index.html + a real subdir index → the subdir wins (the
    #     .pmx one is skipped, exercising the skip filter past the root short-circuit).
    cid2 = "conv_skipdir2"
    _plant_snapshot(
        proj, cid2, {".pmx/index.html": "<h1>PMX JUNK</h1>", "app/index.html": "<h1>REAL APP</h1>"}
    )
    served2 = client._snapshot_served_root(cid2)
    assert served2 is not None and "REAL APP" in served2 and "PMX JUNK" not in served2


@pytest.mark.asyncio
async def test_collect_preview_no_durable_deliverable_stays_broken(tmp_path):
    # No served-root file in the snapshot (no static deliverable) → no substitution; the
    # honest 404 stands so a no-output preview still FAILs.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"notes.txt": "no served root here"})  # no index.html
    _seed_db(db, _CID, [_verify_obs(1, True)])
    transport = _DeadPreviewTransport(db, states=["FINISHED"])
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 404


@pytest.mark.asyncio
async def test_collect_preview_live_proxy_wins_over_snapshot(tmp_path):
    # When the live proxy IS up, it is the truth — the (possibly stale) snapshot is NOT used.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE SNAPSHOT</h1>"})
    _seed_db(db, _CID, [_verify_obs(1, True)])
    transport = FakeTransport(db, states=["FINISHED"], preview_html="<h1>LIVE Build Smoke OK</h1>")
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 200
    assert "LIVE" in preview["content"]
    assert preview.get("source") != "snapshot_serve_probe"


@pytest.mark.asyncio
async def test_static_build_classifies_pass_with_dead_proxy_via_durable_sources(tmp_path):
    # End-to-end mirror of the live PASS: with the post-FINISH preview proxy DOWN, the
    # workspace (Bug 9) reads the snapshot and the preview (Bug 10) is the runner's own
    # serve+PROBE of the snapshot static content (clean_smoke_log has NO in-run verify, so
    # the 200 is GENUINE probe evidence, never a forged absence-of-failure) → PASS.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _seed_db(db, _CID, clean_smoke_log())
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    transport = _DeadPreviewTransport(
        db, states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"]
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out", "run_b10_pass", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification


@pytest.mark.asyncio
async def test_durable_preview_does_not_mask_wrong_content(tmp_path):
    # The durable substitution uses the REAL snapshot html (never a fabricated 200/blank),
    # so a wrong-content static deliverable still FAILs on truth — it is NOT masked into a
    # PASS. (Workspace + preview read the same index.html, so the workspace oracle catches
    # the missing needle first with ARTIFACT_TRUTH_MISMATCH — either truth-mismatch is fine;
    # the point is no false PASS.)
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _seed_db(db, _CID, clean_smoke_log())
    _plant_snapshot(proj, _CID, {"index.html": "<h1>WRONG</h1>"})  # missing the needle
    transport = _DeadPreviewTransport(
        db, states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"]
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out", "run_b10_wrong", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "FAIL"
    assert classification["code"] in ("ARTIFACT_TRUTH_MISMATCH", "PREVIEW_TRUTH_MISMATCH"), (
        classification
    )


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


# ---- Bug 17: runner answers the mid-build clarify / confirm gates ------------


@pytest.mark.asyncio
async def test_clarify_question_answered_then_build_proceeds(tmp_path):
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
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=2)
    # the runner sent the GENERIC clarification answer over the send_message path
    answers = [
        f["content"]
        for f in transport.ws_frames
        if f.get("type") == "send_message"
    ]
    assert len(answers) == 1
    assert "do not ask further" in answers[0].lower()
    # and the build proceeded to a clean terminal
    base = assemble_dossier(
        tmp_path / "out", "run_clarify_001", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification


@pytest.mark.asyncio
async def test_clarify_uses_scenario_provided_answer(tmp_path):
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
    answers = [
        f["content"] for f in transport.ws_frames if f.get("type") == "send_message"
    ]
    assert answers == ["Make it a single dark-mode page titled Acme."]


@pytest.mark.asyncio
async def test_confirmation_gate_confirmed_then_build_proceeds(tmp_path):
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
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
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
async def test_endless_clarify_is_bounded_then_classified(tmp_path):
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


# ---- Bug 15: progress-aware terminal wait (no false BUILD_DID_NOT_FINISH) ----


def _append_event(db_path, cid, seq, *, kind="action", source="agent"):
    """Append ONE new durable event — the runner's progress signal (advancing max seq)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, seq, f"evt_{seq}", kind, source, "",
             json.dumps({"seq": seq, "kind": kind, "source": source})),
        )
        conn.commit()
    finally:
        conn.close()


class _ProgressTransport(FakeTransport):
    """Models a SLOW-BUT-PROGRESSING build: every GET /state appends a NEW event (the
    progress signal the wait resets its inactivity timer on) and reports RUNNING until
    `finish_after` polls, then FINISHED forever. `finish_after=None` → never reaches a
    terminal (always progressing — the hard-cap path)."""

    def __init__(self, db_path, *, finish_after=None, start_seq=100, **kw):
        super().__init__(db_path, states=["RUNNING"], **kw)
        self._reads = 0
        self._finish_after = finish_after
        self._seq = start_seq

    async def get_json(self, path):
        if path.endswith("/state"):
            self._reads += 1
            self._seq += 1
            _append_event(self.db_path, self.cid, self._seq)  # NEW event ⇒ progress
            if self._finish_after is not None and self._reads >= self._finish_after:
                return 200, {"execution_status": "FINISHED"}
            return 200, {"execution_status": "RUNNING"}
        return await super().get_json(path)


@pytest.mark.asyncio
async def test_progress_aware_wait_does_not_cut_off_a_progressing_build(tmp_path):
    # The must_plan repro at the unit level: a TINY inactivity window (a blind wall-clock
    # would give up almost immediately) must NOT cut off a build that keeps EMITTING NEW
    # events — the wait holds until the REAL FINISHED terminal.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _ProgressTransport(db, finish_after=8)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    status = await client.poll_until_terminal_or_gate(_CID, inactivity_s=0.05, hard_cap_s=30.0)
    assert status == "FINISHED"
    assert transport._reads >= 8  # it actually waited through many progressing polls


@pytest.mark.asyncio
async def test_progress_aware_wait_inactive_build_returns_inactive_timeout(tmp_path):
    # No new events + a frozen status for the inactivity window = a genuine wedge →
    # INACTIVE_TIMEOUT (which the drive falls through to classify normally, a real finding).
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])  # frozen log, no FINISHED
    transport = FakeTransport(db, states=["RUNNING"] * 6)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    status = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=30.0)
    assert status == "INACTIVE_TIMEOUT"


@pytest.mark.asyncio
async def test_progress_aware_wait_hard_cap_bounds_a_progressing_run(tmp_path):
    # A build that keeps progressing but never reaches a terminal is BOUNDED by the hard
    # cap — and because it was STILL progressing at the cap, the outcome is the inconclusive
    # PROGRESSING_TIMEOUT (NOT a wedge), never an infinite wait.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    transport = _ProgressTransport(db, finish_after=None)  # never terminal, always progressing
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    status = await client.poll_until_terminal(_CID, inactivity_s=10.0, hard_cap_s=0.2)
    assert status == "PROGRESSING_TIMEOUT"
    assert transport._reads >= 1  # the run was bounded, not hung forever


@pytest.mark.asyncio
async def test_progressing_cutoff_is_invalid_run_not_product_fail(tmp_path):
    # THE Bug 15 pin: a still-actively-progressing build cut off by the hard cap is
    # INCONCLUSIVE (INVALID_RUN / RUN_TIMEOUT_WHILE_PROGRESSING) so §17 re-runs it — it is
    # NEVER frozen mid-flight and mislabeled a product BUILD_DID_NOT_FINISH.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    transport = _ProgressTransport(db, finish_after=None)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_prog_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,  # generous inactivity — never trips while events advance
        hard_cap_s=0.2,  # tiny ceiling — bounds the never-terminating run
    )
    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_TIMEOUT_WHILE_PROGRESSING"
    assert record["code"] != "BUILD_DID_NOT_FINISH"
    assert record["status"] != "FAIL"


@pytest.mark.asyncio
async def test_genuinely_inactive_build_is_a_real_finding_not_inconclusive(tmp_path):
    # The flip side: a genuinely WEDGED build (no new events, frozen status) for the
    # inactivity window IS a real product finding — BUILD_DID_NOT_FINISH — NOT the
    # inconclusive INVALID_RUN reserved for an actively-progressing cutoff.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    transport = FakeTransport(
        db,
        states=["RUNNING"] * 6,
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_wedge_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=0.1,  # short inactivity — the wedge trips it
        hard_cap_s=30.0,
    )
    assert record["status"] == "FAIL"
    assert record["code"] == "BUILD_DID_NOT_FINISH"


# ---- H1: after-terminal follow-ups are SERIALIZED ---------------------------
# Bug: the post-terminal wait returned IMMEDIATELY on the STALE prior FINISHED status, so
# follow-up 2 was sent before the engine started processing follow-up 1; the engine then
# processed only the latest unprocessed user turn, COLLAPSING the pile into ONE plan
# revision (false PLAN_REVISION_NOT_INCREMENTED). wait_for_followup_pickup serializes them.


def _insert_event(db_path, cid, event):
    """Append ONE full-event dict (the _eventlog builder shape) into the durable log."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO events (conversation_id, seq, id, kind, source, created_at, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                cid,
                event["seq"],
                event.get("id", f"evt_{event['seq']}"),
                event["kind"],
                event["source"],
                event.get("timestamp", ""),
                json.dumps(event),
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _ScriptStateTransport(FakeTransport):
    """GET /state yields scripted statuses (clamped at last). `on_read(n)` fires on each
    /state read (1-based) so a test can mutate the DB as the pickup wait polls — modelling
    a re-plan that lands only after several polls."""

    def __init__(self, db_path, *, states, on_read=None, **kw):
        super().__init__(db_path, states=states, **kw)
        self._on_read = on_read
        self.state_reads = 0

    async def get_json(self, path):
        if path.endswith("/state"):
            self.state_reads += 1
            if self._on_read is not None:
                self._on_read(self.state_reads)
            st = self._states[min(self.state_reads - 1, len(self._states) - 1)]
            return 200, {"execution_status": st}
        return await super().get_json(path)


@pytest.mark.asyncio
async def test_wait_for_followup_pickup_detects_terminal_exit(tmp_path):
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
async def test_wait_for_followup_pickup_replan_bump_not_user_append(tmp_path):
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
async def test_wait_for_followup_pickup_fires_on_progress_event_status_stuck_finished(tmp_path):
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
async def test_wait_for_followup_pickup_is_bounded_and_returns_on_timeout(tmp_path):
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
async def test_drive_does_not_return_on_stale_terminal_below_min_seq(tmp_path):
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
async def test_drive_with_no_min_seq_returns_on_first_terminal(tmp_path):
    # The default (non-follow-up) drive is UNCHANGED: with min_terminal_seq=None any terminal
    # ends the wait immediately — the guard only engages for after-terminal follow-ups.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _ScriptStateTransport(db, states=["FINISHED"])
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    result = await client.poll_until_terminal(_CID, inactivity_s=5.0, hard_cap_s=10.0)
    assert result == "FINISHED"
    assert transport.state_reads == 1  # returned on the very first terminal read


class _DeadWindowTransport(FakeTransport):
    """FAITHFUL model of the after-terminal FINALIZATION DEAD-WINDOW (V2 — replaces V1's
    instant-transition fake, which bypassed the dead-window and is why the V1 unit tests
    passed while the live run failed). The build rests at FINISHED and its STATUS NEVER
    CHANGES. On each send_message follow-up:
      * the next `stale_reads` /state reads STILL report FINISHED with NO new durable event —
        the append-only dead window (status does NOT change, the exact V1 trap);
      * then a RE-PLAN event (plan, revision bumped) is appended at a higher seq while the
        status is STILL FINISHED — the ONLY pickup signal is the EVENT SEQ (V2's detector);
      * then, after `work_reads` more reads, the follow-up's OWN NEW terminal (a FINISHED
        status event) is appended at a yet-higher seq — the terminal whose seq > the baseline
        that the min_seq drive guard requires before returning.
    If the runner relied on the status string (V1) it would NEVER detect pickup here; if the
    drive returned on the stale terminal it would send follow-up 2 before follow-up 1's own
    terminal. The logs record the read counts so the test can assert strict ordering."""

    def __init__(self, db_path, *, stale_reads=2, work_reads=2, start_seq=10, **kw):
        super().__init__(db_path, states=["FINISHED"], **kw)
        self._stale_reads = stale_reads
        self._work_reads = work_reads
        self._seq = start_seq
        self._sends = 0
        self._reads_since_send = None
        self._total_reads = 0
        self.send_log = []  # (send_index, total_state_reads_at_send)
        self.pickup_log = []  # (send_index, total_state_reads_at_replan_event)
        self.terminal_log = []  # (send_index, total_state_reads_at_new_terminal)

    async def get_json(self, path):
        if path.endswith("/state"):
            self._total_reads += 1
            if self._reads_since_send is not None:
                self._reads_since_send += 1
                r = self._reads_since_send
                if r == self._stale_reads + 1:
                    # dead window over: append a RE-PLAN event (status STAYS FINISHED).
                    self._seq += 1
                    _insert_event(
                        self.db_path, self.cid, plan(self._seq, revision=1 + self._sends)
                    )
                    self.pickup_log.append((self._sends, self._total_reads))
                elif r == self._stale_reads + 1 + self._work_reads:
                    # the follow-up's OWN new terminal at a yet-higher seq.
                    self._seq += 1
                    _insert_event(self.db_path, self.cid, status(self._seq, "FINISHED"))
                    self.terminal_log.append((self._sends, self._total_reads))
                    self._reads_since_send = None
            return 200, {"execution_status": "FINISHED"}  # status NEVER changes
        return await super().get_json(path)

    async def ws_control(self, conversation_id, frame):
        await super().ws_control(conversation_id, frame)
        if frame.get("type") == "send_message":
            self._sends += 1
            self._reads_since_send = 0
            self.send_log.append((self._sends, self._total_reads))


@pytest.mark.asyncio
async def test_after_terminal_followups_are_serialized(tmp_path):
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


@pytest.mark.asyncio
async def test_followup_pickup_timeout_hard_fails_invalid_run(tmp_path, monkeypatch):
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
async def test_followup_picked_up_within_bound_sends_exactly_once(tmp_path):
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


def test_followup_pickup_timeout_env_override_is_honored(monkeypatch):
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
async def test_followup_pickup_env_override_bounds_the_wait(tmp_path, monkeypatch):
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
        client.wait_for_followup_pickup(_CID, baseline), timeout=5.0  # default bound = env (0.03s)
    )
    assert result == FOLLOWUP_PICKUP_TIMEOUT


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


# ---- runner hygiene: kill an abandoned conversation -------------------------


@pytest.mark.asyncio
async def test_abandoned_run_kills_its_conversation(tmp_path):
    # RUNNER HYGIENE: an inconclusive run the runner stops watching while it is STILL
    # non-terminal (RUNNING) must KILL the conversation it created — a leaked RUNNING conv
    # loads the shared server. Here the build never terminates (progressing cutoff →
    # INVALID_RUN); the finally must POST /conversations/{cid}/kill.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])  # no FINISHED — never terminal
    transport = _ProgressTransport(db, finish_after=None)  # always RUNNING + progressing
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_kill_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,  # generous inactivity — never trips while events advance
        hard_cap_s=0.2,  # tiny ceiling — bounds the never-terminating run
    )
    assert record["status"] == "INVALID_RUN"  # progressing cutoff, not a product fail
    # it KILLED the conversation it created (still non-terminal when released)
    kills = [p for p in transport.posts if p[0] == f"/conversations/{_CID}/kill"]
    assert len(kills) == 1, transport.posts


@pytest.mark.asyncio
async def test_cleanly_terminal_run_is_not_killed(tmp_path):
    # The flip side: a run that reached a genuine terminal (FINISHED) needs NO kill — the
    # runner must NOT kill an already-terminal conversation (no wasted teardown, no
    # double-kill). A clean smoke PASS must issue zero /kill posts.
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<html><h1>Build Smoke OK</h1></html>",
    )
    client = _client(transport, tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_nokill_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )
    assert record["status"] == "PASS", record
    assert not any(p[0].endswith("/kill") for p in transport.posts)  # already terminal → no kill


@pytest.mark.asyncio
async def test_kill_is_idempotent_on_already_terminal_conv(tmp_path):
    # The kill adapter method is harmless/idempotent on an already-terminal conversation
    # (the route is always-available); _release_conversation skips it, but a direct kill
    # must still succeed cleanly so a belt-and-suspenders call never crashes teardown.
    db = tmp_path / "disco.db"
    transport = FakeTransport(db, states=["FINISHED"])
    client = _client(transport, tmp_path)
    resp = await client.kill(_CID)
    assert resp["http_status"] == 200
    assert any(p[0] == f"/conversations/{_CID}/kill" for p in transport.posts)


@pytest.mark.asyncio
async def test_release_conversation_swallows_unreachable_server(tmp_path):
    # Teardown is BEST-EFFORT: if the server is gone when the runner releases the conv,
    # _release_conversation must not raise (it would otherwise turn a recorded verdict into
    # a crash). get_state raises → status undeterminable → it attempts a kill, which also
    # raises → swallowed. No exception escapes.
    from harness.build_soak.run import _release_conversation

    class _DeadTransport(FakeTransport):
        async def get_json(self, path):
            raise ConnectionError("server gone")

        async def post_json(self, path, body):
            raise ConnectionError("server gone")

    transport = _DeadTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)
    await _release_conversation(client, _CID)  # must not raise
    await _release_conversation(client, None)  # no cid → no-op, must not raise


# ---- scenario schema / loader validation ------------------------------------

_TERMINAL_VOCAB = {"FINISHED", "VERIFIED", "STUCK", "ERROR", "IDLE"}
_EVENT_CHAIN_KEYS = {
    "require_user_event",
    "require_plan_before_execution",
    "require_action_observation_pairs",
}
_ASSERTION_KEYS = {
    "event_chain",
    "planning",
    "revisions",
    "workspace",
    "preview",
    "terminal_status_in",
}
_FOLLOWUP_TRIGGERS = {"after_terminal", "after_first_file_write"}


def test_every_scenario_loads_with_a_valid_schema():
    # Every scenario in scenarios.yaml must load AND be well-formed against the shape the
    # oracles consume — so a typo'd key / missing terminal vocab / unsatisfiable contract
    # can't slip in. Mirrors the ContractOracle / OutputTruthOracle / RevisionOracle fields.
    scen = load_scenarios()
    assert scen, "no scenarios loaded"
    # all four originals + the three new ones are present
    expected = {
        "static_html_minimal",
        "must_plan_before_tool",
        "revise_after_finish",
        "steer_while_running_requires_plan_update_or_clear_execution_note",
        "multifile_static_site",
        "revise_twice_complex",
        "verify_catches_broken_then_fixed",
    }
    assert expected <= set(scen), sorted(set(scen) ^ expected)

    for sid, s in scen.items():
        assert s.get("id") == sid
        assert isinstance(s.get("prompt"), str) and s["prompt"].strip(), sid
        assert s.get("mode") == "api", sid
        a = s.get("assertions") or {}
        assert isinstance(a, dict) and a, f"{sid}: assertions missing"
        assert set(a) <= _ASSERTION_KEYS, f"{sid}: unknown keys {set(a) - _ASSERTION_KEYS}"

        # NO scenario asserts tool_scope (no per-turn tool-scope evidence yet — file header).
        assert "tool_scope" not in a, f"{sid}: must not assert tool_scope (no evidence yet)"

        ec = a.get("event_chain") or {}
        assert set(ec) <= _EVENT_CHAIN_KEYS, f"{sid}: bad event_chain keys"

        term = a.get("terminal_status_in")
        assert isinstance(term, list) and term, f"{sid}: terminal_status_in required"
        assert set(term) <= _TERMINAL_VOCAB, f"{sid}: bad terminal {set(term) - _TERMINAL_VOCAB}"

        # workspace.files: every file has a path + (optional) list-of-str must_contain.
        for spec in (a.get("workspace") or {}).get("files") or []:
            assert isinstance(spec.get("path"), str) and spec["path"], f"{sid}: file needs a path"
            mc = spec.get("must_contain") or []
            assert isinstance(mc, list) and all(isinstance(x, str) for x in mc), sid

        # preview: required is bool; must_contain (if any) is a list of str.
        prev = a.get("preview") or {}
        if prev:
            assert isinstance(prev.get("required"), bool), f"{sid}: preview.required must be bool"
            assert all(isinstance(x, str) for x in (prev.get("must_contain") or [])), sid

        # followups: each has a text + a known trigger; if ANY requires a plan revision the
        # ContractOracle requires assertions.revisions.expected_final_plan_revision.
        followups = s.get("followups") or []
        for f in followups:
            assert isinstance(f.get("text"), str) and f["text"], f"{sid}: followup needs text"
            assert f.get("trigger") in _FOLLOWUP_TRIGGERS, f"{sid}: bad trigger {f.get('trigger')}"
        if any(f.get("requires_plan_revision") for f in followups):
            assert "expected_final_plan_revision" in (a.get("revisions") or {}), (
                f"{sid}: revision followups need revisions.expected_final_plan_revision"
            )


def test_new_scenarios_assert_deterministic_oracle_checkable_output():
    # The three new scenarios must each carry a deterministically-checkable output oracle
    # expectation (specific files with must_contain markers and/or a required preview) — no
    # vague asserts that the OutputTruthOracle could not verify.
    scen = load_scenarios()
    new_ids = ("multifile_static_site", "revise_twice_complex", "verify_catches_broken_then_fixed")
    for sid in new_ids:
        a = scen[sid]["assertions"]
        files = (a.get("workspace") or {}).get("files") or []
        # at least one declared file with concrete must_contain markers
        assert files, f"{sid}: must declare workspace files"
        assert any(spec.get("must_contain") for spec in files), f"{sid}: needs must_contain markers"
        assert a.get("terminal_status_in"), sid

    # multifile: three distinct files, each with markers; preview required.
    ms = scen["multifile_static_site"]["assertions"]
    paths = {spec["path"] for spec in ms["workspace"]["files"]}
    assert {"index.html", "about.html", "style.css"} <= paths
    assert ms["preview"]["required"] is True

    # revise_twice: two revision-requiring followups → expected_final_plan_revision == 3.
    rt = scen["revise_twice_complex"]
    assert sum(bool(f.get("requires_plan_revision")) for f in rt["followups"]) == 2
    assert rt["assertions"]["revisions"]["expected_final_plan_revision"] == 3
