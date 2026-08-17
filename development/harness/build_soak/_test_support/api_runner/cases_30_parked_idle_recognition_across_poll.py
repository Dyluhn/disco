"""Moved parked idle recognition across poll collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    INACTIVE_TIMEOUT,
    Any,
    DiscoApiClient,
    cast,
    msg,
)
from .helpers_01 import _seed_db
from .helpers_02 import (
    _killed_log,
    _ParkedIdleTransport,
)


async def _impl_test_poll_recognizes_parked_idle_when_invocation_starts_after_kill(tmp_path):
    """Seed 405414: a delayed approve_plan WS ack held the driver past the
    scenario kill, so a FRESH poll invocation began against a stable killed
    IDLE. `seen_active` is invocation-local; the durable log within the live
    state's seq horizon must prove the IDLE is parked, not pre-kick — the poll
    returns IDLE promptly instead of waiting out the inactivity window."""
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, _killed_log())
    client = DiscoApiClient(cast(Any, _ParkedIdleTransport(last_seq=6)), db_path=str(db))
    out = await client.poll_until_terminal_or_gate(_CID, inactivity_s=30.0, hard_cap_s=60.0)
    assert out == "IDLE"


async def _impl_test_poll_still_waits_through_live_pre_kick_idle(tmp_path):
    """Control: a genuinely pre-kick IDLE (user turn durable, no status event in
    the horizon) must still be waited through — the parked-IDLE rule cannot
    abort the drive before the run ever goes active."""
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [msg(1, "user", "build it")])
    client = DiscoApiClient(cast(Any, _ParkedIdleTransport(last_seq=1)), db_path=str(db))
    out = await client.poll_until_terminal_or_gate(_CID, inactivity_s=1.5, hard_cap_s=5.0)
    assert out == INACTIVE_TIMEOUT
