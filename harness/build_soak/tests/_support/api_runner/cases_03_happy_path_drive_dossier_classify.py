"""Moved happy path drive dossier classify collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    CollectedRun,
    assemble_dossier,
    classify_dossier,
    classify_run_folder,
    clean_smoke_log,
    drive_scenario,
    hashlib,
    json,
    load_manifest,
    pytest,
    verify_evidence_unchanged,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _fake_inspect_trace,
    _seed_db,
    _smoke_scenario,
)


@pytest.mark.asyncio
async def _impl_test_smoke_run_assembles_dossier_and_classifies_pass(tmp_path):
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
        tmp_path / "out",
        "run_smoke_001",
        scenario,
        run,
        model="fake-model",
        autonomous=False,
        commit="a" * 40,
        repo_revision="a" * 40 + "+dirty.0123456789abcdef",
        repo_dirty=True,
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification

    # Frozen replay must derive the selected tool-scope assertion from the
    # evidence-locked inspect trace, not from an in-memory runner argument.
    replayed = classify_run_folder(base)
    assert replayed["status"] == "PASS", replayed
    scope_results = [
        result for result in replayed["oracle_results"] if result["oracle"] == "ToolScopeOracle"
    ]
    assert scope_results
    assert not any(result.get("skipped", False) for result in scope_results)
    assert any(
        result.get("facts", {}).get("checked") == "allowed_in_planning" for result in scope_results
    )

    # §5 run-folder shape
    conv = base / "conversations" / _CID
    assert (base / "manifest.json").is_file()
    assert (base / "timeline.md").is_file()
    assert (base / "prompt.txt").is_file()
    assert (conv / "thrash-monitor.json").is_file()
    assert (base / "classification.json").is_file()
    assert (conv / "events.jsonl").is_file()
    assert (conv / "state.final.json").is_file()
    assert (conv / "workspace-manifest.json").is_file()
    assert (conv / "preview" / "health.json").is_file()
    assert (conv / "preview" / "metadata.json").is_file()
    manifest = load_manifest(base)
    assert manifest.repo_commit == "a" * 40
    assert manifest.repo_revision == "a" * 40 + "+dirty.0123456789abcdef"
    assert manifest.repo_dirty is True


def _impl_test_dossier_persists_required_provenance_and_replays_provider_oracle(tmp_path):
    """H183/H185: a frozen live dossier must reproduce its exact provider verdict."""
    scenario = json.loads(json.dumps(_smoke_scenario()))
    scenario["assertions"]["provider"] = {
        "require_ledger": True,
        "require_host_substr": "opencode.ai",
        "model": "deepseek-v4-flash",
    }
    events = clean_smoke_log()
    events[0]["timestamp"] = "2026-07-15T09:34:46.402189Z"
    content = "<html><h1>Build Smoke OK</h1></html>"
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={
            "index.html": {
                "present": True,
                "size": len(content.encode()),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
                "proof": "raw_sha",
                "content_stable": True,
            }
        },
        preview={
            "health": {"status": 200},
            "content": content,
            "available": True,
            "runtime_available": False,
            "runtime_availability_status": 200,
            "source": "isolated_path_capability",
        },
        inspect_trace=_fake_inspect_trace(),
        timeline=["collected"],
    )
    provider_ledger = [
        {
            "ts": 1.0,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "after_terminal": False,
            "has_tools": True,
            "conversation_id": _CID,
        },
        {
            "ts": 2.0,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "after_terminal": False,
            "has_tools": True,
            "conversation_id": _CID,
        },
    ]
    started_at = "2026-07-15T09:34:46.346771+00:00"

    base = assemble_dossier(
        tmp_path / "out",
        "run_provider_replay_001",
        scenario,
        run,
        model="opencode-go-deepseek-v4-flash",
        autonomous=False,
        commit="a" * 40,
        started_at=started_at,
        provider_ledger=provider_ledger,
    )
    original = classify_dossier(
        base,
        scenario,
        run,
        autonomous=False,
        commit="a" * 40,
        provider_ledger=provider_ledger,
    )
    replayed = classify_run_folder(base)
    caller_weakened = json.loads(json.dumps(scenario))
    del caller_weakened["assertions"]["provider"]
    mismatched_replay = classify_run_folder(base, scenario=caller_weakened)
    manifest = load_manifest(base)

    assert manifest.scenario_sha256.startswith("sha256:")
    assert manifest.provider == "opencode.ai"
    assert manifest.started_at == started_at
    assert {
        "scenario.json",
        "state.initial.json",
        "provider-call-ledger.jsonl",
    } <= set(manifest.evidence_hashes)
    assert verify_evidence_unchanged(base, manifest).intact
    assert original["status"] == replayed["status"] == "PASS"
    assert mismatched_replay["status"] == "INVALID_RUN"
    assert mismatched_replay["code"] == "EVIDENCE_HASH_MISMATCH"
    for classification in (original, replayed):
        provider_result = next(
            result
            for result in classification["oracle_results"]
            if result["oracle"] == "provider_ledger"
        )
        assert provider_result["status"] == "PASS"
        assert provider_result["facts"] == {"calls": 2}


def _impl_test_locked_provider_scenario_rejects_untracked_ledger_injection(tmp_path):
    """H185: an added-after-lock ledger must never satisfy a required provider oracle."""
    scenario = json.loads(json.dumps(_smoke_scenario()))
    scenario["assertions"]["provider"] = {
        "require_ledger": True,
        "require_host_substr": "opencode.ai",
        "model": "deepseek-v4-flash",
    }
    content = "<html><h1>Build Smoke OK</h1></html>"
    run = CollectedRun(
        conversation_id=_CID,
        events=clean_smoke_log(),
        state_initial={},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={"index.html": {"present": True, "content": content}},
        preview={
            "health": {"status": 200},
            "content": content,
            "available": True,
            "source": "isolated_path_capability",
        },
        inspect_trace=_fake_inspect_trace(),
    )
    base = assemble_dossier(
        tmp_path / "out",
        "run_provider_injection_001",
        scenario,
        run,
        model="opencode-go-deepseek-v4-flash",
        autonomous=False,
    )
    (base / "provider-call-ledger.jsonl").write_text(
        json.dumps({"host": "opencode.ai", "model": "deepseek-v4-flash"}) + "\n",
        encoding="utf-8",
    )

    replayed = classify_run_folder(base)

    assert replayed["status"] == "INVALID_RUN"
    assert replayed["code"] == "MISSING_REQUIRED_EVIDENCE"


@pytest.mark.asyncio
async def _impl_test_pre_kick_idle_does_not_abort_the_drive(tmp_path):
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
async def _impl_test_classify_receives_workspace_and_preview(tmp_path):
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
async def _impl_test_missing_workspace_file_is_false_finish(tmp_path):
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
