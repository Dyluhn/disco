"""Moved infra gate fires only pre collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    _is_terminal_sandbox_preflight_trace,
    _run_mod,
    json,
    load_manifest,
    msg,
    pytest,
    run_once,
    status,
    verify_evidence_unchanged,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _sandbox_preflight_helper_fixture,
    _seed_db,
    _smoke_scenario,
)


@pytest.mark.asyncio
async def _impl_test_infra_gate_fires_pre_create_on_unreachable_server(tmp_path):
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
async def _impl_test_infra_gate_maps_5xx_health(tmp_path):
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
async def _impl_test_post_create_error_status_is_product_not_infra(tmp_path):
    # codex #3: a StatusEvent(ERROR) AFTER create (driver preflight) is a PRODUCT
    # outcome, never infra. Health is fine pre-create; the run errors post-create.
    err_log = [
        {
            "id": "e1",
            "seq": 1,
            "kind": "message",
            "source": "user",
            "message": {"role": "user", "content": "build"},
        },
        {
            "id": "e2",
            "seq": 2,
            "kind": "status",
            "source": "system",
            "status": "ERROR",
            "detail": "Driver local-qwen unreachable",
        },
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
async def _impl_test_terminal_driver_preflight_failure_is_not_masked_by_missing_agent_span(
    tmp_path,
):
    err_log = [
        {
            "id": "e1",
            "seq": 1,
            "kind": "message",
            "source": "user",
            "message": {"role": "user", "content": "build"},
        },
        {
            "id": "e2",
            "seq": 2,
            "kind": "status",
            "source": "system",
            "status": "ERROR",
            "detail": "Driver configured-model unreachable",
        },
    ]
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, err_log)

    class _PreflightFailureTransport(FakeTransport):
        async def get_json(self, path):
            if path == f"/api/debug/trace/{self.cid}":
                decision = {
                    "role": "agent_driver",
                    "chosen_model": "configured-model",
                    "provider": "configured-provider",
                    "path": "manual",
                    "reason": "terminal failure: LLMTransientError (config)",
                    "attempt": 1,
                    "overflow_triggers": [],
                }
                return 200, {
                    "conversation_id": self.cid,
                    "event_count": 1,
                    "dropped_event_count": 0,
                    "routing_decisions": [decision],
                    "spans": [],
                    "tool_scopes": [],
                    "events": [{"seq": 1, "kind": "routing", **decision}],
                }
            return await super().get_json(path)

    transport = _PreflightFailureTransport(db, states=["ERROR", "ERROR"])
    scenario = _smoke_scenario()
    record = await run_once(
        _client(transport, tmp_path),
        scenario,
        run_id="run_driver_preflight_error_001",
        out_root=tmp_path / "out",
        model="configured-model",
        autonomous=False,
        commit="abc",
        timeout_s=5,
        require_inspect_trace=True,
    )

    assert record["status"] == "FAIL", record
    assert record["code"] != "MISSING_REQUIRED_EVIDENCE"
    assert record["conversation_id"] == _CID


@pytest.mark.asyncio
async def _impl_test_terminal_sandbox_preflight_failure_is_not_masked_by_missing_agent_span(
    tmp_path,
):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            status(
                2,
                "ERROR",
                detail=(
                    "podman sandbox host unix:///run/user/1000/podman/podman.sock "
                    "unreachable: no response within 12s (sandbox pre-flight timed out)"
                ),
            ),
        ],
    )

    class _SandboxPreflightFailureTransport(FakeTransport):
        async def get_json(self, path):
            if path == f"/api/debug/trace/{self.cid}":
                decision = {
                    "role": "agent_driver",
                    "chosen_model": "configured-model",
                    "provider": "configured-provider",
                    "path": "manual",
                    "reason": "shared driver preflight success",
                    "attempt": 1,
                    "overflow_triggers": ["driver_preflight"],
                }
                return 200, {
                    "conversation_id": self.cid,
                    "event_count": 1,
                    "dropped_event_count": 0,
                    "routing_decisions": [decision],
                    "spans": [],
                    "tool_scopes": [],
                    "events": [{"seq": 1, "kind": "routing", **decision}],
                }
            return await super().get_json(path)

    scenario = _smoke_scenario()
    scenario["assertions"]["provider"] = {"model": "configured-model"}
    record = await run_once(
        _client(_SandboxPreflightFailureTransport(db, states=["ERROR", "ERROR"]), tmp_path),
        scenario,
        run_id="run_sandbox_preflight_error_001",
        out_root=tmp_path / "out",
        model="configured-model",
        autonomous=False,
        commit="abc",
        timeout_s=5,
        require_inspect_trace=True,
    )

    assert record["status"] == "FAIL"
    assert record["code"] != "MISSING_REQUIRED_EVIDENCE"
    assert record["conversation_id"] == _CID


@pytest.mark.parametrize(
    "detail",
    [
        "sandbox backend is misconfigured: unsupported backend",
        "gvisor sandbox host unix:///socket unreachable: refused",
        "local sandbox host unix:///socket error: probe failed",
        "podman sandbox host unix:///socket unreachable: timed out",
        "process sandbox unreachable: workspace missing",
        "process sandbox error: permission denied",
    ],
)
def _impl_test_terminal_sandbox_preflight_helper_accepts_only_named_preloop_shapes(detail):
    run, trace, scenario = _sandbox_preflight_helper_fixture(detail)
    assert _is_terminal_sandbox_preflight_trace(
        run, trace, trace["routing_decisions"], [], scenario
    )


def _apply_preflight_mutation(mutation, run, trace) -> None:
    route = trace["routing_decisions"][0]
    if mutation == "generic_detail":
        run.events[-1]["detail"] = "loop failed after starting"
    elif mutation == "embedded_detail":
        run.events[-1]["detail"] = "note: podman sandbox host local unreachable: timed out"
    elif mutation == "user_status":
        run.events[-1]["source"] = "user"
    elif mutation == "later_generic_error":
        run.events.append(status(3, "ERROR", detail="later loop error"))
    elif mutation == "final_not_error":
        run.state_final = {"execution_status": "FINISHED"}
    elif mutation == "wrong_role":
        route["role"] = "title_generator"
    elif mutation == "fallback_path":
        route["path"] = "role_fallback"
    elif mutation == "wrong_model":
        route["chosen_model"] = "other-model"
    elif mutation == "dropped_trace":
        trace["dropped_event_count"] = 1
    elif mutation == "tool_scope":
        trace["tool_scopes"] = [{"mode": "planning"}]
    elif mutation == "agent_span":
        trace["spans"] = [{"span": "agent.step"}]
    elif mutation == "malformed_route":
        trace["routing_decisions"] = ["not-a-route"]
    elif mutation == "trace_projection_mismatch":
        trace["events"] = [{"seq": 1, "kind": "tool_scope"}]


@pytest.mark.parametrize(
    "mutation",
    [
        "generic_detail",
        "embedded_detail",
        "user_status",
        "later_generic_error",
        "final_not_error",
        "wrong_role",
        "fallback_path",
        "wrong_model",
        "dropped_trace",
        "tool_scope",
        "agent_span",
        "malformed_route",
        "trace_projection_mismatch",
    ],
)
def _impl_test_terminal_sandbox_preflight_helper_rejects_tainted_or_postloop_evidence(mutation):
    run, trace, scenario = _sandbox_preflight_helper_fixture(
        "podman sandbox host unix:///socket unreachable: timed out"
    )
    _apply_preflight_mutation(mutation, run, trace)

    agent_spans = [
        span
        for span in trace["spans"]
        if isinstance(span, dict) and span.get("span") == "agent.step"
    ]
    assert not _is_terminal_sandbox_preflight_trace(
        run, trace, trace["routing_decisions"], agent_spans, scenario
    )


@pytest.mark.asyncio
async def _impl_test_sandbox_shaped_error_after_tool_scope_still_requires_agent_span(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            status(
                2,
                "ERROR",
                detail="podman sandbox host local error: loop failed after tool scope",
            ),
        ],
    )

    class _PostLoopFailureTransport(FakeTransport):
        async def get_json(self, path):
            if path == f"/api/debug/trace/{self.cid}":
                decision = {
                    "chosen_model": "configured-model",
                    "provider": "configured-provider",
                    "path": "manual",
                    "reason": "config",
                }
                scope = {
                    "mode": "planning",
                    "attempt": 1,
                    "complete": True,
                    "offered_tools": ["submit_plan"],
                    "allowed_tools": ["submit_plan"],
                }
                return 200, {
                    "conversation_id": self.cid,
                    "event_count": 2,
                    "dropped_event_count": 0,
                    "routing_decisions": [decision],
                    "spans": [],
                    "tool_scopes": [scope],
                    "events": [
                        {"seq": 1, "kind": "routing", **decision},
                        {"seq": 2, "kind": "tool_scope", **scope},
                    ],
                }
            return await super().get_json(path)

    record = await run_once(
        _client(_PostLoopFailureTransport(db, states=["ERROR", "ERROR"]), tmp_path),
        _smoke_scenario(),
        run_id="run_sandbox_shaped_post_loop_error_001",
        out_root=tmp_path / "out",
        model="configured-model",
        autonomous=False,
        commit="abc",
        timeout_s=5,
        require_inspect_trace=True,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"


@pytest.mark.asyncio
async def _impl_test_nonterminal_routing_trace_still_requires_agent_span(monkeypatch, tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            status(2, "ERROR", detail="loop failed after starting"),
        ],
    )

    class _MissingLoopSpanTransport(FakeTransport):
        async def get_json(self, path):
            if path == f"/api/debug/trace/{self.cid}":
                decision = {
                    "chosen_model": "configured-model",
                    "provider": "configured-provider",
                    "path": "manual",
                    "reason": "config",
                }
                scope = {
                    "mode": "planning",
                    "attempt": 1,
                    "complete": True,
                    "offered_tools": ["submit_plan"],
                    "allowed_tools": ["submit_plan"],
                    "offered_count": 1,
                    "allowed_count": 1,
                }
                return 200, {
                    "conversation_id": self.cid,
                    "event_count": 2,
                    "dropped_event_count": 0,
                    "routing_decisions": [decision],
                    "spans": [],
                    "tool_scopes": [scope],
                    "events": [
                        {"seq": 1, "kind": "tool_scope", **scope},
                        {"seq": 2, "kind": "routing", **decision},
                    ],
                }
            return await super().get_json(path)

    provider_ledger = [
        {
            "ts": 1.0,
            "host": "provider.test",
            "model": "configured-model",
            "after_terminal": False,
            "has_tools": True,
            "conversation_id": _CID,
        }
    ]
    monkeypatch.setattr(_run_mod, "_provider_ledger_for_run", lambda _run: provider_ledger)

    transport = _MissingLoopSpanTransport(db, states=["ERROR", "ERROR"])
    out_root = tmp_path / "out"
    run_id = "run_missing_loop_span_001"
    record = await run_once(
        _client(transport, tmp_path),
        _smoke_scenario(),
        run_id=run_id,
        out_root=out_root,
        model="configured-model",
        autonomous=False,
        commit="abc",
        timeout_s=5,
        require_inspect_trace=True,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert record["conversation_id"] == _CID

    # H262: a missing required trace part invalidates the verdict without discarding
    # the already-collected evidence that explains it.  The incomplete trace and the
    # other safe run/provider evidence are frozen under the normal evidence lock.
    base = out_root / run_id
    conv = base / "conversations" / _CID
    trace = json.loads((conv / "inspect-trace.json").read_text(encoding="utf-8"))
    assert trace["routing_decisions"]
    assert trace["spans"] == []
    assert (conv / "events.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads((conv / "state.final.json").read_text(encoding="utf-8"))
    written_ledger = [
        json.loads(line)
        for line in (conv / "provider-call-ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert written_ledger == provider_ledger
    assert "required inspect evidence incomplete" in (base / "timeline.md").read_text(
        encoding="utf-8"
    )

    manifest = load_manifest(base)
    assert {
        "events.jsonl",
        "inspect-trace.json",
        "provider-call-ledger.jsonl",
        "state.final.json",
        "workspace-manifest.json",
    } <= set(manifest.evidence_hashes)
    assert verify_evidence_unchanged(base, manifest).intact


@pytest.mark.asyncio
async def _impl_test_absent_required_inspect_trace_retains_collected_dossier(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            status(2, "ERROR", detail="loop failed after starting"),
        ],
    )

    class _AbsentTraceTransport(FakeTransport):
        async def get_json(self, path):
            if path == f"/api/debug/trace/{self.cid}":
                return 404, {}
            return await super().get_json(path)

    out_root = tmp_path / "out"
    run_id = "run_absent_inspect_trace_001"
    record = await run_once(
        _client(_AbsentTraceTransport(db, states=["ERROR", "ERROR"]), tmp_path),
        _smoke_scenario(),
        run_id=run_id,
        out_root=out_root,
        model="configured-model",
        autonomous=False,
        commit="abc",
        timeout_s=5,
        require_inspect_trace=True,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert record["conversation_id"] == _CID
    assert record["facts"]["missing_trace_parts"] == [
        "lossless inspect aggregation",
        "routing_decisions",
        "agent.step spans",
    ]

    base = out_root / run_id
    conv = base / "conversations" / _CID
    retained_trace = json.loads((conv / "inspect-trace.json").read_text(encoding="utf-8"))
    assert retained_trace["aggregation"]["lossless"] is False
    assert retained_trace["aggregation"]["finalized"] is True
    assert "inspect_unavailable" in retained_trace["aggregation"]["failure_reasons"]
    assert (conv / "events.jsonl").read_text(encoding="utf-8").strip()
    assert (conv / "state.initial.json").is_file()
    assert (conv / "state.final.json").is_file()
    assert (conv / "workspace-manifest.json").is_file()
    manifest = load_manifest(base)
    assert {
        "events.jsonl",
        "inspect-trace.json",
        "state.final.json",
        "workspace-manifest.json",
    } <= set(manifest.evidence_hashes)
    assert verify_evidence_unchanged(base, manifest).intact


@pytest.mark.asyncio
async def _impl_test_mid_run_transport_loss_is_invalid_run(tmp_path):
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
