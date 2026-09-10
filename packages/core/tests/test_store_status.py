import sqlite3
from unittest.mock import patch

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)


@pytest.fixture
def store():
    s = SqliteEventStore(":memory:")
    yield s
    s.close()


async def test_status_write_through(store):
    cid = "conv_1"
    # Append StatusEvent
    ev = StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.RUNNING)
    await store.append(cid, ev)

    # Check if column is updated
    row = store._conn.execute(
        "SELECT status FROM conversations WHERE conversation_id = ?", (cid,)
    ).fetchone()
    assert row["status"] == "RUNNING"


async def test_non_status_event_column_untouched(store):
    cid = "conv_1"
    # Create conversation first (or it will be auto-created with NULL status by the first append)
    store.create_conversation(cid, title="Test")

    # Append MessageEvent
    ev = MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hello"))
    await store.append(cid, ev)

    # Check if column is still NULL
    row = store._conn.execute(
        "SELECT status FROM conversations WHERE conversation_id = ?", (cid,)
    ).fetchone()
    assert row["status"] is None


async def test_read_repair_on_list(store):
    cid = "conv_1"
    owner = "local"
    # 1. Manually insert a row with NULL status
    store.create_conversation(cid, owner_id=owner)
    # 2. Append a StatusEvent (it will update the column, so we manually NULL it
    #    out to simulate old data)
    await store.append(
        cid, StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.FINISHED)
    )
    store._conn.execute("UPDATE conversations SET status = NULL WHERE conversation_id = ?", (cid,))
    store._conn.commit()

    # Verify it's NULL
    row = store._conn.execute(
        "SELECT status FROM conversations WHERE conversation_id = ?", (cid,)
    ).fetchone()
    assert row["status"] is None

    # 3. Call list_conversation_summaries
    summaries = await store.list_conversation_summaries(owner_id=owner)
    assert len(summaries) == 1
    assert summaries[0].status == "FINISHED"

    # 4. Verify it was backfilled in the DB
    row = store._conn.execute(
        "SELECT status FROM conversations WHERE conversation_id = ?", (cid,)
    ).fetchone()
    assert row["status"] == "FINISHED"


async def test_repair_writes_once(store):
    cid = "conv_1"
    owner = "local"
    store.create_conversation(cid, owner_id=owner)
    await store.append(
        cid, StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.FINISHED)
    )
    store._conn.execute("UPDATE conversations SET status = NULL WHERE conversation_id = ?", (cid,))
    store._conn.commit()

    # Spy on get_state
    with patch.object(SqliteEventStore, "get_state", wraps=store.get_state) as spy:
        # First list triggers repair
        await store.list_conversation_summaries(owner_id=owner)
        assert spy.call_count == 1

        # Second list should NOT trigger repair as status is now in DB
        await store.list_conversation_summaries(owner_id=owner)
        assert spy.call_count == 1


async def test_zero_event_conversations_hidden_from_summaries(store):
    """BW-08: a pre-created conversation that never got any events (the eager-
    create ghost) must NOT appear in the History/Projects listing. Only
    conversations with at least one event are listed."""
    owner = "local"
    # A real, used conversation (has a first user message).
    store.create_conversation("conv_used", owner_id=owner)
    await store.append(
        "conv_used",
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="hi")),
    )
    # A ghost: pre-created row with zero events.
    store.create_conversation("conv_ghost", owner_id=owner)

    # nonempty_only=True (the History/Projects path) hides the ghost.
    summaries = await store.list_conversation_summaries(owner_id=owner, nonempty_only=True)
    ids = [s.conversation_id for s in summaries]
    assert ids == ["conv_used"]
    assert "conv_ghost" not in ids

    # The default (internal readers, e.g. the activity feed) still sees both, so
    # a freshly-kicked running task mid-first-append is never dropped.
    all_ids = [s.conversation_id for s in await store.list_conversation_summaries(owner_id=owner)]
    assert set(all_ids) == {"conv_used", "conv_ghost"}


async def test_schema_migration_adds_column(tmp_path):
    db_path = tmp_path / "test.db"

    # 1. Create a legacy DB without the status column
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            owner_id        TEXT NOT NULL,
            space_id        TEXT,
            title           TEXT,
            created_at      TEXT NOT NULL,
            surface         TEXT
        );
    """)
    conn.execute(
        "INSERT INTO conversations (conversation_id, owner_id, created_at) VALUES (?, ?, ?)",
        ("legacy_1", "local", "2026-06-10T10:00:00Z"),
    )
    conn.commit()
    conn.close()

    # 2. Open with SqliteEventStore (should trigger migration)
    store = SqliteEventStore(db_path)

    # 3. Verify column exists by performing a list (which triggers read-repair)
    # First, we need an event to repair FROM.
    await store.append(
        "legacy_1", StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.RUNNING)
    )

    # Now list. If migration failed, this will throw OperationalError: no such column: status
    summaries = await store.list_conversation_summaries(owner_id="local")
    assert len(summaries) == 1
    assert summaries[0].status == "RUNNING"

    # 4. Verify write-through works on the migrated table
    await store.append(
        "legacy_1", StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.FINISHED)
    )

    # Check DB directly
    row = store._conn.execute(
        "SELECT status FROM conversations WHERE conversation_id = ?", ("legacy_1",)
    ).fetchone()
    assert row["status"] == "FINISHED"

    store.close()
