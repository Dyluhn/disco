"""Moved a non terminal run is collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    _disco_mod,
    _run_mod,
    action,
    clean_smoke_log,
    hashlib,
    json,
    pytest,
    run_once,
)
from .helpers_01 import (
    FakeTransport,
    _browser_screenshot_observation,
    _plant_snapshot,
    _SandboxIdProgressTransport,
    _seed_db,
    _smoke_log_with_file_write,
    _smoke_scenario,
)
from .helpers_02 import _FreezableProgressTransport


def _impl_test_snapshot_wait_does_not_mask_a_run_that_never_finished():
    """A run with no SYSTEM FINISHED terminal has no agent-final state.

    Demanding the snapshot converge on one is demanding something that cannot
    exist, and raising there made the snapshot the reported cause while the real
    one — a STUCK run released at the clarify cap — was buried. It happened three
    times this campaign (seeds 440023, 450000, 440019), each time costing a
    counted stream to re-diagnose.
    """
    assert _disco_mod._NO_SYSTEM_FINISHED_TERMINAL == "latest status is not exact SYSTEM FINISHED"

    # The producer emits exactly the sentinel the snapshot wait keys on, so the
    # two cannot drift apart.
    evidence = _disco_mod._strict_final_workspace_seal(
        [
            {"kind": "status", "seq": 1, "source": "system", "status": "RUNNING"},
            {"kind": "status", "seq": 2, "source": "system", "status": "STUCK"},
        ],
        "conv_mask_probe",
    )
    assert evidence.error == _disco_mod._NO_SYSTEM_FINISHED_TERMINAL


def _impl_test_seal_evidence_still_reports_a_finished_run_normally():
    """Regression guard: the exemption keys on 'never finished', so a run that DID
    finish keeps its full snapshot demand."""
    evidence = _disco_mod._strict_final_workspace_seal(
        [
            {"kind": "status", "seq": 1, "source": "system", "status": "RUNNING"},
            {"kind": "status", "seq": 2, "source": "system", "status": "FINISHED"},
        ],
        "conv_mask_probe",
    )
    assert evidence.error != _disco_mod._NO_SYSTEM_FINISHED_TERMINAL


@pytest.mark.asyncio
async def _impl_test_non_terminal_run_returns_observed_state_instead_of_masking(tmp_path):
    """A run whose LIVE status never reached a terminal has no agent-final state.

    The snapshot then returns what it observed and the ordinary oracles judge the
    run on its real status — a stricter outcome than INVALID_RUN, which does not
    count. The sibling fail-closed tests cover the other side: a live-FINISHED run
    missing its terminal EVENT is evidence loss and still raises.
    """
    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    events = _smoke_log_with_file_write("index.html", "<h1>bytes</h1>")[:-1]
    _seed_db(db, _CID, events)
    _plant_snapshot(projects, _CID, {"index.html": "<h1>bytes</h1>"})
    client = DiscoApiClient(
        # LIVE status is STUCK: the run really did not finish.
        FakeTransport(db, states=["STUCK"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
        snapshot_wait_s=0.0,
        require_workspace_commit=True,
    )

    manifest = await client.collect_workspace(_CID, ["index.html"])

    assert isinstance(manifest, dict)  # observed state, not a masking exception


def _impl_test_browser_evidence_after_the_freeze_horizon_is_not_certifiable(tmp_path):
    """A screenshot referenced AFTER the accepted event horizon cannot be certified.

    The pre-kill freeze binds evidence to one exact immutable version named by a
    `WorkspaceVersionEvent`.  Bytes referenced after that event are not in the frozen
    version, so certifying them would assert evidence the frozen workspace does not
    carry.  Without the horizon the same call legitimately captures both -- the
    control below proves the clip is what excludes the late reference, not some
    unrelated eligibility rule.
    """
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    ws = _plant_snapshot(proj, _CID, {"index.html": "<h1>frozen</h1>"})

    in_horizon_rel = ".pmx/screenshots/0001-navigate.png"
    in_horizon = b"\x89PNG\r\n\x1a\nbefore-the-freeze"
    (ws / in_horizon_rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / in_horizon_rel).write_bytes(in_horizon)

    post_horizon_rel = ".pmx/screenshots/0002-after.png"
    (ws / post_horizon_rel).write_bytes(b"\x89PNG\r\n\x1a\nafter-the-freeze")

    events = clean_smoke_log()
    events[-2]["seq"] = 15
    events[-2]["id"] = "evt_15"
    events[-1]["seq"] = 16
    events[-1]["id"] = "evt_16"
    events[-2:-2] = [
        action(11, "browser", action_id="act_11"),
        _browser_screenshot_observation(in_horizon_rel, seq=12),
        action(13, "browser", action_id="act_13"),
        _browser_screenshot_observation(post_horizon_rel, seq=14),
    ]
    _seed_db(db, _CID, events)
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"], workspace={}),
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(proj),
    )
    workspace = client._read_snapshot_manifest(_CID, ["index.html"], ws)

    # CONTROL: with no horizon both references are eligible.
    assert set(client.collect_browser_evidence(_CID, events, workspace)) == {
        in_horizon_rel,
        post_horizon_rel,
    }

    # The freeze horizon is the version event at seq 12: seq 14 is excluded.
    captured = client.collect_browser_evidence(_CID, events, workspace, horizon_seq=12)

    assert captured == {in_horizon_rel: in_horizon}


@pytest.mark.asyncio
async def _impl_test_failed_freeze_is_subordinate_and_never_launders_the_primary_verdict(
    tmp_path, monkeypatch
):
    """A freeze that does not land must not change what the run is judged to be.

    The hard cap's verdict is RUN_TIMEOUT_WHILE_PROGRESSING: the build was still
    progressing when it was bounded. If the pre-kill freeze then times out, that is a
    SUBORDINATE fact about evidence capture -- it is disclosed, but it must not
    overwrite the primary verdict, must not claim any workspace or browser truth, and
    must not change kill semantics.  Laundering a primary verdict behind a lesser
    secondary failure is pattern P7.

    This drives the REAL run_once -> drive_scenario hard-cap path, not a helper.
    """
    db = tmp_path / "disco.db"
    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())
    _seed_db(db, _CID, clean_smoke_log()[:-1])
    # This transport never emits a durable PAUSED, so the freeze cannot land.
    transport = _SandboxIdProgressTransport(db, finish_after=None)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_freeze_timeout_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,
        hard_cap_s=0.2,
        parallel_workers=10,
    )

    # 1. the PRIMARY verdict survives the subordinate failure
    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_TIMEOUT_WHILE_PROGRESSING"
    assert record["code"] != "FREEZE_TIMEOUT", "a freeze failure must not become the verdict"

    # 2. kill semantics unchanged: exactly one kill, as before the freeze existed
    assert len([p for p, _b in transport.posts if p.endswith("/kill")]) == 1

    conv = tmp_path / "out" / "run_freeze_timeout_001" / "conversations" / _CID
    evidence = json.loads((conv / "product-evidence.json").read_text())
    freeze = evidence["diagnostic_stop"]["workspace_freeze"]

    # 3. the failure is DISCLOSED, with a reason -- never implied or silent
    assert freeze["status"] == "FREEZE_TIMEOUT"
    assert freeze["reason"], "a failed freeze must say why"
    assert freeze["horizon_seq"] is None
    assert freeze["tree_digest"] is None

    # 4. and it claims NO workspace truth
    assert json.loads((conv / "workspace-manifest.json").read_text()) == {}


def _assert_hardcap_freeze_identity(transport, version, freeze) -> None:
    # The pause actually reached a step boundary and the version event followed it.
    assert transport.paused_at is not None
    assert transport.version_at > transport.paused_at

    # The freeze LANDED and names the exact immutable version + horizon.
    assert freeze["status"] == "frozen", freeze.get("reason")
    assert freeze["version_seq"] == version.seq
    assert freeze["paused_seq"] == transport.paused_at
    assert freeze["horizon_seq"] == transport.version_at
    assert freeze["tree_digest"] == version.tree_digest


def _assert_hardcap_freeze_artifacts(conv, content, screenshot_rel, screenshot) -> None:
    # The artifact survived the kill, byte-exact.
    manifest = json.loads((conv / "workspace-manifest.json").read_text())
    entry = manifest["index.html"]
    assert entry["present"] is True
    assert entry["sha256"] == hashlib.sha256(content.encode()).hexdigest()

    # So did the PNG -- read from the frozen immutable version.
    assert (conv / "browser-evidence" / screenshot_rel).read_bytes() == screenshot


def _assert_hardcap_freeze_outcome(record, transport, conv, evidence) -> None:
    # The primary verdict is unchanged, and kill semantics are untouched.
    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "RUN_TIMEOUT_WHILE_PROGRESSING"
    assert len([p for p, _b in transport.posts if p.endswith("/kill")]) == 1

    # Inspect is finalized AFTER the kill.
    assert (conv / "inspect-trace.json").is_file()

    # The run owns zero resources afterwards.
    product = json.loads((conv / "product-evidence.json").read_text(encoding="utf-8"))
    assert product["cleanup"]["scope"] == "conversation"
    assert product["cleanup"]["container_orphans"] == 0
    assert evidence["diagnostic_stop"]["release_confirmed"] is True


@pytest.mark.asyncio
async def _impl_test_progressing_hardcap_freeze_preserves_artifact_and_png_across_kill(
    tmp_path, monkeypatch
):
    """THE Ruling-2 positive, driven through the REAL run_once hard-cap path.

    Kill destroys the executor/sandbox without snapshotting, so reading the durable
    store afterwards returned only the pre-run import snapshot and every byte the
    progressing run had written was reported missing.  The corrected sequence pauses
    first, waits for a durable PAUSED and the WorkspaceVersionEvent that follows it,
    and reads THAT exact immutable version.

    Critically this exercises real SQLite rows.  `collect_events` returns
    {seq, kind, source, id, created_at, payload-as-JSON-string}; a freeze that read
    `status`/`trigger` straight off a row would see None every time and time out on
    every real run, while still passing any test whose fake returns flat dicts.
    """
    from disco.tools.projects.store import ProjectStore

    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>written while progressing</h1>"
    screenshot_rel = ".pmx/screenshots/0001-navigate.png"
    screenshot = b"\x89PNG\r\n\x1a\nprogressing-visual-proof\xff\x00"

    # The run's work exists in the workspace BEFORE the version is cut.
    ws = _plant_snapshot(projects, _CID, {"index.html": content})
    (ws / screenshot_rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / screenshot_rel).write_bytes(screenshot)
    version = ProjectStore(str(projects)).cut_version(_CID, trigger="PAUSED")
    assert version is not None

    # A browser observation references the PNG, at a seq before the freeze horizon.
    events = _smoke_log_with_file_write("index.html", content)[:-1]
    events.append(action(40, "browser", action_id="act_40"))
    events.append(_browser_screenshot_observation(screenshot_rel, seq=41))
    _seed_db(db, _CID, events)

    transport = _FreezableProgressTransport(db, version_seq=version.seq, finish_after=None)
    client = DiscoApiClient(
        transport,
        db_path=str(db),
        poll_interval_s=0.0,
        projects_root=str(projects),
    )

    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_freeze_ok_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,
        hard_cap_s=0.2,
        parallel_workers=10,
    )

    conv = tmp_path / "out" / "run_freeze_ok_001" / "conversations" / _CID
    evidence = json.loads((conv / "product-evidence.json").read_text())
    freeze = evidence["diagnostic_stop"]["workspace_freeze"]
    _assert_hardcap_freeze_identity(transport, version, freeze)
    _assert_hardcap_freeze_artifacts(conv, content, screenshot_rel, screenshot)
    _assert_hardcap_freeze_outcome(record, transport, conv, evidence)


@pytest.mark.asyncio
async def _impl_test_tampered_immutable_version_makes_the_freeze_fail_closed(tmp_path, monkeypatch):
    """Identity is re-verified AROUND collection, not trusted from the event.

    The WorkspaceVersionEvent names a version; it does not prove the bytes on disk
    still match it.  `_verified_workspace_version` rescans every regular file
    (never following symlinks) and checks size and sha256 against the version
    record.  If those disagree the freeze must claim NOTHING -- reporting `frozen`
    over tampered bytes would certify evidence that is not the run's work.
    """
    from disco.tools.projects.store import ProjectStore

    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())

    db = tmp_path / "disco.db"
    projects = tmp_path / "projects"
    content = "<h1>written while progressing</h1>"
    _plant_snapshot(projects, _CID, {"index.html": content})
    store = ProjectStore(str(projects))
    version = store.cut_version(_CID, trigger="PAUSED")
    assert version is not None

    # Tamper the immutable version AFTER it was published.
    immutable = store.version_workspace_path(_CID, version.seq)
    (immutable / "index.html").write_text("tampered after publication", encoding="utf-8")

    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", content)[:-1])
    transport = _FreezableProgressTransport(db, version_seq=version.seq, finish_after=None)
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(projects)
    )

    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_freeze_tampered_001",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=10.0,
        hard_cap_s=0.2,
        parallel_workers=10,
    )

    conv = tmp_path / "out" / "run_freeze_tampered_001" / "conversations" / _CID
    freeze = json.loads((conv / "product-evidence.json").read_text())["diagnostic_stop"][
        "workspace_freeze"
    ]

    assert freeze["status"] == "FREEZE_TIMEOUT"
    assert "freshly verified" in (freeze["reason"] or "")
    assert freeze["tree_digest"] is None
    # No workspace truth, and the primary verdict is still the hard cap's.
    assert json.loads((conv / "workspace-manifest.json").read_text()) == {}
    assert record["code"] == "RUN_TIMEOUT_WHILE_PROGRESSING"
