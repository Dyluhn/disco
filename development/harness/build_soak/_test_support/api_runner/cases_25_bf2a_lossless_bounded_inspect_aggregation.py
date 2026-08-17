"""Moved bf2a lossless bounded inspect aggregation collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    Any,
    BrowserEvidenceCollectionError,
    CollectedRun,
    DiscoApiClient,
    _run_mod,
    asyncio,
    cast,
    clean_smoke_log,
    drive_scenario,
    pytest,
    run_once,
)
from .helpers_01 import (
    FakeTransport,
    _client,
    _fake_inspect_trace,
    _inspect_client,
    _inspect_snapshot,
    _inspect_span,
    _InspectSnapshotTransport,
    _seed_db,
    _smoke_scenario,
)


def _assert_lossless_bounded_trace(trace) -> None:
    assert trace is not None
    assert trace["event_count"] == 1200
    assert trace["dropped_event_count"] == 0
    assert trace["source_dropped_event_count"] == 176
    assert len(trace["spans"]) == 1197
    assert {span.get("kind") for span in trace["spans"]} == {None, "soft"}
    assert trace["routing_decisions"] == [{"role": "agent", "request_id": "request-1"}]
    assert trace["tool_scopes"] == [{"mode": "execution", "request_id": "request-2"}]
    assert trace["progress_shadows"] == [{"latest_event_seq": 3, "request_id": "request-3"}]
    assert trace["aggregation"]["lossless"] is True
    assert trace["aggregation"]["continuity_reason"] == "overlap_proven"
    assert trace["aggregation"]["overlap_sample_count"] >= 2
    assert trace["aggregation"]["unique_event_count"] == 1200


@pytest.mark.asyncio
async def _impl_test_bf2a_overlapping_windows_retain_more_than_ring_and_derive_projections():
    events = [_inspect_span(seq) for seq in range(1, 1201)]
    events[:3] = [
        {"seq": 1, "kind": "routing", "role": "agent", "request_id": "request-1"},
        {"seq": 2, "kind": "tool_scope", "mode": "execution", "request_id": "request-2"},
        {
            "seq": 3,
            "kind": "progress_shadow",
            "latest_event_seq": 3,
            "request_id": "request-3",
        },
    ]
    # Actual governed trace shape: log_event(..., kind="soft") overwrites
    # the flattened outer kind="span" field. Exact span/event semantics retain
    # it as a span projection while preserving the payload kind.
    events[1099] = {
        "seq": 1100,
        "kind": "soft",
        "span": "request_budget.preview",
        "event": "point",
        "request_id": "request-1100",
    }
    transport = _InspectSnapshotTransport(_inspect_snapshot(events[:1024]))
    client = _inspect_client(transport)

    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot(events[76:1100], dropped=76)
    await client.collect_inspect_trace(_CID)
    transport.snapshot = _inspect_snapshot(events[176:1200], dropped=176)
    trace = await client.finish_inspect_collection(_CID)

    _assert_lossless_bounded_trace(trace)


@pytest.mark.asyncio
async def _impl_test_bf2a_expected_same_conversation_pretrace_404_is_not_a_failure():
    class _PretraceTransport(_InspectSnapshotTransport):
        async def get_json(self, _path):
            if self.snapshot is None:
                return 404, {"error": "no trace", "conversation_id": _CID}
            return 200, self.snapshot

    transport = _PretraceTransport(None)
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot([_inspect_span(1)])
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["pretrace_unavailable_count"] == 1
    assert trace["aggregation"]["failure_reasons"] == []
    assert trace["aggregation"]["lossless"] is True


@pytest.mark.asyncio
async def _impl_test_bf2a_same_conversation_no_trace_after_snapshot_is_source_loss():
    class _DisappearingTraceTransport(_InspectSnapshotTransport):
        async def get_json(self, _path):
            if self.snapshot is None:
                return 404, {"error": "no trace", "conversation_id": _CID}
            return 200, self.snapshot

    transport = _DisappearingTraceTransport(_inspect_snapshot([_inspect_span(1)]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = None
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert trace["aggregation"]["pretrace_unavailable_count"] == 0
    assert trace["aggregation"]["unavailable_sample_count"] == 1
    assert "source_reset" in trace["aggregation"]["failure_reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        (404, {"error": "inspect disabled"}),
        (404, {"error": "no trace", "conversation_id": "conv_other"}),
        (503, {"error": "unavailable"}),
    ],
)
async def _impl_test_bf2a_noncanonical_pretrace_http_failure_taints_collection(response):
    class _HttpFailureTransport(_InspectSnapshotTransport):
        async def get_json(self, _path):
            if self.snapshot is None:
                return response
            return 200, self.snapshot

    transport = _HttpFailureTransport(None)
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot([_inspect_span(1)])
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert "inspect_unavailable" in trace["aggregation"]["failure_reasons"]


@pytest.mark.asyncio
async def _impl_test_bf2a_pretrace_network_failure_is_not_forgiven_by_later_snapshot():
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(1)]))
    transport.raise_on_trace = True
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.raise_on_trace = False
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert trace["aggregation"]["pretrace_unavailable_count"] == 0
    assert trace["aggregation"]["unavailable_sample_count"] == 1
    assert "inspect_unavailable" in trace["aggregation"]["failure_reasons"]


@pytest.mark.asyncio
async def _impl_test_bf2a_empty_window_then_first_event_window_already_dropped_is_not_lossless():
    transport = _InspectSnapshotTransport(_inspect_snapshot([]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot([_inspect_span(50)], dropped=49)
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert "first_snapshot_already_dropped" in trace["aggregation"]["failure_reasons"]
    assert trace["dropped_event_count"] is None


@pytest.mark.asyncio
async def _impl_test_bf2a_eviction_without_overlap_is_an_explicit_gap():
    transport = _InspectSnapshotTransport(
        _inspect_snapshot([_inspect_span(seq) for seq in range(1, 4)])
    )
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot([_inspect_span(seq) for seq in range(10, 13)], dropped=9)
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert "eviction_gap_no_overlap" in trace["aggregation"]["failure_reasons"]
    assert trace["aggregation"]["unique_event_count"] == 3


@pytest.mark.asyncio
async def _impl_test_bf2a_duplicate_sequence_changed_content_is_rejected():
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(1, label="original")]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot([_inspect_span(1, label="changed"), _inspect_span(2)])
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert trace["aggregation"]["conflict_count"] == 1
    assert trace["events"] == [_inspect_span(1, label="original")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "later", "reason"),
    [
        (
            _inspect_snapshot([_inspect_span(1)]),
            _inspect_snapshot([_inspect_span(True)]),
            "malformed_snapshot",
        ),
        (
            _inspect_snapshot([_inspect_span(1)]),
            _inspect_snapshot([_inspect_span("2")]),
            "malformed_snapshot",
        ),
        (
            _inspect_snapshot([_inspect_span(1)]),
            _inspect_snapshot([_inspect_span(2), _inspect_span(1)]),
            "snapshot_sequence_regression",
        ),
        (
            _inspect_snapshot([_inspect_span(10), _inspect_span(11)]),
            _inspect_snapshot([_inspect_span(1), _inspect_span(2)]),
            "source_reset",
        ),
        (
            _inspect_snapshot([_inspect_span(1)]),
            _inspect_snapshot([_inspect_span(1)], conversation_id="conv_other"),
            "conversation_mismatch",
        ),
        (
            _inspect_snapshot([_inspect_span(1)]),
            _inspect_snapshot([{"seq": 1, "kind": "soft", "value": 1}]),
            "ambiguous_event_discriminator",
        ),
        (
            _inspect_snapshot([_inspect_span(1)]),
            _inspect_snapshot([{"seq": 1, "kind": True}]),
            "ambiguous_event_discriminator",
        ),
        (
            _inspect_snapshot([_inspect_span(1)]),
            _inspect_snapshot([{"seq": 1, "kind": "routing", "span": "x", "event": "point"}]),
            "ambiguous_event_discriminator",
        ),
    ],
)
async def _impl_test_bf2a_invalid_snapshot_seams_fail_closed(first, later, reason):
    transport = _InspectSnapshotTransport(first)
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = later
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert reason in trace["aggregation"]["failure_reasons"]


@pytest.mark.asyncio
async def _impl_test_bf2a_dropped_count_regression_is_not_reset_green():
    # The intermediate window is a PHYSICALLY POSSIBLE eviction (member 1 gone,
    # new suffix 3 arrived) so the regression seam — not the impossible-
    # transition seam — is what this test exercises.
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(1), _inspect_span(2)]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.snapshot = _inspect_snapshot([_inspect_span(2), _inspect_span(3)], dropped=1)
    await client.collect_inspect_trace(_CID)
    transport.snapshot = _inspect_snapshot([_inspect_span(2), _inspect_span(3)], dropped=0)
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    reasons = trace["aggregation"]["failure_reasons"]
    assert "dropped_count_regression" in reasons
    assert "source_reset" in reasons
    assert trace["aggregation"]["source_max_dropped_count"] == 1


@pytest.mark.asyncio
async def _impl_test_bf2a_transient_collector_failure_persists_after_later_overlap():
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(1)]))
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)
    transport.raise_on_trace = True
    await client.collect_inspect_trace(_CID)
    transport.raise_on_trace = False
    transport.snapshot = _inspect_snapshot([_inspect_span(1), _inspect_span(2)])
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is False
    assert trace["aggregation"]["unavailable_sample_count"] == 1
    assert "inspect_unavailable" in trace["aggregation"]["failure_reasons"]
    assert trace["aggregation"]["unique_event_count"] == 2


@pytest.mark.asyncio
async def _impl_test_bf2a_start_finish_are_idempotent_and_poller_collects_dedicatedly():
    transport = _InspectSnapshotTransport(_inspect_snapshot([_inspect_span(1)]))
    client = DiscoApiClient(cast(Any, transport), db_path=":memory:", poll_interval_s=0.01)
    await client.start_inspect_collection(_CID)
    first_task = client._inspect_poll_tasks[_CID]
    await client.start_inspect_collection(_CID)
    assert client._inspect_poll_tasks[_CID] is first_task
    transport.snapshot = _inspect_snapshot([_inspect_span(1), _inspect_span(2)])
    await asyncio.sleep(0.04)
    first = await client.finish_inspect_collection(_CID)
    second = await client.finish_inspect_collection(_CID)

    assert first == second
    assert first is not None
    assert first["aggregation"]["lossless"] is True
    assert first["aggregation"]["unique_event_count"] == 2


@pytest.mark.asyncio
async def _impl_test_bf2a_cancelled_finish_caller_cannot_replace_collected_prefix():
    class _BlockedFinalSampleTransport(_InspectSnapshotTransport):
        def __init__(self):
            super().__init__(_inspect_snapshot([_inspect_span(1)]))
            self.calls = 0
            self.final_sample_started = asyncio.Event()
            self.release_final_sample = asyncio.Event()

        async def get_json(self, _path):
            self.calls += 1
            if self.calls == 1:
                return 200, self.snapshot
            self.final_sample_started.set()
            await self.release_final_sample.wait()
            return 200, _inspect_snapshot([_inspect_span(1), _inspect_span(2)])

    transport = _BlockedFinalSampleTransport()
    client = _inspect_client(transport)
    await client.start_inspect_collection(_CID)

    cancelled_caller = asyncio.create_task(client.finish_inspect_collection(_CID))
    await transport.final_sample_started.wait()
    cancelled_caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_caller

    transport.release_final_sample.set()
    trace = await client.finish_inspect_collection(_CID)

    assert trace is not None
    assert trace["aggregation"]["lossless"] is True
    assert trace["aggregation"]["finalized"] is True
    assert trace["aggregation"]["unique_event_count"] == 2
    assert [event["seq"] for event in trace["events"]] == [1, 2]


@pytest.mark.asyncio
async def _impl_test_bf2a_collection_starts_before_message_kick():
    class _CreationTransport(_InspectSnapshotTransport):
        def __init__(self):
            super().__init__(None)
            self.order: list[str] = []

        async def post_json(self, path, _body):
            self.order.append(path)
            if path == "/conversations":
                return 200, {"conversation_id": _CID}
            return 200, {"ok": True}

        async def get_json(self, _path):
            self.order.append("inspect")
            return 404, {"error": "no trace", "conversation_id": _CID}

    transport = _CreationTransport()
    client = _inspect_client(transport)
    cid = await client.create_build_conversation("build", model="m")
    await client.finish_inspect_collection(cid)

    assert transport.order[:3] == ["/conversations", "inspect", f"/conversations/{_CID}/messages"]


@pytest.mark.asyncio
async def _impl_test_bf2a_inspect_finalizes_before_later_browser_capture_failure(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, clean_smoke_log())
    client = _client(FakeTransport(db, states=["FINISHED"]), tmp_path)

    async def terminal(*_args, **_kwargs):
        return "FINISHED"

    async def workspace(*_args, **_kwargs):
        return {"index.html": {"content": "ok"}}

    def browser_failure(*_args, **_kwargs):
        raise BrowserEvidenceCollectionError("missing browser bytes", {})

    monkeypatch.setattr(_run_mod, "_drive_to_terminal", terminal)
    monkeypatch.setattr(client, "collect_workspace", workspace)
    monkeypatch.setattr(client, "collect_browser_evidence", browser_failure)

    with pytest.raises(BrowserEvidenceCollectionError):
        await drive_scenario(client, _smoke_scenario(), model="m", autonomous=False)

    trace = await client.finish_inspect_collection(_CID)
    assert trace is not None
    assert trace["aggregation"]["finalized"] is True
    assert trace["aggregation"]["lossless"] is True


@pytest.mark.asyncio
async def _impl_test_bf2a_required_gate_rejects_legacy_trace_without_aggregation(
    tmp_path, monkeypatch
):
    run = CollectedRun(
        conversation_id=_CID,
        events=clean_smoke_log(),
        state_initial={"execution_status": "IDLE"},
        state_final={"execution_status": "FINISHED"},
        workspace_manifest={},
        preview=None,
        inspect_trace=_fake_inspect_trace(),
    )

    async def legacy_drive(*_args, **_kwargs):
        return run

    monkeypatch.setattr(_run_mod, "drive_scenario", legacy_drive)
    client = _client(FakeTransport(tmp_path / "missing.db", states=["FINISHED"]), tmp_path)
    record = await run_once(
        client,
        _smoke_scenario(),
        run_id="run_bf2a_missing_aggregation",
        out_root=tmp_path / "out",
        model="m",
        autonomous=False,
        commit="abc",
        timeout_s=1,
        require_inspect_trace=True,
    )

    assert record["status"] == "INVALID_RUN"
    assert record["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert "lossless inspect aggregation" in record["facts"]["missing_trace_parts"]
