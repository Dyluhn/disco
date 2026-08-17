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
    _insert_event,
    _inspect_snapshot,
    _install_clock,
    _seed_db,
)


def _h349_verifier_started(seq: int = 3, *, event_id: str = "verify_start"):
    return {
        "id": event_id,
        "seq": seq,
        "kind": "verifier_started",
        "source": "system",
        "verifier": "host",
        "operation": "host.verify_deliverable",
    }


def _h349_verifier_verdict(seq: int, *, requested_by_event_id: str):
    return {
        "id": f"verify_verdict_{seq}",
        "seq": seq,
        "kind": "verifier_verdict",
        "source": "system",
        "requested_by_event_id": requested_by_event_id,
    }


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


def _impl_test_h349_host_verifier_pairing_requires_exact_later_verdict(tmp_path):
    db = tmp_path / "disco.db"
    started = _h349_verifier_started(seq=4)
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            status(2, "RUNNING"),
            _h349_verifier_verdict(3, requested_by_event_id=started["id"]),
            started,
        ],
    )
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))

    assert client._latest_dangling_verifier(_CID) == (
        started["id"],
        "host.verify_deliverable",
    )

    _insert_event(db, _CID, _h349_verifier_verdict(5, requested_by_event_id="other_start"))
    assert client._latest_dangling_verifier(_CID) == (
        started["id"],
        "host.verify_deliverable",
    )

    _insert_event(db, _CID, _h349_verifier_verdict(6, requested_by_event_id=started["id"]))
    assert client._latest_dangling_verifier(_CID) is None

    contractless_db = tmp_path / "contractless.db"
    contractless = _h349_verifier_started(event_id="contractless_start")
    contractless["operation"] = ""
    _seed_db(
        contractless_db,
        _CID,
        [msg(1, "user", "build"), status(2, "RUNNING"), contractless],
    )
    contractless_client = DiscoApiClient(
        FakeTransport(contractless_db, states=["RUNNING"]), db_path=str(contractless_db)
    )
    assert contractless_client._latest_dangling_verifier(_CID) == (
        contractless["id"],
        "host.verify_deliverable",
    )


@pytest.mark.asyncio
async def _impl_test_h349_host_verifier_outlives_ordinary_inactivity(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    started = _h349_verifier_started()
    _seed_db(db, _CID, [msg(1, "user", "build"), status(2, "RUNNING"), started])

    def complete_on_third_read(reads):
        if reads == 3:
            _insert_event(
                db,
                _CID,
                _h349_verifier_verdict(4, requested_by_event_id=started["id"]),
            )

    transport = _H347StateTransport(
        db,
        states=["RUNNING", "RUNNING", "RUNNING", "FINISHED"],
        on_state_read=complete_on_third_read,
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=1.0)
    monkeypatch.setattr(
        _disco_mod,
        "_TOOL_TIMEOUT_OVERRIDES_S",
        {"host.verify_deliverable": 5.0},
    )
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == "FINISHED"
    assert transport.state_reads == 4


@pytest.mark.asyncio
async def _impl_test_h349_host_verifier_expires_at_its_bounded_deadline(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [msg(1, "user", "build"), status(2, "RUNNING"), _h349_verifier_started()],
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["RUNNING"]), db_path=str(db), poll_interval_s=1.0
    )
    monkeypatch.setattr(
        _disco_mod,
        "_TOOL_TIMEOUT_OVERRIDES_S",
        {"host.verify_deliverable": 2.0},
    )
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert clock.t == 1002.0
