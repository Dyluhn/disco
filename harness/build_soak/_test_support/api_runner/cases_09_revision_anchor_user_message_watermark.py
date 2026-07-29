"""Moved revision anchor user message watermark collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    msg,
    pytest,
    status,
)
from .helpers_01 import (
    FakeTransport,
    _seed_db,
)


@pytest.mark.asyncio
async def _impl_test_latest_user_message_seq_tracks_user_turns(tmp_path):
    # The watermark used to attribute harness-sent turns: the highest USER message seq.
    db = tmp_path / "disco.db"
    _seed_db(
        db,
        _CID,
        [
            msg(1, "user", "build a page"),
            status(2, "RUNNING"),
            msg(3, "agent", "working", role="assistant"),  # NOT a user turn
            msg(8, "user", "revise the heading"),
        ],
    )
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"]), db_path=str(db), poll_interval_s=0.0
    )
    assert client.latest_user_message_seq(_CID) == 8
    # a new user turn beyond the watermark is detected; one at/under it is not
    assert await client.wait_for_new_user_message_seq(_CID, after_seq=3, timeout_s=1) == 8
    assert await client.wait_for_new_user_message_seq(_CID, after_seq=8, timeout_s=0.2) is None


@pytest.mark.asyncio
async def _impl_test_latest_user_message_seq_no_user_turns(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [status(1, "RUNNING")])  # no user message at all
    client = DiscoApiClient(
        FakeTransport(db, states=["FINISHED"]), db_path=str(db), poll_interval_s=0.0
    )
    assert client.latest_user_message_seq(_CID) == -1
