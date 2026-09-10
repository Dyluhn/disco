"""Crash/rollback contract tests for the SQLite event store.

Covers the frozen PKG-04-STORES crash contract:

* append_many mid-batch failure rolls back all rows and preserves next seq;
* exception after commit/before publish leaves the event replayable;
* injected crash during migration, full rollback, then successful retry
  (store-level path).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from disco.core import SqliteEventStore
from disco.core.store.schema import (
    DATABASE_SCHEMA_VERSION,
    apply_schema,
)
from event_fakes import action, user_msg

# ---- append_many mid-batch failure ------------------------------------------


async def test_append_many_mid_batch_failure_rolls_back_all_rows(
    tmp_path: Path,
) -> None:
    """A failure mid-batch in append_many rolls back all rows in that batch
    and preserves the next seq for a subsequent append."""
    db = tmp_path / "crash_batch.db"
    store = SqliteEventStore(db)

    # First, append one event normally.
    first = await store.append("conv_batch", user_msg("first"))
    assert first.seq == 1

    # Now build a batch where the second event triggers a failure.
    # We use a payload-too-large event to force a mid-batch exception.
    from disco.core import ActionEvent, ToolCall

    huge_payload = {"x": "a" * (2 * 1024 * 1024)}
    huge_event = ActionEvent(
        thought="huge",
        tool_call=ToolCall(tool_name="shell", arguments=huge_payload),
    )

    batch = [action("ok_before"), huge_event, action("after_fail")]

    from disco.core.store.sqlite import EventPayloadTooLarge

    with pytest.raises(EventPayloadTooLarge):
        await store.append_many("conv_batch", batch)

    # The batch rolled back: only the first event exists.
    events = await store.get_events("conv_batch")
    assert len(events) == 1
    assert events[0].seq == 1

    # The next seq is preserved (2, not 4).
    next_event = await store.append("conv_batch", action("after"))
    assert next_event.seq == 2

    store.close()


async def test_append_many_successful_batch_preserves_seq(tmp_path: Path) -> None:
    """A successful append_many assigns contiguous seqs and persists all."""
    db = tmp_path / "ok_batch.db"
    store = SqliteEventStore(db)

    batch = [user_msg("u"), action("a1"), action("a2")]
    stored = await store.append_many("conv_ok", batch)
    assert [e.seq for e in stored] == [1, 2, 3]

    events = await store.get_events("conv_ok")
    assert [e.seq for e in events] == [1, 2, 3]

    store.close()


# ---- exception after commit / before publish -------------------------------


async def test_exception_after_commit_before_publish_leaves_event_replayable(
    tmp_path: Path,
) -> None:
    """If an exception occurs after the commit but before publish, the event
    is durable and replayable on reopen."""
    db = tmp_path / "commit_no_publish.db"
    store = SqliteEventStore(db)

    # Patch _publish to raise after the commit.
    original_publish = store._publish
    call_count = 0

    def failing_publish(conversation_id: str, event: object) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("publish failed after commit")

    store._publish = failing_publish  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="publish failed"):
        await store.append("conv_replay", user_msg("committed"))

    # The event was committed despite the publish failure.
    store._publish = original_publish  # type: ignore[method-assign]
    events = await store.get_events("conv_replay")
    assert len(events) == 1
    assert events[0].seq == 1

    # Reopen: the event is durable.
    store.close()
    store2 = SqliteEventStore(db)
    events2 = await store2.get_events("conv_replay")
    assert len(events2) == 1
    assert events2[0].seq == 1
    store2.close()


# ---- store-level migration crash and retry ---------------------------------


def test_store_migration_crash_rolls_back_and_retry_succeeds(tmp_path: Path) -> None:
    """A crash during the store's schema migration rolls back; a retry via
    store reopen succeeds."""
    db = tmp_path / "store_crash.db"

    # Create a legacy DB.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE conversations ("
        "conversation_id TEXT PRIMARY KEY, "
        "owner_id TEXT NOT NULL, "
        "title TEXT, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO conversations VALUES ('legacy_crash', 'local', 'Title', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    # Inject a crash during apply_schema via the after_step hook.
    # We call apply_schema directly to simulate the crash.
    conn = sqlite3.connect(str(db))

    def crash_hook(_step: str) -> None:
        raise RuntimeError("store migration crash")

    with pytest.raises(RuntimeError, match="store migration crash"):
        apply_schema(conn, after_step=crash_hook)
    conn.close()

    # The migration rolled back: no schema_meta.
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    assert row is None
    conn.close()

    # Retry via store open succeeds.
    store = SqliteEventStore(db)
    vrow = store._conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert vrow[0] == DATABASE_SCHEMA_VERSION
    # Legacy row preserved.
    crow = store._conn.execute(
        "SELECT conversation_id, title FROM conversations WHERE conversation_id = ?",
        ("legacy_crash",),
    ).fetchone()
    assert crow["conversation_id"] == "legacy_crash"
    assert crow["title"] == "Title"
    store.close()
