"""Moved h348 an in flight host collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    INACTIVE_TIMEOUT,
    PROGRESSING_TIMEOUT,
    DiscoApiClient,
    _disco_mod,
    msg,
    pytest,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _FakeClock,
    _H347StateTransport,
    _h348_attach,
    _h348_span,
    _inspect_snapshot,
    _install_clock,
    _seed_db,
)


def _impl_test_h348_active_agent_step_requires_one_latest_unmatched_exact_request(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [msg(1, "user", "build"), status(2, "RUNNING")])
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))

    _h348_attach(client, _h348_span(1, "start"))
    assert client._active_agent_step_request_id(_CID) == "req_h348"

    completed = _disco_mod._InspectTraceAggregation(_CID)
    completed.add_snapshot(_inspect_snapshot([_h348_span(1, "start"), _h348_span(2, "end")]))
    client._inspect_aggregations[_CID] = completed
    assert client._active_agent_step_request_id(_CID) is None

    stale_open = _disco_mod._InspectTraceAggregation(_CID)
    stale_open.add_snapshot(
        _inspect_snapshot(
            [
                _h348_span(1, "start", "req_old"),
                _h348_span(2, "start", "req_new"),
                _h348_span(3, "end", "req_new"),
            ]
        )
    )
    client._inspect_aggregations[_CID] = stale_open
    assert client._active_agent_step_request_id(_CID) is None

    tainted = _disco_mod._InspectTraceAggregation(_CID)
    tainted.add_snapshot(_inspect_snapshot([_h348_span(1, "start")]))
    tainted.note_unavailable()
    client._inspect_aggregations[_CID] = tainted
    assert client._active_agent_step_request_id(_CID) is None

    foreign = _disco_mod._InspectTraceAggregation("conv_foreign")
    foreign.add_snapshot(
        _inspect_snapshot(
            [_h348_span(1, "start")],
            conversation_id="conv_foreign",
        )
    )
    client._inspect_aggregations[_CID] = foreign
    assert client._active_agent_step_request_id(_CID) is None


@pytest.mark.asyncio
async def _impl_test_h348_active_agent_step_outlives_ordinary_inactivity(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [msg(1, "user", "build"), status(2, "RUNNING")])
    transport = _H347StateTransport(
        db,
        states=["RUNNING", "RUNNING", "FINISHED"],
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=1.0)
    _h348_attach(client, _h348_span(1, "start"))
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == "FINISHED"
    assert transport.state_reads == 3


@pytest.mark.asyncio
async def _impl_test_h348_active_agent_step_hits_hard_cap_as_progressing(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [msg(1, "user", "build"), status(2, "RUNNING")])
    client = DiscoApiClient(
        FakeTransport(db, states=["RUNNING"]), db_path=str(db), poll_interval_s=1.0
    )
    _h348_attach(client, _h348_span(1, "start"))
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=2.0)

    assert result == PROGRESSING_TIMEOUT
    assert clock.t == 1002.0


@pytest.mark.asyncio
async def _impl_test_h348_tainted_active_span_grants_no_extension(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [msg(1, "user", "build"), status(2, "RUNNING")])
    client = DiscoApiClient(
        FakeTransport(db, states=["RUNNING"]), db_path=str(db), poll_interval_s=1.0
    )
    _h348_attach(client, _h348_span(1, "start"))
    client._inspect_aggregations[_CID].note_unavailable()
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert clock.t == 1001.0
