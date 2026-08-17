"""Moved bug 15 progress aware terminal collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    INACTIVE_TIMEOUT,
    UTC,
    DiscoApiClient,
    _run_mod,
    action,
    assemble_dossier,
    classify_dossier,
    clean_smoke_log,
    datetime,
    drive_scenario,
    json,
    load_manifest,
    msg,
    pytest,
    run_once,
    sqlite3,
    status,
    verify_evidence_unchanged,
)
from .helpers_01 import (
    FakeTransport,
    _browser_screenshot_observation,
    _insert_event,
    _ProgressTransport,
    _RejectedKillProgressTransport,
    _SandboxIdProgressTransport,
    _seed_db,
    _smoke_scenario,
)


def _assert_progressing_cutoff_record(record, transport) -> None:
    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_TIMEOUT_WHILE_PROGRESSING"
    assert record["code"] != "BUILD_DID_NOT_FINISH"
    assert record["status"] != "FAIL"
    assert len([path for path, _body in transport.posts if path.endswith("/kill")]) == 1


def _assert_progressing_cutoff_dossier(base, record) -> None:
    conv = base / "conversations" / _CID
    assert record["conversation_id"] == _CID
    for rel in (
        "events.jsonl",
        "state.initial.json",
        "state.final.json",
        "workspace-manifest.json",
        "inspect-trace.json",
        "thrash-monitor.json",
        "product-evidence.json",
    ):
        assert (conv / rel).is_file(), rel
    manifest = load_manifest(base)
    assert {
        "events.jsonl",
        "inspect-trace.json",
        "product-evidence.json",
        "thrash-monitor.json",
        "preview/health.json",
        "preview/served.html",
        "preview/metadata.json",
    } <= set(manifest.evidence_files)
    diagnostic = json.loads((conv / "product-evidence.json").read_text(encoding="utf-8"))[
        "diagnostic_stop"
    ]
    assert diagnostic["release_confirmed"] is True
    assert diagnostic["boundary_source"] == "durable_killed_status"
    assert isinstance(diagnostic["boundary_seq"], int)
    assert isinstance(diagnostic["boundary_epoch"], (int, float))
    assert diagnostic["sandbox_instance_ids"] == ["sbx_progress_timeout"]
    product = json.loads((conv / "product-evidence.json").read_text(encoding="utf-8"))
    assert product["cleanup"]["scope"] == "conversation"
    assert product["cleanup"]["container_orphans"] == 0
    assert verify_evidence_unchanged(base, manifest).intact


@pytest.mark.asyncio
async def _impl_test_progress_aware_wait_does_not_cut_off_a_progressing_build(tmp_path):
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
async def _impl_test_progress_aware_wait_inactive_build_returns_inactive_timeout(tmp_path):
    # No new events + a frozen status for the inactivity window = a genuine wedge →
    # INACTIVE_TIMEOUT (which the drive falls through to classify normally, a real finding).
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])  # frozen log, no FINISHED
    transport = FakeTransport(db, states=["RUNNING"] * 6)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    status = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=30.0)
    assert status == "INACTIVE_TIMEOUT"


@pytest.mark.asyncio
async def _impl_test_progress_aware_wait_hard_cap_bounds_a_progressing_run(tmp_path):
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
async def _impl_test_progressing_cutoff_is_invalid_run_not_product_fail(tmp_path, monkeypatch):
    # THE Bug 15 pin: a still-actively-progressing build cut off by the hard cap is
    # INCONCLUSIVE (INVALID_RUN / RUN_TIMEOUT_WHILE_PROGRESSING) so §17 re-runs it — it is
    # NEVER frozen mid-flight and mislabeled a product BUILD_DID_NOT_FINISH.
    db = tmp_path / "disco.db"
    # Cleanup proof is part of this unit contract. Keep it hermetic: the host may
    # deliberately deny Podman access (or have unrelated live containers), neither of
    # which is evidence about the fake conversation exercised here.
    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    transport = _SandboxIdProgressTransport(db, finish_after=None)
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
        parallel_workers=10,
    )
    _assert_progressing_cutoff_record(record, transport)

    base = tmp_path / "out" / "run_prog_001"
    _assert_progressing_cutoff_dossier(base, record)


@pytest.mark.asyncio
async def _impl_test_progressing_cutoff_pre_stop_read_failure_does_not_trust_old_kill(
    tmp_path, monkeypatch
):
    async def no_sleep(_seconds: float) -> None:
        return None

    started = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
    events = [
        msg(1, "user", "first turn"),
        status(2, "IDLE", "killed"),
        msg(3, "user", "continue"),
        status(4, "RUNNING", "planning"),
    ]
    for offset, event in zip((0, 1, 2, 3), events, strict=True):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    transport = _ProgressTransport(db, finish_after=None, start_seq=4)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    original_collect = client.collect_events

    def fail_until_stop(conversation_id: str):
        if not any(path.endswith("/kill") for path, _body in transport.posts):
            raise sqlite3.OperationalError("injected pre-stop read failure")
        return original_collect(conversation_id)

    monkeypatch.setattr(_run_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(client, "collect_events", fail_until_stop)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_untrusted_watermark_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,
        hard_cap_s=0.05,
    )

    assert record["status"] == "INVALID_RUN"
    product = json.loads(
        (
            tmp_path
            / "out"
            / "run_untrusted_watermark_001"
            / "conversations"
            / _CID
            / "product-evidence.json"
        ).read_text(encoding="utf-8")
    )
    diagnostic = product["diagnostic_stop"]
    assert diagnostic["kill_acknowledged"] is True
    assert diagnostic["pre_stop_watermark_trusted"] is False
    assert diagnostic["release_confirmed"] is False
    assert diagnostic["boundary_seq"] is None
    assert diagnostic["boundary_epoch"] is None
    assert len([path for path, _body in transport.posts if path.endswith("/kill")]) == 2


@pytest.mark.asyncio
async def _impl_test_progressing_cutoff_rejected_kill_retains_retry_and_partial_dossier(
    tmp_path, monkeypatch
):
    async def no_sleep(_seconds: float) -> None:
        return None

    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    transport = _RejectedKillProgressTransport(db, finish_after=None)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    original_collect = client.collect_events

    def fail_after_stop(conversation_id: str):
        if any(path.endswith("/kill") for path, _body in transport.posts):
            raise sqlite3.OperationalError("injected event read failure")
        return original_collect(conversation_id)

    def fail_thrash_snapshot(*_args, **_kwargs):
        raise RuntimeError("injected thrash snapshot failure")

    monkeypatch.setattr(_run_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(client, "collect_events", fail_after_stop)
    monkeypatch.setattr(client, "observe_live_thrash_snapshot", fail_thrash_snapshot)

    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_rejected_stop_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,
        hard_cap_s=0.05,
    )

    assert record["status"] == "INVALID_RUN"
    kills = [path for path, _body in transport.posts if path.endswith("/kill")]
    assert len(kills) == 3  # stop, cleanup attempt, finally fallback
    base = tmp_path / "out" / "run_rejected_stop_001"
    conv = base / "conversations" / _CID
    product = json.loads((conv / "product-evidence.json").read_text(encoding="utf-8"))
    assert product["diagnostic_stop"]["release_confirmed"] is False
    assert product["diagnostic_stop"]["boundary_epoch"] is None
    timeline = (base / "timeline.md").read_text(encoding="utf-8")
    assert "event collection failed: OperationalError" in timeline
    assert "thrash snapshot failed: RuntimeError" in timeline
    manifest = load_manifest(base)
    assert verify_evidence_unchanged(base, manifest).intact


@pytest.mark.asyncio
async def _impl_test_genuinely_inactive_build_is_a_real_finding_not_inconclusive(
    tmp_path, monkeypatch
):
    # The flip side: a genuinely WEDGED build (no new events, frozen status) for the
    # inactivity window IS a real product finding — BUILD_DID_NOT_FINISH — NOT the
    # inconclusive INVALID_RUN reserved for an actively-progressing cutoff.
    db = tmp_path / "disco.db"
    # This is a fake-transport product-adjudication test, not a live Podman probe.
    # Pin an empty host baseline/after state so cleanup applicability cannot change
    # with the developer machine's runtime permissions.
    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())
    events = clean_smoke_log()[:-1]
    screenshot_path = ".pmx/screenshots/nonterminal.png"
    events += [
        action(
            10,
            "browser",
            args={"action": "navigate", "url": "http://localhost:8000/"},
            action_id="act_10",
        ),
        _browser_screenshot_observation(screenshot_path, seq=11),
    ]
    _seed_db(db, _CID, events)
    transport = FakeTransport(
        db,
        states=["RUNNING"] * 6,
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    provider_ledger = tmp_path / "provider.jsonl"
    provider_ledger.write_text("", encoding="utf-8")
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", str(provider_ledger))
    client = DiscoApiClient(
        transport,
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(tmp_path / "projects"),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )
    scenario = _smoke_scenario()
    scenario["followups"] = [
        {
            "trigger": "after_terminal",
            "text": "this must not be sent after inactivity",
            "requires_plan_revision": False,
        }
    ]
    record = await run_once(
        client,
        scenario,
        run_id="run_wedge_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=0.1,  # short inactivity — the wedge trips it
        hard_cap_s=30.0,
    )
    assert record["status"] == "FAIL", record
    assert record["code"] == "BUILD_DID_NOT_FINISH"
    assert record["first_broken_link"] == "required_finish -> terminal_status"
    message_posts = [path for path, _body in transport.posts if path.endswith("/messages")]
    assert len(message_posts) == 1  # initial prompt only; no after-terminal follow-up
    workspace_path = (
        tmp_path / "out" / "run_wedge_001" / "conversations" / _CID / "workspace-manifest.json"
    )
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    assert workspace == {
        "_capture": {
            "drive_status": "INACTIVE_TIMEOUT",
            "reason": "strict atomic workspace evidence requires a durable terminal",
            "status": "not_collected_nonterminal",
        },
        "files": {},
    }
    conv = workspace_path.parent
    browser_diagnostic = json.loads(
        (conv / _run_mod._BROWSER_EVIDENCE_COLLECTION_ERROR_NAME).read_text(encoding="utf-8")
    )
    assert browser_diagnostic == {
        "admissible_as_browser_evidence": False,
        "conversation_id": _CID,
        "facts": {"referenced_paths": [screenshot_path]},
        "kind": "browser_evidence_not_collected_nonterminal",
        "preserved_for": "nonterminal_product_adjudication",
        "reason": "strict browser bytes require the unavailable terminal snapshot",
        "schema_version": 1,
    }
    product_evidence = json.loads((conv / "product-evidence.json").read_text(encoding="utf-8"))
    assert product_evidence["nonterminal_adjudication"] == {
        "drive_status": "INACTIVE_TIMEOUT",
        "frozen_max_seq": 11,
        "schema_version": 1,
        "terminal_baseline_seq": None,
        "terminal_cleanup_slices_not_applicable": ["sidecar"],
        "terminal_only_evidence_unavailable": ["workspace", "browser"],
        "terminal_snapshot_available": False,
    }
    manifest = load_manifest(tmp_path / "out" / "run_wedge_001")
    assert verify_evidence_unchanged(tmp_path / "out" / "run_wedge_001", manifest).intact


@pytest.mark.asyncio
async def _impl_test_inactive_poll_late_terminal_race_uses_strict_snapshot_path(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())  # terminal appears in the frozen event read
    transport = FakeTransport(
        db,
        states=["RUNNING"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    async def inactive(*_args, **_kwargs):
        return INACTIVE_TIMEOUT

    workspace_calls = 0

    async def strict_workspace(_cid, _declared):
        nonlocal workspace_calls
        workspace_calls += 1
        return {"index.html": "<h1>Build Smoke OK</h1>"}

    monkeypatch.setattr(client, "poll_until_terminal_or_gate", inactive)
    monkeypatch.setattr(client, "poll_until_terminal", inactive)
    monkeypatch.setattr(client, "collect_workspace", strict_workspace)

    run = await drive_scenario(
        client,
        _smoke_scenario(),
        model="m",
        autonomous=False,
        timeout_s=0.1,
    )

    assert workspace_calls == 1
    assert run.workspace_manifest == {"index.html": "<h1>Build Smoke OK</h1>"}
    assert not any("not_collected_nonterminal" in line for line in run.timeline)


@pytest.mark.asyncio
async def _impl_test_followup_inactivity_ignores_stale_terminal_and_stops_later_followups(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = FakeTransport(
        db,
        states=["FINISHED"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)
    scripted_statuses = iter(["FINISHED", INACTIVE_TIMEOUT])

    async def scripted_drive(*_args, **_kwargs):
        return next(scripted_statuses)

    sent_followups: list[str] = []

    async def append_followup(cid, content, *, kind="message"):
        del kind
        sent_followups.append(content)
        _insert_event(db, cid, msg(11, "user", content))
        _insert_event(db, cid, status(12, "RUNNING", "planning"))
        return {"event_id": "evt_11", "seq": 11}

    async def strict_workspace_must_not_run(*_args, **_kwargs):
        raise AssertionError("stale initial FINISHED must not enable strict snapshot collection")

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", scripted_drive)
    monkeypatch.setattr(client, "send_followup", append_followup)
    monkeypatch.setattr(client, "collect_workspace", strict_workspace_must_not_run)
    scenario = _smoke_scenario()
    scenario["followups"] = [
        {"trigger": "after_terminal", "text": "first", "requires_plan_revision": False},
        {"trigger": "after_terminal", "text": "second", "requires_plan_revision": False},
    ]

    run = await drive_scenario(
        client,
        scenario,
        model="m",
        autonomous=False,
        timeout_s=0.1,
    )

    assert sent_followups == ["first"]
    assert run.workspace_manifest["_capture"] == {
        "drive_status": INACTIVE_TIMEOUT,
        "reason": "strict atomic workspace evidence requires a durable terminal",
        "status": "not_collected_nonterminal",
    }
    base = assemble_dossier(
        tmp_path / "out", "run_followup_inactive", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "FAIL", classification
    assert classification["code"] == "BUILD_DID_NOT_FINISH"
    assert classification["first_broken_link"] == "required_finish -> terminal_status"
