"""Moved runner hygiene and cleanup implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    LIVE_THRASH_STOP,
    UTC,
    Any,
    BrowserEvidenceCollectionError,
    CollectedRun,
    DiscoApiClient,
    SidecarStopOracle,
    _run_mod,
    cast,
    clean_smoke_log,
    datetime,
    drive_scenario,
    json,
    msg,
    pytest,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _cleanup_run,
    _CleanupKillClient,
    _client,
    _fake_podman,
    _seed_db,
    _smoke_scenario,
    _strict_live_thrash_monitor,
)


@pytest.mark.asyncio
async def _impl_test_h302_live_stop_marker_without_strict_confirmation_keeps_invalid_behavior(
    tmp_path, monkeypatch
):
    scenario = _smoke_scenario()
    events = clean_smoke_log()
    events[-1] = status(10, "IDLE", "killed")
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, events)
    transport = FakeTransport(
        db,
        states=["RUNNING", "IDLE"],
        workspace={"index.html": "<h1>Build Smoke OK</h1>"},
        preview_html="<h1>Build Smoke OK</h1>",
    )
    client = _client(transport, tmp_path)

    async def unconfirmed_stop(*_args, **_kwargs):
        return LIVE_THRASH_STOP

    def missing_screenshot(*_args, **_kwargs):
        raise BrowserEvidenceCollectionError("missing screenshot", {"path": "missing.png"})

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", unconfirmed_stop)
    monkeypatch.setattr(client, "collect_browser_evidence", missing_screenshot)

    with pytest.raises(BrowserEvidenceCollectionError, match="missing screenshot"):
        await drive_scenario(
            client,
            scenario,
            model="m",
            autonomous=False,
            timeout_s=5,
        )


def _impl_test_confirmed_live_thrash_provider_boundary_is_scoped_and_tool_bearing(
    tmp_path, monkeypatch
):
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    first = msg(1, "user", "build")
    first["timestamp"] = started.isoformat()
    killed = status(2, "IDLE", "killed")
    killed["timestamp"] = datetime.fromtimestamp(started.timestamp() + 10, UTC).isoformat()
    run = CollectedRun(
        conversation_id=_CID,
        events=[first, killed],
        state_initial={"execution_status": "RUNNING"},
        state_final={"execution_status": "IDLE"},
        workspace_manifest={},
        preview=None,
        thrash_monitor=_strict_live_thrash_monitor(),
    )
    ledger = tmp_path / "provider.jsonl"
    records = [
        {
            "ts": started.timestamp() + 2,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": _CID,
        },
        {
            "ts": started.timestamp() + 11,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": _CID,
        },
        {
            "ts": started.timestamp() + 12,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": "conv_other",
        },
        {
            "ts": started.timestamp() + 13,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": False,
            "conversation_id": _CID,
        },
    ]
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    monkeypatch.setattr(_run_mod, "_relay_log_path", lambda: str(ledger))

    scoped = _run_mod._provider_ledger_for_run(run)
    assert scoped is not None
    assert len(scoped) == 3
    assert [record["after_terminal"] for record in scoped] == [False, True, False]

    run.thrash_monitor = {}
    assert _run_mod._provider_ledger_for_run(run) is None


def _impl_test_progress_timeout_diagnostic_stop_has_scoped_provider_boundary(tmp_path, monkeypatch):
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    first = msg(1, "user", "build")
    first["timestamp"] = started.isoformat()
    stale_terminal = status(2, "FINISHED")
    stale_terminal["timestamp"] = datetime.fromtimestamp(started.timestamp() + 1, UTC).isoformat()
    followup = msg(3, "user", "revise")
    followup["timestamp"] = datetime.fromtimestamp(started.timestamp() + 2, UTC).isoformat()
    killed = status(4, "IDLE", "killed")
    killed["timestamp"] = datetime.fromtimestamp(started.timestamp() + 10, UTC).isoformat()
    run = CollectedRun(
        conversation_id=_CID,
        events=[first, stale_terminal, followup, killed],
        state_initial={"execution_status": "RUNNING"},
        state_final={"execution_status": "IDLE"},
        workspace_manifest={},
        preview=None,
        diagnostic_stop="progressing_hard_cap",
        diagnostic_stop_epoch=started.timestamp() + 10,
        diagnostic_stop_seq=4,
        diagnostic_release_confirmed=True,
    )
    ledger = tmp_path / "provider.jsonl"
    records = [
        {
            "ts": started.timestamp() + 2,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": _CID,
        },
        {
            "ts": started.timestamp() + 11,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": _CID,
        },
        {
            "ts": started.timestamp() + 12,
            "host": "opencode.ai",
            "model": "deepseek-v4-flash",
            "has_tools": True,
            "conversation_id": "conv_other",
        },
    ]
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    monkeypatch.setattr(_run_mod, "_relay_log_path", lambda: str(ledger))

    scoped = _run_mod._provider_ledger_for_run(run)
    assert scoped is not None
    assert len(scoped) == 2
    assert [record["after_terminal"] for record in scoped] == [False, True]

    run.diagnostic_stop = "other_invalid_stop"
    assert _run_mod._provider_ledger_for_run(run) is None
    run.diagnostic_stop = "progressing_hard_cap"
    run.diagnostic_stop_epoch = None
    assert _run_mod._provider_ledger_for_run(run) is None


def _impl_test_progress_timeout_boundary_ignores_earlier_cancel_kill(tmp_path, monkeypatch):
    started = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
    events = [
        msg(1, "user", "build"),
        status(2, "IDLE", "killed"),
        msg(3, "user", "continue"),
        status(4, "RUNNING", "planning"),
        status(5, "IDLE", "killed"),
    ]
    for offset, event in zip((0, 2, 3, 4, 10), events, strict=True):
        event["timestamp"] = datetime.fromtimestamp(started.timestamp() + offset, UTC).isoformat()
    run = CollectedRun(
        conversation_id=_CID,
        events=events,
        state_initial={"execution_status": "RUNNING"},
        state_final={"execution_status": "IDLE"},
        workspace_manifest={},
        preview=None,
        diagnostic_stop="progressing_hard_cap",
        diagnostic_stop_epoch=started.timestamp() + 10,
        diagnostic_stop_seq=5,
        diagnostic_release_confirmed=True,
    )
    ledger = tmp_path / "provider.jsonl"
    ledger.write_text(
        "".join(
            json.dumps(
                {
                    "ts": started.timestamp() + offset,
                    "host": "opencode.ai",
                    "model": "deepseek-v4-flash",
                    "has_tools": True,
                    "conversation_id": _CID,
                }
            )
            + "\n"
            for offset in (5, 11)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_run_mod, "_relay_log_path", lambda: str(ledger))

    scoped = _run_mod._provider_ledger_for_run(run)
    assert scoped is not None
    assert [record["after_terminal"] for record in scoped] == [False, True]


@pytest.mark.asyncio
async def _impl_test_confirmed_live_thrash_cleanup_requires_pre_stop_provider_call(
    tmp_path, monkeypatch
):
    async def no_sleep(_seconds: float) -> None:
        return None

    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    first = msg(1, "user", "build")
    first["timestamp"] = started.isoformat()
    killed = status(2, "IDLE", "killed")
    killed["timestamp"] = datetime.fromtimestamp(started.timestamp() + 10, UTC).isoformat()
    run = CollectedRun(
        conversation_id=_CID,
        events=[first, killed],
        state_initial={"execution_status": "RUNNING"},
        state_final={"execution_status": "IDLE"},
        workspace_manifest={},
        preview=None,
        thrash_monitor=_strict_live_thrash_monitor(),
    )
    ledger = tmp_path / "provider.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "ts": started.timestamp() + 11,
                "host": "opencode.ai",
                "model": "deepseek-v4-flash",
                "has_tools": True,
                "conversation_id": _CID,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_run_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(_run_mod, "_live_disco_container_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: [])
    monkeypatch.setattr(_run_mod, "_dangling_volume_names", lambda: set())

    evidence = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        _CID,
        run,
        baseline_containers=0,
        relay_log=str(ledger),
        timeline=[],
        baseline_dangling_volumes=set(),
        grace_s=0.0,
    )

    assert "lifecycle" in evidence
    assert "cleanup" in evidence
    assert "sidecar" not in evidence


@pytest.mark.asyncio
async def _impl_test_provider_calls_after_terminal_are_conversation_scoped(tmp_path, monkeypatch):
    class _KillClient:
        def __init__(self) -> None:
            self.killed: list[str] = []

        async def kill(self, cid: str) -> dict[str, Any]:
            self.killed.append(cid)
            return {
                "http_status": 200,
                "killed": True,
                "state": {"execution_status": "IDLE"},
            }

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(_run_mod, "_live_disco_container_count", lambda: 0)

    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC).timestamp()
    terminal = start + 10
    events = [
        {"kind": "message", "timestamp": datetime.fromtimestamp(start, UTC).isoformat()},
        {
            "kind": "status",
            "status": "FINISHED",
            "timestamp": datetime.fromtimestamp(terminal, UTC).isoformat(),
        },
    ]

    async def collect(records: list[dict]) -> dict:
        relay = tmp_path / "relay.jsonl"
        relay.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        run = CollectedRun(
            conversation_id="conv_terminal",
            events=events,
            state_initial={},
            state_final={"status": "FINISHED"},
            workspace_manifest={},
            preview=None,
            timeline=["RUNNING", "FINISHED"],
        )
        return await _run_mod._collect_terminal_cleanup_evidence(
            cast(DiscoApiClient, _KillClient()),
            "conv_terminal",
            run,
            baseline_containers=0,
            relay_log=str(relay),
            timeline=[],
            grace_s=0.0,
        )

    overlap = [
        {
            "ts": start + 2,
            "host": "api.minimaxi.chat",
            "model": "MiniMax-M3",
            "has_tools": True,
            "conversation_id": "conv_terminal",
        },
        {
            "ts": terminal + 1,
            "host": "api.minimaxi.chat",
            "model": "MiniMax-M3",
            "has_tools": True,
            "conversation_id": "conv_other_lane",
        },
    ]
    ev = await collect(overlap)
    assert ev["sidecar"]["provider_calls_after_terminal"] == 0
    assert SidecarStopOracle().check(product_evidence=ev)[0].passed

    same_conversation = [*overlap, {**overlap[-1], "conversation_id": "conv_terminal"}]
    ev = await collect(same_conversation)
    assert ev["sidecar"]["provider_calls_after_terminal"] == 1
    r = SidecarStopOracle().check(product_evidence=ev)[0]
    assert r.failed and r.code == "SIDECAR_NOT_STOPPED"


@pytest.mark.asyncio
async def _impl_test_cleanup_orphans_are_scoped_to_this_conversation_sandbox_ids(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(
        monkeypatch,
        ps_stdout="\n".join(
            [
                "disco-sbx-sbx_this_conv",
                "disco-sbx-sbx_other_conv",
                "disco-egr-sbx_other_conv",
                "unrelated-container",
            ]
        ),
        volume_stdout="",
        dangling_stdout="",
    )
    run = _cleanup_run(
        {"status": "FINISHED", "extras": {"sandbox_instance_ids": ["sbx_this_conv"]}}
    )

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        "conv_terminal",
        run,
        baseline_containers=0,
        relay_log=None,
        timeline=[],
        baseline_dangling_volumes=set(),
        grace_s=0.0,
    )

    assert ev["cleanup"] == {
        "orphans": 1,
        "workspace_released": False,
        "scope": "conversation",
        "container_orphans": 1,
        "volume_orphans": 0,
        "volume_scope": "conversation",
    }


@pytest.mark.asyncio
async def _impl_test_cleanup_scoped_count_ignores_other_conversation_live_sandboxes(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(
        monkeypatch,
        ps_stdout="\n".join(["disco-sbx-sbx_other_conv", "disco-egr-sbx_other_conv"]),
        volume_stdout="",
        dangling_stdout="",
    )
    run = _cleanup_run()

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(
            DiscoApiClient,
            _CleanupKillClient(
                {
                    "http_status": 200,
                    "killed": True,
                    "state": {"execution_status": "IDLE"},
                    "sandbox_instance_ids": ["sbx_this_conv"],
                }
            ),
        ),
        "conv_terminal",
        run,
        baseline_containers=0,
        relay_log=None,
        timeline=[],
        baseline_dangling_volumes=set(),
        grace_s=0.0,
    )

    assert ev["cleanup"] == {
        "orphans": 0,
        "workspace_released": True,
        "scope": "conversation",
        "container_orphans": 0,
        "volume_orphans": 0,
        "volume_scope": "conversation",
    }


@pytest.mark.asyncio
async def _impl_test_cleanup_counts_scoped_leftover_workspace_volume_as_orphan(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(
        monkeypatch,
        ps_stdout="",
        volume_stdout="\n".join(["disco-ws-sbx_this_conv", "disco-ws-sbx_other_conv"]),
        dangling_stdout="",
    )
    run = _cleanup_run(
        {"status": "FINISHED", "extras": {"sandbox_instance_ids": ["sbx_this_conv"]}}
    )

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        "conv_terminal",
        run,
        baseline_containers=0,
        relay_log=None,
        timeline=[],
        baseline_dangling_volumes=set(),
        grace_s=0.0,
    )

    assert ev["cleanup"] == {
        "orphans": 1,
        "workspace_released": False,
        "scope": "conversation",
        "container_orphans": 0,
        "volume_orphans": 1,
        "volume_scope": "conversation",
    }


@pytest.mark.asyncio
async def _impl_test_cleanup_orphan_count_falls_back_to_global_delta_without_sandbox_ids(
    monkeypatch,
):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(
        monkeypatch,
        ps_stdout="\n".join(
            [
                "disco-sbx-sbx_before_a",
                "disco-egr-sbx_before_a",
                "disco-sbx-sbx_after_b",
                "disco-egr-sbx_after_b",
            ]
        ),
        volume_stdout="",
        dangling_stdout="\n".join(["pre_existing", "new_run_volume"]),
    )
    run = _cleanup_run({"status": "FINISHED", "extras": {}})

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        "conv_terminal",
        run,
        baseline_containers=2,
        relay_log=None,
        timeline=[],
        baseline_dangling_volumes={"pre_existing"},
        grace_s=0.0,
    )

    assert ev["cleanup"] == {
        "orphans": 3,
        "workspace_released": False,
        "scope": "global",
        "container_orphans": 2,
        "volume_orphans": 1,
        "volume_scope": "global_dangling",
    }


@pytest.mark.asyncio
async def _impl_test_cleanup_progress_timeout_uses_preserved_kill_id_in_parallel(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(
        monkeypatch,
        ps_stdout="\n".join(["disco-sbx-sbx_other_conv", "disco-egr-sbx_other_conv"]),
        volume_stdout="",
        dangling_stdout="",
    )
    run = _cleanup_run({"status": "IDLE", "extras": {}})
    run.diagnostic_stop = "progressing_hard_cap"
    run.diagnostic_release_confirmed = True
    run.product_evidence = {
        "diagnostic_stop": {
            "kind": "progressing_hard_cap",
            "sandbox_instance_ids": ["sbx_this_conv"],
        }
    }

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        "conv_terminal",
        run,
        baseline_containers=0,
        relay_log=None,
        timeline=[],
        baseline_dangling_volumes=set(),
        grace_s=0.0,
        allow_global_cleanup_fallback=False,
    )

    assert ev["cleanup"] == {
        "orphans": 0,
        "workspace_released": True,
        "scope": "conversation",
        "container_orphans": 0,
        "volume_orphans": 0,
        "volume_scope": "conversation",
    }


@pytest.mark.asyncio
async def _impl_test_cleanup_parallel_without_id_refuses_global_attribution(monkeypatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(
        monkeypatch,
        ps_stdout="\n".join(["disco-sbx-sbx_other_conv", "disco-egr-sbx_other_conv"]),
        volume_stdout="",
        dangling_stdout="",
    )
    run = _cleanup_run({"status": "IDLE", "extras": {}})
    run.diagnostic_stop = "progressing_hard_cap"
    run.diagnostic_release_confirmed = True
    timeline: list[str] = []

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        "conv_terminal",
        run,
        baseline_containers=0,
        relay_log=None,
        timeline=timeline,
        baseline_dangling_volumes=set(),
        grace_s=0.0,
        allow_global_cleanup_fallback=False,
    )

    assert "cleanup" not in ev
    assert any("refused contaminated global container delta" in line for line in timeline)


@pytest.mark.asyncio
async def _impl_test_cleanup_extracts_all_frozen_event_stream_sandbox_ids(monkeypatch):
    """Restart evidence owns every generation, not only the terminal snapshot."""
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(
        monkeypatch,
        ps_stdout="\n".join(
            [
                "disco-sbx-sbx_before_restart",
                "disco-egr-sbx_before_restart",
                "disco-sbx-sbx_after_restart",
                "disco-egr-sbx_after_restart",
                "disco-sbx-sbx_other_lane",
            ]
        ),
        volume_stdout="",
        dangling_stdout="",
    )
    run = _cleanup_run({"status": "FINISHED", "extras": {}})
    normalized_events = [
        {
            "tool_result": {
                "structured": {"sandbox_instance_id": "sbx_before_restart"}
            }
        },
        {"preview_selection": {"sandbox_instance_id": "sbx_after_restart"}},
    ]
    run.events = [
        {
            "seq": seq,
            "kind": "observation",
            "payload": json.dumps({"kind": "observation", **event}),
        }
        for seq, event in enumerate(normalized_events, start=1)
    ]

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        "conv_terminal",
        run,
        baseline_containers=0,
        relay_log=None,
        timeline=[],
        baseline_dangling_volumes=set(),
        grace_s=0.0,
        allow_global_cleanup_fallback=False,
    )

    assert ev["cleanup"]["scope"] == "conversation"
    assert ev["cleanup"]["container_orphans"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def _impl_test_cleanup_scoped_volume_probe_failure_omits_adjudication(monkeypatch, parallel):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_run_mod.asyncio, "sleep", _no_sleep)
    _fake_podman(monkeypatch, ps_stdout="", volume_stdout="", dangling_stdout="")
    monkeypatch.setattr(_run_mod, "_disco_volume_names", lambda: None)
    run = _cleanup_run(
        {"status": "FINISHED", "extras": {"sandbox_instance_ids": ["sbx_this_conv"]}}
    )
    timeline: list[str] = []

    ev = await _run_mod._collect_terminal_cleanup_evidence(
        cast(DiscoApiClient, _CleanupKillClient()),
        "conv_terminal",
        run,
        baseline_containers=0,
        relay_log=None,
        timeline=timeline,
        baseline_dangling_volumes=set(),
        grace_s=0.0,
        allow_global_cleanup_fallback=not parallel,
    )

    assert "cleanup" not in ev
    assert any("applicable volume probe was unavailable" in line for line in timeline)


@pytest.mark.asyncio
async def _impl_test_kill_is_idempotent_on_already_terminal_conv(tmp_path):
    # The kill adapter method is harmless/idempotent on an already-terminal conversation
    # (the route is always-available); [REL-4] _release_conversation now RELEASES terminal
    # convs too (to tear down the lingering sandbox + sidecar containers), so this idempotent
    # kill must succeed cleanly and never crash teardown.
    db = tmp_path / "disco.db"
    transport = FakeTransport(db, states=["FINISHED"])
    client = _client(transport, tmp_path)
    resp = await client.kill(_CID)
    assert resp["http_status"] == 200
    assert any(p[0] == f"/conversations/{_CID}/kill" for p in transport.posts)


@pytest.mark.asyncio
async def _impl_test_release_conversation_swallows_unreachable_server(tmp_path):
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
