"""Moved scenarios yaml loads shapes collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    LIVE_THRASH_STOP,
    UTC,
    CollectedRun,
    ContractOracle,
    Path,
    _run_mod,
    action,
    clean_smoke_log,
    datetime,
    json,
    load_scenarios,
    observation,
    pytest,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _smoke_scenario,
    _strict_live_thrash_monitor,
)


def _impl_test_scenarios_yaml_parses_all_15_scenarios():
    scen = load_scenarios()
    assert {"static_html_minimal", "must_plan_before_tool", "revise_after_finish"} <= set(scen)
    # the §15.4 steer scenario carries the after_first_file_write trigger
    steer = scen["steer_while_running_requires_plan_update_or_clear_execution_note"]
    assert steer["followups"][0]["trigger"] == "after_first_file_write"
    devserver = scen["diag_devserver"]
    assert "PORT" in devserver["prompt"]
    assert "os.environ.get" in devserver["prompt"]
    assert "8000" in devserver["prompt"]
    assert "preview_start" in devserver["prompt"]
    assert "never launch or kill a web server through shell" in devserver["prompt"]
    assert devserver["assertions"]["preview"] == {
        "required": True,
        "must_contain": ["Live Server Up"],
    }
    assert devserver["assertions"]["browser_verification"]["required"] is True


def _impl_test_phase4_counted_scenarios_are_frozen_and_contract_valid() -> None:
    path = Path(__file__).resolve().parents[2] / "scenarios_phase4.yaml"
    scenarios = load_scenarios(path)

    assert len(scenarios) == 23
    assert sum(scenario.get("appkit") is True for scenario in scenarios.values()) == 5
    assert {
        "p4_ff_static_basic",
        "p4_ff_react_basic",
        "p4_ff_node_basic",
        "p4_ff_python_basic",
        "p4_ff_import_basic",
        "p4_ff_context_catalog",
        "p4_appkit_create",
        "p4_appkit_restart",
    } <= set(scenarios)

    evidence = {
        "events",
        "workspace",
        "preview",
        "tool_scope",
        "product_evidence",
        "browser_verification",
    }
    for scenario_id, scenario in scenarios.items():
        result = ContractOracle().check(scenario, available_evidence=evidence)[0]
        assert result.passed, (scenario_id, result.to_dict())
        assert "{{seed}}" in json.dumps(scenario)


def _impl_test_live_thrash_monitor_confirms_repeated_model_repair(tmp_path):
    transport = FakeTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)
    scenario = _smoke_scenario()
    client.enable_live_thrash_monitor(scenario)
    trace = {
        "spans": [
            {"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"},
            {"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"},
        ]
    }

    assert not client.observe_live_thrash_snapshot([], trace, terminal_status="RUNNING")
    assert client.live_thrash_monitor["findings"] == []
    assert client.observe_live_thrash_snapshot([], trace, terminal_status="RUNNING")

    monitor = client.live_thrash_monitor
    assert monitor["enabled"] is True
    assert monitor["sample_count"] == 2
    assert len(monitor["findings"]) == 1
    assert monitor["findings"][0]["oracle_results"][0]["code"] == "MODEL_REPAIR_THRASH"


def _impl_test_live_thrash_monitor_normalizes_sqlite_rows_before_adjudication(tmp_path):
    client = _client(FakeTransport(tmp_path / "disco.db", states=["RUNNING"]), tmp_path)
    client.enable_live_thrash_monitor(_smoke_scenario())
    rows = [
        {
            "seq": event["seq"],
            "kind": event["kind"],
            "source": event["source"],
            "id": event["id"],
            "created_at": event.get("timestamp", "2026-01-01T00:00:00Z"),
            "payload": json.dumps(event),
        }
        for event in clean_smoke_log()
    ]

    assert not client.observe_live_thrash_snapshot(rows, None, terminal_status="RUNNING")
    assert not client.observe_live_thrash_snapshot(rows, None, terminal_status="RUNNING")
    assert client.live_thrash_monitor["findings"] == []


def _impl_test_live_thrash_monitor_distinguishes_recovery_from_restart_loop(tmp_path):
    scenario = _smoke_scenario()
    recovery = [
        action(
            22,
            "shell_exec",
            action_id="start22",
            args={"command": "python3 /workspace/server.py &", "session": "server"},
        ),
        observation(23, "start22", tool="shell_exec"),
        action(26, "shell_kill_process", action_id="kill26", args={"session": "server"}),
        observation(27, "kill26", tool="shell_kill_process"),
        action(
            28,
            "shell",
            action_id="start28",
            args={"command": "kill 88; sleep 1; python3 /workspace/server.py &"},
        ),
        observation(29, "start28"),
        action(34, "shell", action_id="kill34", args={"command": "fuser -k 8000/tcp"}),
        observation(35, "kill34"),
        action(
            36,
            "shell_exec",
            action_id="start36",
            args={"command": "python3 /workspace/server.py &", "session": "server"},
        ),
        observation(37, "start36", tool="shell_exec"),
    ]
    client = _client(FakeTransport(tmp_path / "recovery.db", states=["RUNNING"]), tmp_path)
    client.enable_live_thrash_monitor(scenario)
    assert not client.observe_live_thrash_snapshot(recovery, None, terminal_status="RUNNING")
    assert not client.observe_live_thrash_snapshot(recovery, None, terminal_status="RUNNING")
    assert client.live_thrash_monitor["findings"] == []

    loop = list(recovery)
    loop += [
        action(38, "shell", action_id="kill38", args={"command": "pkill -f server.py"}),
        observation(39, "kill38"),
        action(
            40,
            "shell_exec",
            action_id="start40",
            args={"command": "python3 /workspace/server.py &", "session": "server2"},
        ),
        observation(41, "start40", tool="shell_exec"),
    ]
    looping_client = _client(FakeTransport(tmp_path / "loop.db", states=["RUNNING"]), tmp_path)
    looping_client.enable_live_thrash_monitor(scenario)
    assert not looping_client.observe_live_thrash_snapshot(loop, None, terminal_status="RUNNING")
    assert looping_client.observe_live_thrash_snapshot(loop, None, terminal_status="RUNNING")
    finding = looping_client.live_thrash_monitor["findings"][0]
    oracle = finding["oracle_results"][0]
    assert oracle["first_broken_link"] == "tool_call -> repeated_background_script_restart"


@pytest.mark.parametrize(
    "malformation",
    [
        "disabled",
        "one_sample",
        "sample_count_too_small",
        "wrong_oracle",
        "missing_detection_epoch",
        "nonfinite_detection_epoch",
        "stale_detection_epoch",
        "terminal_finding",
    ],
)
def _impl_test_killed_idle_audit_boundary_requires_strict_live_thrash_monitor(malformation):
    monitor = _strict_live_thrash_monitor()
    if malformation == "disabled":
        monitor["enabled"] = False
    elif malformation == "one_sample":
        monitor["minimum_confirmation_samples"] = 1
        monitor["findings"][0]["confirmation_samples"] = 1
    elif malformation == "sample_count_too_small":
        monitor["sample_count"] = 1
    elif malformation == "wrong_oracle":
        monitor["findings"][0]["oracle_results"][0]["oracle"] = "OtherOracle"
    elif malformation == "missing_detection_epoch":
        del monitor["findings"][0]["detected_at_epoch"]
    elif malformation == "nonfinite_detection_epoch":
        monitor["findings"][0]["detected_at_epoch"] = float("nan")
    elif malformation == "stale_detection_epoch":
        monitor["findings"][0]["detected_at_epoch"] -= 1_000
    elif malformation == "terminal_finding":
        monitor["findings"][0]["terminal_status"] = "IDLE"

    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    first = status(1, "RUNNING")
    first["timestamp"] = started.isoformat()
    killed = status(2, "IDLE", "killed")
    killed["timestamp"] = datetime.fromtimestamp(started.timestamp() + 10, UTC).isoformat()
    run = CollectedRun(
        conversation_id=_CID,
        events=[first, killed],
        state_initial={},
        state_final={"execution_status": "IDLE"},
        workspace_manifest={},
        preview=None,
        thrash_monitor=monitor,
    )
    assert _run_mod._confirmed_live_thrash_stop(run) is False


def _impl_test_confirmed_killed_idle_boundary_ignores_prior_build_terminal():
    started = datetime(2026, 7, 15, 21, 0, tzinfo=UTC)
    killed = status(1, "IDLE", "killed")
    killed["timestamp"] = datetime.fromtimestamp(started.timestamp() + 10, UTC).isoformat()
    finished = status(2, "FINISHED")
    finished["timestamp"] = datetime.fromtimestamp(started.timestamp() + 8, UTC).isoformat()

    assert _run_mod._terminal_status_epoch([killed]) is None
    assert _run_mod._terminal_status_epoch([killed], allow_killed_idle=True) == pytest.approx(
        started.timestamp() + 10, rel=0, abs=1e-6
    )
    assert _run_mod._terminal_status_epoch(
        [killed, finished], allow_killed_idle=True
    ) == pytest.approx(started.timestamp() + 10, rel=0, abs=1e-6)


@pytest.mark.asyncio
async def _impl_test_progress_poll_kills_conversation_on_confirmed_live_thrash(
    monkeypatch, tmp_path
):
    client = _client(FakeTransport(tmp_path / "disco.db", states=["RUNNING"]), tmp_path)
    client.enable_live_thrash_monitor(_smoke_scenario())

    async def crossed(_conversation_id: str, *, terminal_status: str = "") -> bool:
        return True

    killed: list[str] = []

    async def kill(conversation_id: str) -> dict[str, object]:
        killed.append(conversation_id)
        return {"http_status": 200}

    monkeypatch.setattr(client, "_sample_live_thrash", crossed)
    monkeypatch.setattr(client, "kill", kill)

    result = await client.poll_until_terminal_or_gate(_CID, inactivity_s=5, hard_cap_s=5)

    assert result == LIVE_THRASH_STOP
    assert killed == [_CID]
