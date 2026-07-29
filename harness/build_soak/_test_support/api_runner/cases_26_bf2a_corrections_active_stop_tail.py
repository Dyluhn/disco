"""Moved bf2a corrections active stop tail collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    INACTIVE_TIMEOUT,
    Any,
    CollectedRun,
    DiscoApiClient,
    _disco_mod,
    _run_mod,
    cast,
    clean_smoke_log,
    drive_scenario,
    json,
    pytest,
    run_once,
)
from .helpers_01 import (
    FakeTransport,
    _ActiveReleaseTransport,
    _client,
    _inspect_client,
    _inspect_snapshot,
    _inspect_span,
    _InspectSnapshotTransport,
    _seed_db,
    _smoke_scenario,
)
from .helpers_02 import (
    _nonterminal_inactive_log,
    _NonterminalCollectionTransport,
)
from .helpers_03 import _forged_lossless_trace, _honest_gate_trace


@pytest.mark.asyncio
async def _impl_test_bf2a_release_of_active_conversation_retains_kill_tail_event():
    from harness.build_soak.run import _release_conversation

    transport = _ActiveReleaseTransport()
    client = DiscoApiClient(cast(Any, transport), db_path=":memory:", poll_interval_s=100.0)
    await client.start_inspect_collection(_CID)

    await _release_conversation(client, _CID)
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert [event["seq"] for event in trace["events"]] == [1, 2, 3]
    assert trace["aggregation"]["lossless"] is True
    assert trace["aggregation"]["finalized"] is True
    # The stop lands BEFORE the final sample, so the tail is observable.
    kill_index = transport.calls.index("kill")
    assert "trace" in transport.calls[kill_index:]


@pytest.mark.asyncio
async def _impl_test_bf2a_release_of_terminal_conversation_freezes_inspect_before_kill():
    from harness.build_soak.run import _release_conversation

    transport = _ActiveReleaseTransport(state="FINISHED")
    client = DiscoApiClient(cast(Any, transport), db_path=":memory:", poll_interval_s=100.0)
    await client.start_inspect_collection(_CID)

    await _release_conversation(client, _CID)
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    # A terminal source emits nothing further; the aggregate freezes before the
    # cleanup kill so release can never race the final sample.
    assert [event["seq"] for event in trace["events"]] == [1, 2]
    assert trace["aggregation"]["lossless"] is True
    kill_index = transport.calls.index("kill")
    assert "trace" not in transport.calls[kill_index:]


@pytest.mark.asyncio
async def _impl_test_bf2a_release_with_unconfirmed_kill_taints_continuity():
    from harness.build_soak.run import _release_conversation

    transport = _ActiveReleaseTransport(kill_ack=False)
    client = DiscoApiClient(cast(Any, transport), db_path=":memory:", poll_interval_s=100.0)
    await client.start_inspect_collection(_CID)

    await _release_conversation(client, _CID)
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    # The observed prefix (and observable tail) is preserved, but quiescence was
    # never proven, so the aggregate must not claim losslessness.
    assert [event["seq"] for event in trace["events"]] == [1, 2, 3]
    assert trace["aggregation"]["lossless"] is False
    assert "active_stop_unconfirmed" in trace["aggregation"]["failure_reasons"]


@pytest.mark.asyncio
async def _impl_test_bf2a_progress_hard_cap_stop_retains_kill_tail_event(tmp_path, monkeypatch):
    class _HardCapTailTransport(FakeTransport):
        def __init__(self, db_path, **kwargs):
            super().__init__(db_path, **kwargs)
            self.trace_snapshot = _inspect_snapshot([_inspect_span(1), _inspect_span(2)])
            self.trace_reads_after_kill = 0

        async def get_json(self, path):
            if path == f"/api/debug/trace/{self.cid}":
                if self._killed:
                    self.trace_reads_after_kill += 1
                return 200, json.loads(json.dumps(self.trace_snapshot))
            return await super().get_json(path)

        async def post_json(self, path, body):
            status_code, data = await super().post_json(path, body)
            if path.endswith("/kill"):
                self.trace_snapshot = _inspect_snapshot(
                    [_inspect_span(1), _inspect_span(2), _inspect_span(3)]
                )
            return status_code, data

    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    transport = _HardCapTailTransport(db, states=["RUNNING"])
    client = _client(transport, tmp_path)

    async def hard_capped(*_args, **_kwargs):
        raise _disco_mod.InconclusiveRunError("progress-aware hard cap", {"elapsed_s": 1})

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", hard_capped)
    with pytest.raises(_disco_mod.InconclusiveRunError) as excinfo:
        await drive_scenario(client, _smoke_scenario(), model="m", autonomous=False)

    run = excinfo.value.collected_run
    assert run is not None
    trace = run.inspect_trace
    assert trace is not None
    assert [event["seq"] for event in trace["events"]] == [1, 2, 3]
    assert trace["aggregation"]["lossless"] is True
    assert transport.trace_reads_after_kill >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        # A byte-identical retained window cannot raise the eviction counter.
        ([1, 2, 3], ([1, 2, 3], 5)),
        # Eviction without any new suffix is impossible: the ring evicts only
        # while appending.
        ([1, 2, 3], ([2, 3], 1)),
        # A delta covering every prior member cannot leave an overlap behind.
        ([1, 2], ([2, 6, 7], 3)),
    ],
)
async def _impl_test_bf2a_impossible_dropped_count_transitions_fail_closed(first, second):
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(seq) for seq in first]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    seqs, dropped = second
    transport.snapshot = _inspect_snapshot([_inspect_span(seq) for seq in seqs], dropped=dropped)
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    reasons = trace["aggregation"]["failure_reasons"]
    assert "impossible_dropped_transition" in reasons
    assert "source_reset" in reasons
    assert trace["dropped_event_count"] is None


@pytest.mark.asyncio
async def _impl_test_bf2a_true_eviction_with_new_suffix_from_below_capacity_stays_green():
    # The earlier sample was below ring capacity; the rule must enforce only the
    # necessary eviction + new-suffix facts, never an exact capacity assumption.
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(1), _inspect_span(2)]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot(
        [_inspect_span(2), _inspect_span(3), _inspect_span(4)], dropped=1
    )
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is True
    assert [event["seq"] for event in trace["events"]] == [1, 2, 3, 4]
    assert trace["source_dropped_event_count"] == 1
    assert trace["aggregation"]["continuity_reason"] == "overlap_proven"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        # Boolean event_count: True == 1 satisfies the length check, so only the
        # exact non-bool type test rejects it.
        {**_inspect_snapshot([_inspect_span(2)]), "event_count": True},
        {**_inspect_snapshot([]), "event_count": -1},
        {**_inspect_snapshot([_inspect_span(2)]), "dropped_event_count": True},
        # NaN violates strict canonical JSON (allow_nan=False).
        _inspect_snapshot([{**_inspect_span(2), "value": float("nan")}]),
    ],
)
async def _impl_test_bf2a_boolean_negative_and_nan_snapshot_scalars_fail_closed(snapshot):
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(1)]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = snapshot
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert "malformed_snapshot" in trace["aggregation"]["failure_reasons"]
    assert trace["aggregation"]["malformed_sample_count"] == 1


def _impl_test_bf2a_validator_accepts_honest_lossless_and_honest_tainted_aggregates():
    assert _disco_mod.inspect_aggregate_violations(_honest_gate_trace(), conversation_id=_CID) == []
    tainted = _disco_mod._InspectTraceAggregation(_CID)
    tainted.note_unavailable()
    tainted.add_snapshot(_inspect_snapshot([_inspect_span(1)]))
    tainted.finish()
    assert _disco_mod.inspect_aggregate_violations(tainted.render(), conversation_id=_CID) == []


@pytest.mark.parametrize(
    ("tamper", "label"),
    [
        (lambda t: t.__setitem__("event_count", 99), "event_count"),
        (lambda t: t["aggregation"].__setitem__("unique_event_count", 99), "event_count"),
        (
            lambda t: t["aggregation"].__setitem__("first_retained_seq", 7),
            "retained_seq:first_retained_seq",
        ),
        (lambda t: t["events"].reverse(), "event_sequence"),
        (lambda t: t["events"][0].__setitem__("seq", True), "event_sequence"),
        (lambda t: t["events"][0].__setitem__("value", float("nan")), "event_not_canonical"),
        (lambda t: t.__setitem__("routing_decisions", []), "projection:routing_decisions"),
        (
            lambda t: t.__setitem__("spans", [{"span": "agent.step", "event": "end", "forged": 1}]),
            "projection:spans",
        ),
        (
            lambda t: t["aggregation"].__setitem__("failure_reasons", ["not_a_reason"]),
            "failure_reasons",
        ),
        (
            lambda t: t["aggregation"].__setitem__(
                "failure_reasons", ["source_reset", "event_content_conflict"]
            ),
            "failure_reasons",
        ),
        (lambda t: t["aggregation"].__setitem__("lossless", False), "lossless_incoherent"),
        (lambda t: t.__setitem__("dropped_event_count", None), "dropped_event_count"),
        (lambda t: t["aggregation"].__setitem__("continuity", "incomplete"), "continuity"),
        (
            lambda t: t["aggregation"].__setitem__("continuity_reason", "overlap_proven"),
            "continuity_reason",
        ),
        (
            lambda t: t.__setitem__("source_dropped_event_count", 3),
            "source_dropped_event_count",
        ),
        (lambda t: t.__setitem__("extra", 1), "top_level_keys"),
        (lambda t: t.__setitem__("conversation_id", "conv_other"), "conversation_id"),
        (lambda t: t["aggregation"].pop("conflict_count"), "aggregation_keys"),
        (lambda t: t["aggregation"].__setitem__("schema_version", True), "schema_version"),
        (lambda t: t["aggregation"].__setitem__("sample_count", True), "counter:sample_count"),
        (
            lambda t: t["aggregation"].__setitem__("event_bearing_sample_count", 5),
            "sample_accounting",
        ),
        # Counter <-> reason coherence: a disclosed rejected/unavailable/
        # malformed sample or source eviction cannot coexist with a green wear.
        (
            lambda t: t["aggregation"].__setitem__("conflict_count", 1),
            "conflict_reason_incoherent",
        ),
        (
            lambda t: t["aggregation"].__setitem__("unavailable_sample_count", 1),
            "unavailable_reason_incoherent",
        ),
        (
            lambda t: t["aggregation"].__setitem__("malformed_sample_count", 1),
            "malformed_reason_incoherent",
        ),
        (
            lambda t: (
                t.__setitem__("source_dropped_event_count", 2),
                t["aggregation"].__setitem__("source_max_dropped_count", 2),
            ),
            "overlap_continuity_incoherent",
        ),
    ],
)
def _impl_test_bf2a_validator_flags_each_forged_inconsistency(tamper, label):
    trace = _honest_gate_trace()
    tamper(trace)
    violations = _disco_mod.inspect_aggregate_violations(trace, conversation_id=_CID)
    assert label in violations


@pytest.mark.asyncio
async def _impl_test_bf2a_required_gate_rejects_forged_lossless_aggregate(tmp_path, monkeypatch):
    run = CollectedRun(
        conversation_id=_CID,
        events=clean_smoke_log(),
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={},
        preview=None,
        inspect_trace=_forged_lossless_trace(),
    )

    async def forged_drive(*_args, **_kwargs):
        return run

    monkeypatch.setattr(_run_mod, "drive_scenario", forged_drive)
    client = _client(FakeTransport(tmp_path / "missing.db", states=["FINISHED"]), tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_bf2a_forged_lossless",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=1,
        require_inspect_trace=True,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert "internally consistent inspect aggregate" in record["facts"]["missing_trace_parts"]
    violations = record["facts"]["inspect_aggregate_violations"]
    assert "projection:routing_decisions" in violations
    assert "projection:spans" in violations


@pytest.mark.asyncio
async def _impl_test_bf2a_required_gate_accepts_honest_lossless_aggregate(tmp_path, monkeypatch):
    run = CollectedRun(
        conversation_id=_CID,
        events=clean_smoke_log(),
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={},
        preview=None,
        inspect_trace=_honest_gate_trace(),
    )

    async def honest_drive(*_args, **_kwargs):
        return run

    monkeypatch.setattr(_run_mod, "drive_scenario", honest_drive)
    client = _client(FakeTransport(tmp_path / "missing.db", states=["FINISHED"]), tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_bf2a_honest_lossless",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=1,
        require_inspect_trace=True,
    )

    # The gate must not reject an honest lossless aggregate; whatever later
    # gates decide about this synthetic run, the inspect evidence is complete.
    assert record["code"] != "MISSING_REQUIRED_EVIDENCE"
    assert "missing_trace_parts" not in (record.get("facts") or {})


def _assert_inspect_summary_continuity(inspect_summary) -> None:
    assert inspect_summary["captured"] is True
    assert inspect_summary["lossless"] is True
    assert inspect_summary["finalized"] is True
    assert inspect_summary["continuity"] == "complete"
    assert inspect_summary["continuity_reason"] == "overlap_proven"
    assert inspect_summary["aggregate_dropped_event_count"] == 0
    assert inspect_summary["source_dropped_event_count"] == 1
    assert inspect_summary["sample_count"] == 2


def _assert_inspect_summary_bounds(inspect_summary) -> None:
    assert inspect_summary["accepted_sample_count"] == 2
    assert inspect_summary["overlap_sample_count"] == 1
    assert inspect_summary["unique_event_count"] == 3
    assert inspect_summary["first_retained_seq"] == 1
    assert inspect_summary["last_retained_seq"] == 3
    assert inspect_summary["conflict_count"] == 0
    assert inspect_summary["failure_reasons"] == []
    # Bounded scalars only — never the unbounded event list.
    assert "events" not in inspect_summary


def _impl_test_bf2a_batch_item_summary_discloses_bounded_aggregation_scalars(tmp_path):
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_inspect_span(1), _inspect_span(2)]))
    aggregation.add_snapshot(_inspect_snapshot([_inspect_span(2), _inspect_span(3)], dropped=1))
    aggregation.finish()
    trace = aggregation.render()
    conv_dir = tmp_path / "run_batch" / "conversations" / _CID
    conv_dir.mkdir(parents=True)
    (conv_dir / "inspect-trace.json").write_text(json.dumps(trace), encoding="utf-8")

    summary = _run_mod._run_batch_item(
        index=0,
        run_id="run_batch",
        base=tmp_path,
        classification={"conversation_id": _CID, "status": "PASS", "scenario_id": "s"},
    )

    inspect_summary = summary["inspect"]
    _assert_inspect_summary_continuity(inspect_summary)
    _assert_inspect_summary_bounds(inspect_summary)


@pytest.mark.asyncio
async def _impl_test_bf2a_required_gate_rejects_laundered_conflict_aggregate(tmp_path, monkeypatch):
    # Drive the real aggregation into a conflict taint, then wipe the taint and
    # wear green: the disclosed conflict_count survives, so the validator's
    # counter<->reason coherence must fail the gate.
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(
        _inspect_snapshot(
            [
                {"seq": 1, "kind": "routing", "role": "agent_driver", "chosen_model": "m"},
                {"seq": 2, "kind": "span", "span": "agent.step", "event": "end"},
            ]
        )
    )
    aggregation.add_snapshot(
        _inspect_snapshot(
            [
                {"seq": 1, "kind": "routing", "role": "agent_driver", "chosen_model": "m"},
                {"seq": 2, "kind": "span", "span": "agent.step", "event": "start"},
            ]
        )
    )
    aggregation.finish()
    trace = aggregation.render()
    assert trace["aggregation"]["conflict_count"] == 1
    trace["aggregation"]["failure_reasons"] = []
    trace["aggregation"]["lossless"] = True
    trace["aggregation"]["continuity"] = "complete"
    trace["aggregation"]["continuity_reason"] = "no_source_eviction_observed"
    trace["dropped_event_count"] = 0

    run = CollectedRun(
        conversation_id=_CID,
        events=clean_smoke_log(),
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={},
        preview=None,
        inspect_trace=trace,
    )

    async def laundered_drive(*_args, **_kwargs):
        return run

    monkeypatch.setattr(_run_mod, "drive_scenario", laundered_drive)
    client = _client(FakeTransport(tmp_path / "missing.db", states=["FINISHED"]), tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_bf2a_laundered_conflict",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=1,
        require_inspect_trace=True,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert "internally consistent inspect aggregate" in record["facts"]["missing_trace_parts"]
    assert "conflict_reason_incoherent" in record["facts"]["inspect_aggregate_violations"]


@pytest.mark.asyncio
async def _impl_test_bf2a_inactive_timeout_collection_stops_before_inspect_freeze(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, _nonterminal_inactive_log())
    transport = _NonterminalCollectionTransport(db, states=["RUNNING"])
    client = _client(transport, tmp_path)

    async def inactive_drive(*_args, **_kwargs):
        return INACTIVE_TIMEOUT

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", inactive_drive)
    run = await drive_scenario(client, _smoke_scenario(), model="m", autonomous=False)

    trace = run.inspect_trace
    assert trace is not None
    # The nonterminal conversation was stopped BEFORE the final sample, so the
    # tail event emitted during the kill is retained and losslessness is honest.
    assert [event["seq"] for event in trace["events"]] == [1, 2, 3]
    assert trace["aggregation"]["lossless"] is True
    # The dossier keeps the true pre-stop product state.
    assert run.state_final == {"execution_status": "RUNNING"}


@pytest.mark.asyncio
async def _impl_test_bf2a_inactive_timeout_unconfirmed_stop_taints_continuity(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, _nonterminal_inactive_log())
    transport = _NonterminalCollectionTransport(db, states=["RUNNING"], kill_ack=False)
    client = _client(transport, tmp_path)

    async def inactive_drive(*_args, **_kwargs):
        return INACTIVE_TIMEOUT

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", inactive_drive)
    run = await drive_scenario(client, _smoke_scenario(), model="m", autonomous=False)

    trace = run.inspect_trace
    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert "active_stop_unconfirmed" in trace["aggregation"]["failure_reasons"]
