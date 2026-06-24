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
