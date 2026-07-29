"""Moved runner hygiene and cleanup implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    LIVE_THRASH_STOP,
    UTC,
    BrowserEvidenceCollectionError,
    CollectedRun,
    DiscoApiClient,
    ThrashOracle,
    _run_mod,
    assemble_dossier,
    classify_dossier,
    classify_run_folder,
    clean_smoke_log,
    datetime,
    drive_scenario,
    hashlib,
    json,
    load_manifest,
    pytest,
    run_once,
    status,
    verify_evidence_unchanged,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _fake_inspect_trace,
    _ProgressTransport,
    _seed_db,
    _smoke_scenario,
    _strict_live_thrash_monitor,
)


@pytest.mark.asyncio
async def _impl_test_abandoned_run_kills_its_conversation(tmp_path):
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
async def _impl_test_cleanly_terminal_run_is_released(tmp_path):
    # [REL-4] A run that reached a genuine terminal (FINISHED) IS released — the runner must
    # kill it to tear down the lingering sandbox + egress sidecar containers. MEASURED on the
    # podman backend: a FINISHED build leaves 2 live containers (disco-sbx-* + disco-egr-*) that
    # are NOT destroyed at the terminal event; POST /kill clears both (2 -> 0). Releasing every
    # terminal conv is what keeps a long soak at 0 orphan containers. A clean smoke PASS must
    # therefore issue exactly the /kill teardown post (evidence is already frozen, so it's safe).
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
        run_id="run_release_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )
    assert record["status"] == "PASS", record
    # Terminal state releases the orphaned runtime.
    assert any(p[0].endswith("/kill") for p in transport.posts)


@pytest.mark.asyncio
async def _impl_test_missing_live_cleanup_evidence_is_invalid_not_crash(tmp_path, monkeypatch):
    """A configured live ledger with missing cleanup slices must emit INVALID_RUN."""

    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<html><h1>Build Smoke OK</h1></html>",
    )
    client = _client(transport, tmp_path)

    async def _missing_cleanup_evidence(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(_run_mod, "_collect_terminal_cleanup_evidence", _missing_cleanup_evidence)
    monkeypatch.setattr(_run_mod, "_relay_log_path", lambda: tmp_path / "relay.jsonl")

    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_missing_cleanup_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=5,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_INTERRUPTED"
    assert record["facts"]["missing_slices"] == ["lifecycle", "sidecar", "cleanup"]
    assert "terminal cleanup not adjudicable" in record["facts"]["reason"]
    assert record["conversation_id"] == _CID

    base = tmp_path / "out" / "run_missing_cleanup_001"
    conv = base / "conversations" / _CID
    assert (base / "manifest.json").is_file()
    assert (conv / "events.jsonl").is_file()
    assert (conv / "inspect-trace.json").is_file()
    assert (conv / "thrash-monitor.json").is_file()
    assert verify_evidence_unchanged(base, load_manifest(base)).intact


@pytest.mark.asyncio
async def _impl_test_confirmed_live_thrash_killed_idle_is_fail_not_cleanup_invalid(
    tmp_path, monkeypatch
):
    """A live thrash stop is a product verdict, even though kill leaves IDLE.

    The in-run monitor kills a conversation only after the same strict ThrashOracle
    failure persists across two samples.  That deliberate stop appends
    ``IDLE(detail=killed)`` rather than FINISHED/ERROR.  Cleanup/provider evidence
    must anchor on that kill and preserve the confirmed FAIL; treating the missing
    work-terminal timestamp as RUN_INTERRUPTED masks the product failure.
    """

    scenario = json.loads(json.dumps(_smoke_scenario()))
    scenario["assertions"]["provider"] = {
        "require_ledger": True,
        "require_host_substr": "opencode.ai",
        "model": "deepseek-v4-flash",
    }
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    events = clean_smoke_log()
    events[-1] = status(10, "IDLE", "killed")
    for offset, event in enumerate(events):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()

    content = "<html><h1>Build Smoke OK</h1></html>"
    trace = _fake_inspect_trace()
    trace["spans"].extend(
        [
            {
                "span": "agent.repair",
                "event": "point",
                "repair_kind": "unknown_tool",
                "tool_name": "write_file",
            },
            {
                "span": "agent.repair",
                "event": "point",
                "repair_kind": "unknown_tool",
                "tool_name": "file_write",
            },
        ]
    )
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "IDLE"},
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
        inspect_trace=trace,
        thrash_monitor={
            **_strict_live_thrash_monitor(),
        },
        timeline=["confirmed live thrash threshold stopped the conversation"],
    )

    async def fake_drive(active_client, *_args, **_kwargs):
        # Model the live monitor's already-completed stop.  REL-5 must reuse
        # this durable killed state instead of posting a second cleanup kill.
        active_client.last_conversation_id = _CID
        stopped = await active_client.kill(_CID)
        assert stopped["http_status"] == 200
        return run

    async def no_sleep(_seconds: float) -> None:
        return None

    ledger = tmp_path / "provider.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "ts": started.timestamp() + 5,
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "has_tools": True,
                "conversation_id": _CID,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_run_mod, "drive_scenario", fake_drive)
    monkeypatch.setattr(_run_mod, "_relay_log_path", lambda: str(ledger))
    monkeypatch.setattr(_run_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())

    db = tmp_path / "disco.db"
    transport = FakeTransport(db, states=["IDLE"])
    client = _client(transport, tmp_path)
    record = await run_once(
        client,
        scenario,
        run_id="run_live_thrash_stop_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=True,
        commit="abc",
        timeout_s=5,
    )

    assert record["status"] == "FAIL", record
    assert record["required_evidence_present"] is True
    thrash = next(
        result for result in record["oracle_results"] if result["oracle"] == "ThrashOracle"
    )
    assert thrash["status"] == "FAIL"
    assert thrash["code"] == "MODEL_REPAIR_THRASH"
    kills = [path for path, _body in transport.posts if path.endswith("/kill")]
    assert kills == [f"/conversations/{_CID}/kill"]
    assert "REL-5 cleanup observed the live-thrash monitor's durable killed state" in run.timeline
    base = tmp_path / "out" / "run_live_thrash_stop_001"
    assert verify_evidence_unchanged(base, load_manifest(base)).intact


@pytest.mark.asyncio
async def _impl_test_confirmed_live_thrash_missing_parallel_cleanup_reaches_retained_fail(
    tmp_path, monkeypatch
):
    """H524: missing cleanup cannot mask a strictly confirmed monitor verdict."""

    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    events = clean_smoke_log()
    events[-1] = status(10, "IDLE", "killed")
    for offset, event in enumerate(events):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()
    trace = _fake_inspect_trace()
    trace["spans"].extend(
        [
            {"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"},
            {"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"},
        ]
    )
    marker = {
        "schema_version": 1,
        "drive_status": LIVE_THRASH_STOP,
        "terminal_baseline_seq": None,
        "frozen_max_seq": 10,
        "terminal_snapshot_available": False,
        "terminal_only_evidence_unavailable": ["workspace", "browser"],
        "preserved_for": "strictly_confirmed_live_thrash_stop",
        "strict_monitor_confirmed": True,
    }
    monitor = _strict_live_thrash_monitor()
    current_thrash = ThrashOracle().check(events, scenario=_smoke_scenario(), inspect_trace=trace)[
        0
    ]
    assert current_thrash.failed
    monitor["findings"][0]["oracle_results"] = [current_thrash.to_dict()]
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "RUNNING"},
        state_final={"execution_status": "IDLE"},
        workspace_manifest={
            "files": {},
            "_capture": {
                "status": "not_collected_nonterminal",
                "drive_status": LIVE_THRASH_STOP,
                "reason": "strict atomic workspace evidence requires a durable terminal",
            },
        },
        preview={
            "health": {"status": 200},
            "content": "<html><h1>Build Smoke OK</h1></html>",
            "available": True,
            "runtime_available": False,
            "runtime_availability_status": 200,
            "source": "isolated_path_capability",
        },
        inspect_trace=trace,
        thrash_monitor=monitor,
        timeline=["confirmed live thrash threshold stopped the conversation"],
        product_evidence={"nonterminal_adjudication": marker},
    )

    async def fake_drive(active_client, *_args, **_kwargs):
        active_client.last_conversation_id = None
        return run

    async def missing_cleanup(*_args, **_kwargs):
        evidence = {
            **run.product_evidence,
            "lifecycle": {"terminal": "IDLE", "statuses": list(run.timeline)},
            "sidecar": {"stopped_at_terminal": True, "provider_calls_after_terminal": 0},
        }
        run.product_evidence = evidence
        return evidence

    monkeypatch.setattr(_run_mod, "drive_scenario", fake_drive)
    monkeypatch.setattr(_run_mod, "_collect_terminal_cleanup_evidence", missing_cleanup)
    monkeypatch.setattr(_run_mod, "_relay_log_path", lambda: str(tmp_path / "relay.jsonl"))

    client = _client(FakeTransport(tmp_path / "parallel.db", states=["IDLE"]), tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_live_thrash_missing_parallel_cleanup_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=True,
        commit="abc",
        timeout_s=5,
        parallel_workers=4,
    )

    assert record["status"] == "FAIL", record
    assert record["code"] == "MODEL_REPAIR_THRASH"
    assert record["required_evidence_present"] is True
    assert run.product_evidence["nonterminal_adjudication"][
        "terminal_cleanup_slices_unavailable"
    ] == ["cleanup"]
    assert "without granting cleanup proof" in run.timeline[-1]


@pytest.mark.asyncio
async def _impl_test_retained_live_thrash_contradicting_current_oracle_keeps_cleanup_invalid(
    tmp_path, monkeypatch
):
    """A retained monitor claim cannot waive cleanup when frozen evidence disagrees."""

    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    events = clean_smoke_log()
    events[-1] = status(10, "IDLE", "killed")
    for offset, event in enumerate(events):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()
    contradictory_monitor = _strict_live_thrash_monitor()
    contradictory_monitor["findings"][0]["oracle_results"] = [
        {
            "oracle": "ThrashOracle",
            "status": "FAIL",
            "code": "MODEL_REPAIR_THRASH",
            "first_broken_link": "model_request -> repeated_hidden_repair",
            "facts": {
                "repair_kind": "unknown_tool",
                "count": 2,
                "allowed": 1,
                "repairs": [{"claimed": "not present in frozen inspect trace"}],
            },
        }
    ]
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "RUNNING"},
        state_final={"execution_status": "IDLE"},
        workspace_manifest={},
        preview=None,
        inspect_trace=_fake_inspect_trace(),
        thrash_monitor=contradictory_monitor,
        product_evidence={
            "nonterminal_adjudication": {
                "schema_version": 1,
                "drive_status": LIVE_THRASH_STOP,
                "frozen_max_seq": 10,
                "terminal_snapshot_available": False,
                "preserved_for": "strictly_confirmed_live_thrash_stop",
                "strict_monitor_confirmed": True,
            }
        },
    )

    async def fake_drive(active_client, *_args, **_kwargs):
        active_client.last_conversation_id = None
        return run

    async def missing_cleanup(*_args, **_kwargs):
        return {
            **run.product_evidence,
            "lifecycle": {"terminal": "IDLE", "statuses": []},
            "sidecar": {"stopped_at_terminal": True, "provider_calls_after_terminal": 0},
        }

    monkeypatch.setattr(_run_mod, "drive_scenario", fake_drive)
    monkeypatch.setattr(_run_mod, "_collect_terminal_cleanup_evidence", missing_cleanup)
    monkeypatch.setattr(_run_mod, "_relay_log_path", lambda: str(tmp_path / "relay.jsonl"))

    client = _client(FakeTransport(tmp_path / "unconfirmed.db", states=["IDLE"]), tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_unconfirmed_live_thrash_missing_cleanup_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=True,
        commit="abc",
        timeout_s=5,
        parallel_workers=4,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_INTERRUPTED"
    assert record["facts"]["missing_slices"] == ["cleanup"]


@pytest.mark.asyncio
async def _impl_test_confirmed_live_thrash_skips_impossible_terminal_snapshot_and_classifies_fail(
    tmp_path, monkeypatch
):
    scenario = _smoke_scenario()
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    events = clean_smoke_log()
    events[-1] = status(10, "IDLE", "killed")
    for offset, event in enumerate(events):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()

    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    transport = FakeTransport(
        db,
        states=["IDLE"],
        workspace={"index.html": "<h1>must not be collected</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path, require_workspace_commit=True)

    async def confirmed_thrash_stop(*_args, **_kwargs):
        monitor = _strict_live_thrash_monitor()
        client._live_thrash_samples = monitor["sample_count"]
        client._live_thrash_findings = monitor["findings"]
        return LIVE_THRASH_STOP

    async def strict_workspace_must_not_run(*_args, **_kwargs):
        raise AssertionError("killed IDLE cannot produce a finish-triggered snapshot")

    def browser_capture_must_not_run(*_args, **_kwargs):
        raise AssertionError("browser bytes require the unavailable terminal snapshot")

    trace = _fake_inspect_trace()
    trace["spans"].extend(
        [
            {"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"},
            {"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"},
        ]
    )

    async def collect_trace(_conversation_id):
        return trace

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", confirmed_thrash_stop)
    monkeypatch.setattr(client, "collect_workspace", strict_workspace_must_not_run)
    monkeypatch.setattr(client, "collect_browser_evidence", browser_capture_must_not_run)
    monkeypatch.setattr(client, "collect_inspect_trace", collect_trace)
    monkeypatch.setattr(client, "observe_live_thrash_snapshot", lambda *_args, **_kwargs: False)

    run = await drive_scenario(
        client,
        scenario,
        model="m",
        autonomous=False,
        timeout_s=5,
    )

    assert _run_mod._confirmed_live_thrash_stop(run) is True
    assert run.workspace_manifest == {
        "files": {},
        "_capture": {
            "status": "not_collected_nonterminal",
            "drive_status": LIVE_THRASH_STOP,
            "reason": "strict atomic workspace evidence requires a durable terminal",
        },
    }
    assert run.browser_evidence == {}
    assert run.product_evidence["nonterminal_adjudication"] == {
        "schema_version": 1,
        "drive_status": LIVE_THRASH_STOP,
        "terminal_baseline_seq": None,
        "frozen_max_seq": 10,
        "terminal_snapshot_available": False,
        "terminal_only_evidence_unavailable": ["workspace", "browser"],
        "preserved_for": "strictly_confirmed_live_thrash_stop",
        "strict_monitor_confirmed": True,
    }
    base = assemble_dossier(
        tmp_path / "out",
        "run_confirmed_thrash_nonterminal_capture",
        scenario,
        run,
        model="m",
        autonomous=False,
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    replay = classify_run_folder(base)
    assert classification["status"] == "FAIL", classification
    assert classification["code"] == "MODEL_REPAIR_THRASH"
    assert classification["required_evidence_present"] is True
    assert replay["status"] == "FAIL", replay
    assert replay["code"] == "MODEL_REPAIR_THRASH"


@pytest.mark.asyncio
async def _impl_test_h302_confirmed_live_thrash_retains_browser_collection_error_and_fail(
    tmp_path, monkeypatch
):
    """A late work terminal stays strict; later screenshot loss cannot erase thrash."""

    scenario = _smoke_scenario()
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    events = clean_smoke_log()
    events.append(status(11, "IDLE", "killed"))
    for offset, event in enumerate(events):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()

    content = "<html><h1>Build Smoke OK</h1></html>"
    trace = _fake_inspect_trace()
    trace["spans"].extend(
        [
            {
                "span": "agent.repair",
                "event": "point",
                "repair_kind": "unknown_tool",
                "tool_name": "write_file",
            },
            {
                "span": "agent.repair",
                "event": "point",
                "repair_kind": "unknown_tool",
                "tool_name": "file_write",
            },
        ]
    )
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    transport = FakeTransport(
        db,
        states=["RUNNING", "IDLE"],
        workspace={"index.html": content},
        preview_html=content,
    )
    client = _client(transport, tmp_path)

    async def confirmed_thrash_stop(*_args, **_kwargs):
        monitor = _strict_live_thrash_monitor()
        client._live_thrash_samples = monitor["sample_count"]
        client._live_thrash_findings = monitor["findings"]
        return LIVE_THRASH_STOP

    async def collect_trace(_conversation_id):
        return trace

    workspace_calls = 0

    async def collect_strict_workspace(_conversation_id, _declared_paths):
        nonlocal workspace_calls
        workspace_calls += 1
        return {
            "index.html": {
                "present": True,
                "size": len(content.encode()),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content": content,
                "proof": "raw_sha",
                "content_stable": True,
            }
        }

    def missing_screenshot(conversation_id, _events, _workspace, **_kwargs):
        raise BrowserEvidenceCollectionError(
            "referenced browser screenshot is missing from the workspace snapshot",
            {
                "conversation_id": conversation_id,
                "path": ".pmx/screenshots/removed-after-stop.png",
                "error": "FileNotFoundError",
            },
        )

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", confirmed_thrash_stop)
    monkeypatch.setattr(client, "collect_inspect_trace", collect_trace)
    monkeypatch.setattr(client, "collect_workspace", collect_strict_workspace)
    monkeypatch.setattr(client, "collect_browser_evidence", missing_screenshot)
    monkeypatch.setattr(client, "observe_live_thrash_snapshot", lambda *_args, **_kwargs: False)

    run = await drive_scenario(
        client,
        scenario,
        model="m",
        autonomous=False,
        timeout_s=5,
    )

    assert _run_mod._confirmed_live_thrash_stop(run) is True
    assert workspace_calls == 1
    assert run.browser_evidence == {}
    assert run.inspect_trace == trace
    assert run.thrash_monitor == _strict_live_thrash_monitor()
    collection_error = run.browser_evidence_collection_error
    assert collection_error == {
        "schema_version": 1,
        "kind": "browser_evidence_collection_error",
        "exception_type": "BrowserEvidenceCollectionError",
        "reason": "referenced browser screenshot is missing from the workspace snapshot",
        "facts": {
            "conversation_id": _CID,
            "path": ".pmx/screenshots/removed-after-stop.png",
            "error": "FileNotFoundError",
        },
        "conversation_id": _CID,
        "preserved_for": "confirmed_live_thrash_stop",
        "admissible_as_browser_evidence": False,
    }

    base = assemble_dossier(
        tmp_path / "out",
        "run_h302_browser_loss_after_thrash",
        scenario,
        run,
        model="m",
        autonomous=False,
    )
    live = classify_dossier(base, scenario, run, autonomous=False)
    replay = classify_run_folder(base)
    assert live["status"] == "FAIL" and live["code"] == "MODEL_REPAIR_THRASH"
    assert replay["status"] == "FAIL" and replay["code"] == "MODEL_REPAIR_THRASH"

    artifact_name = _run_mod._BROWSER_EVIDENCE_COLLECTION_ERROR_NAME
    artifact = base / "conversations" / _CID / artifact_name
    manifest = load_manifest(base)
    assert json.loads(artifact.read_text(encoding="utf-8")) == collection_error
    assert manifest.evidence_files[artifact_name] == f"conversations/{_CID}/{artifact_name}"
    assert manifest.evidence_hashes[artifact_name].startswith("sha256:")
    assert verify_evidence_unchanged(base, manifest).intact

    artifact.write_text("{}\n", encoding="utf-8")
    tampered = classify_run_folder(base)
    assert tampered["status"] == "INVALID_RUN"
    assert tampered["code"] == "EVIDENCE_HASH_MISMATCH"
