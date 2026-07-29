"""Moved h347 a durable in flight collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    INACTIVE_TIMEOUT,
    PROGRESSING_TIMEOUT,
    DiscoApiClient,
    _disco_mod,
    action,
    inspect,
    msg,
    observation,
    pytest,
    sqlite3,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _FakeClock,
    _h347_seed,
    _H347StateTransport,
    _insert_event,
    _install_clock,
    _seed_db,
)


def _impl_test_h347_pairing_requires_a_strictly_later_durable_response(tmp_path):
    db = tmp_path / "disco.db"
    pending = action(5, "verify_appkit_app", action_id="act_h347")
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            observation(2, pending["id"], tool="verify_appkit_app"),
            pending,
        ],
    )
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))

    assert client._latest_dangling_action(_CID) == (pending["id"], "verify_appkit_app")

    _insert_event(db, _CID, observation(6, pending["id"], tool="verify_appkit_app"))
    assert client._latest_dangling_action(_CID) is None


def _impl_test_h347_older_orphan_is_not_misclassified_as_currently_executing(tmp_path):
    db = tmp_path / "disco.db"
    orphan = action(3, "file_write", action_id="act_orphan")
    completed = action(4, "verify_appkit_app", action_id="act_completed")
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build"),
            status(2, "RUNNING"),
            orphan,
            completed,
            observation(5, completed["id"], tool="verify_appkit_app"),
        ],
    )
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))

    assert client._latest_dangling_action(_CID) is None


@pytest.mark.asyncio
async def _impl_test_h347_delayed_real_pairing_outlives_ordinary_inactivity(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    pending = _h347_seed(db)

    def pair_on_third_read(reads):
        if reads == 3:
            _insert_event(
                db,
                _CID,
                observation(4, pending["id"], tool="verify_appkit_app"),
            )

    transport = _H347StateTransport(
        db,
        states=["RUNNING", "RUNNING", "RUNNING", "FINISHED"],
        on_state_read=pair_on_third_read,
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=1.0)
    monkeypatch.setattr(_disco_mod, "_DEFAULT_TOOL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == "FINISHED"
    assert transport.state_reads == 4


@pytest.mark.asyncio
async def _impl_test_h347_pairing_between_marker_and_action_read_is_progress(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    pending = _h347_seed(db)
    transport = _H347StateTransport(
        db,
        states=["RUNNING", "RUNNING", "FINISHED"],
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=1.0)
    original_progress_marker = client._progress_marker
    marker_reads = 0

    def pair_after_stale_marker(_conversation_id):
        nonlocal marker_reads
        marker_reads += 1
        stale_marker = original_progress_marker(_conversation_id)
        if marker_reads == 3:
            _insert_event(
                db,
                _CID,
                observation(4, pending["id"], tool="verify_appkit_app"),
            )
        return stale_marker

    monkeypatch.setattr(client, "_progress_marker", pair_after_stale_marker)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == "FINISHED"
    assert marker_reads == 3


@pytest.mark.asyncio
async def _impl_test_h347_never_paired_action_expires_at_bounded_deadline(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _h347_seed(db)
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))
    client._poll = 1.0
    monkeypatch.setattr(_disco_mod, "_DEFAULT_TOOL_TIMEOUT_S", 2.0)
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert clock.t == 1002.0


@pytest.mark.asyncio
async def _impl_test_h347_no_dangling_action_keeps_ordinary_inactivity(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [msg(1, "user", "build"), status(2, "RUNNING")])
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))
    client._poll = 1.0
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert clock.t == 1001.0


@pytest.mark.asyncio
async def _impl_test_h347_malformed_durable_action_grants_no_extension(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _h347_seed(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE events SET payload = ? WHERE conversation_id = ? AND seq = 3",
            ("{", _CID),
        )
        conn.commit()
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))
    client._poll = 1.0
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert clock.t == 1001.0


@pytest.mark.asyncio
async def _impl_test_h347_row_payload_identity_mismatch_grants_no_extension(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _h347_seed(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE events SET kind = ? WHERE conversation_id = ? AND seq = 3",
            ("status", _CID),
        )
        conn.commit()
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))
    client._poll = 1.0
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert clock.t == 1001.0


@pytest.mark.asyncio
async def _impl_test_h347_unreadable_snapshot_cannot_restart_same_action_deadline(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _h347_seed(db)
    with sqlite3.connect(str(db)) as conn:
        original_payload = conn.execute(
            "SELECT payload FROM events WHERE conversation_id = ? AND seq = 3",
            (_CID,),
        ).fetchone()[0]

    def corrupt_then_restore(reads):
        if reads not in {2, 3}:
            return
        payload = "{" if reads == 2 else original_payload
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "UPDATE events SET payload = ? WHERE conversation_id = ? AND seq = 3",
                (payload, _CID),
            )
            conn.commit()

    transport = _H347StateTransport(
        db,
        states=["RUNNING"],
        on_state_read=corrupt_then_restore,
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=1.0)
    monkeypatch.setattr(_disco_mod, "_DEFAULT_TOOL_TIMEOUT_S", 2.0)
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert transport.state_reads == 3
    assert clock.t == 1002.0


@pytest.mark.asyncio
async def _impl_test_h347_unrelated_progress_does_not_restart_action_deadline(
    tmp_path, monkeypatch
):
    db = tmp_path / "disco.db"
    _h347_seed(db)

    def append_heartbeat(reads):
        _insert_event(db, _CID, status(3 + reads, "RUNNING", f"heartbeat_{reads}"))

    transport = _H347StateTransport(db, states=["RUNNING"], on_state_read=append_heartbeat)
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=1.0)
    monkeypatch.setattr(_disco_mod, "_DEFAULT_TOOL_TIMEOUT_S", 2.0)
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert transport.state_reads == 3
    assert clock.t == 1002.0


@pytest.mark.asyncio
async def _impl_test_h347_simultaneous_hard_cap_wins_over_action_deadline(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _h347_seed(db)
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))
    client._poll = 1.0
    monkeypatch.setattr(_disco_mod, "_DEFAULT_TOOL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=10.0, hard_cap_s=1.0)

    assert result == PROGRESSING_TIMEOUT
    assert clock.t == 1001.0


@pytest.mark.asyncio
async def _impl_test_h347_slides_override_is_behaviorally_honored(tmp_path, monkeypatch):
    db = tmp_path / "disco.db"
    _h347_seed(db, tool="slides_generate")
    client = DiscoApiClient(FakeTransport(db, states=["RUNNING"]), db_path=str(db))
    client._poll = 1.0
    monkeypatch.setattr(_disco_mod, "_DEFAULT_TOOL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(_disco_mod, "_TOOL_TIMEOUT_OVERRIDES_S", {"slides_generate": 3.0})
    monkeypatch.setattr(_disco_mod, "_ACTION_RESULT_PERSISTENCE_GRACE_S", 0.0)
    clock = _FakeClock()
    _install_clock(monkeypatch, clock)

    result = await client.poll_until_terminal(_CID, inactivity_s=0.1, hard_cap_s=20.0)

    assert result == INACTIVE_TIMEOUT
    assert clock.t == 1003.0


def _impl_test_h347_timeout_constants_are_bound_to_product_definitions():
    from disco.tools.builtin.slides import SlidesTool
    from disco.tools.executor import DefaultToolExecutor

    default = inspect.signature(DefaultToolExecutor).parameters["default_timeout_s"].default
    assert _disco_mod._DEFAULT_TOOL_TIMEOUT_S == default == 300
    assert _disco_mod._SLIDES_GENERATE_TIMEOUT_S == SlidesTool.definition.timeout_s == 900
    assert 0 < _disco_mod._ACTION_RESULT_PERSISTENCE_GRACE_S <= 30
