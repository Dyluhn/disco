"""Schema/version/migration contract tests for the SQLite event store.

Covers the frozen PKG-04-STORES schema contract:

* fresh schema/version and reopen-no-op;
* earliest and partial legacy schemas, preserved rows, and versioning;
* injected crash during migration, full rollback, then successful retry;
* malformed and future database versions rejected without schema/data change;
* future Event schema version rejected with the typed event migration error;
* raw event payload remains JSON text and the read path calls migration;
* bare-connection/store-created MCP tables have identical PRAGMA shapes;
* static source proof finds each MCP CREATE definition only in Core;
* the Tools compatibility entrypoint remains functional.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from disco.core import SqliteEventStore
from disco.core.migration import EventMigrationError, migrate_event
from disco.core.store.schema import (
    DATABASE_SCHEMA_VERSION,
    MIGRATION_STEP_NAMES,
    DatabaseSchemaError,
    apply_schema,
    ensure_mcp_approval_tables,
    mcp_approval_ddl,
    validate_schema,
)
from disco.tools.mcp.migrations import (
    ensure_mcp_approval_tables as tools_ensure_mcp_approval_tables,
)

# ---- fresh schema / version -------------------------------------------------


def test_fresh_schema_writes_version_row(tmp_path: Path) -> None:
    """A fresh database gets DATABASE_SCHEMA_VERSION and the version row."""
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    version = apply_schema(conn)
    assert version == DATABASE_SCHEMA_VERSION
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert row[0] == DATABASE_SCHEMA_VERSION
    conn.close()


def test_reopen_is_no_op(tmp_path: Path) -> None:
    """Reopening a version-1 database validates without writes."""
    db = tmp_path / "reopen.db"
    conn = sqlite3.connect(str(db))
    apply_schema(conn)
    conn.close()

    conn2 = sqlite3.connect(str(db))
    before = "\n".join(conn2.iterdump())
    before_changes = conn2.total_changes
    version = apply_schema(conn2)
    assert version == DATABASE_SCHEMA_VERSION
    validate_schema(conn2)
    assert conn2.total_changes == before_changes
    assert "\n".join(conn2.iterdump()) == before
    conn2.close()


def test_store_open_writes_version_and_reopen_preserves_it(tmp_path: Path) -> None:
    """SqliteEventStore writes the version row; a reopen sees the same version."""
    db = tmp_path / "store.db"
    s1 = SqliteEventStore(db)
    s1.close()

    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row is not None
    assert row[0] == DATABASE_SCHEMA_VERSION
    conn.close()

    s2 = SqliteEventStore(db)
    s2.close()


# ---- earliest and partial legacy schemas -----------------------------------


def test_earliest_legacy_schema_migrates_and_preserves_rows(tmp_path: Path) -> None:
    """A legacy DB with only a conversations table (no schema_meta) migrates
    to version 1 and preserves existing rows."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    # Earliest legacy: only conversations, missing many columns.
    conn.execute(
        "CREATE TABLE conversations ("
        "conversation_id TEXT PRIMARY KEY, "
        "owner_id TEXT NOT NULL, "
        "title TEXT, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO conversations VALUES ('legacy_conv', 'local', 'Old Title', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    # Open with the store — should migrate 0 -> 1.
    store = SqliteEventStore(db)
    row = store._conn.execute(
        "SELECT conversation_id, owner_id, title, created_at, space_id, status, "
        "surface, appkit_mode FROM conversations WHERE conversation_id = ?",
        ("legacy_conv",),
    ).fetchone()
    assert row["conversation_id"] == "legacy_conv"
    assert row["owner_id"] == "local"
    assert row["title"] == "Old Title"
    assert row["created_at"] == "2026-01-01"
    # Migrated columns should be present with defaults.
    assert row["space_id"] is None
    assert row["status"] is None
    assert row["surface"] is None
    assert row["appkit_mode"] == 0
    # Version row written.
    vrow = store._conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert vrow[0] == DATABASE_SCHEMA_VERSION
    store.close()


def test_partial_legacy_schema_with_share_tokens_preserves_and_freezes(tmp_path: Path) -> None:
    """A legacy DB with share_tokens missing bundle_title/bundle_surface
    migrates and freezes the title/surface at upgrade time."""
    db = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            surface TEXT
        );
        CREATE TABLE share_tokens (
            token TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            bundle_seq INTEGER NOT NULL
        );
        INSERT INTO conversations VALUES ('conv_s', 'local', 'Frozen Title', '2026-01-01', 'build');
        INSERT INTO share_tokens VALUES ('tok123', 'conv_s', 'local', '2026-01-01', NULL, 5);
        """
    )
    conn.commit()
    conn.close()

    store = SqliteEventStore(db)
    token = store.lookup_share_token("tok123")
    assert token is not None
    assert token["bundle_title"] == "Frozen Title"
    assert token["bundle_surface"] == "build"
    store.close()


# ---- injected crash during migration ---------------------------------------


def _create_crash_legacy_fixture(db: Path) -> str:
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE share_tokens (
            token TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            bundle_seq INTEGER NOT NULL
        );
        CREATE TABLE schedules (
            schedule_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            rrule TEXT NOT NULL,
            description TEXT NOT NULL,
            depth TEXT,
            model_override TEXT,
            created_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run TEXT
        );
        CREATE TABLE local_preview_leases (
            conversation_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            listener_port INTEGER NOT NULL UNIQUE,
            target_port INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        INSERT INTO conversations
            VALUES ('legacy_conv', 'local', 'Legacy title', '2026-01-01');
        INSERT INTO share_tokens
            VALUES ('legacy_token', 'legacy_conv', 'local', '2026-01-01', NULL, 1);
        """
    )
    conn.commit()
    before = "\n".join(conn.iterdump())
    conn.close()
    return before


def _stable_migration_step_id(step: str) -> str:
    # The inventory treats collected node IDs as durable identities. The schema
    # version written at this logical edge advances, but the edge itself does not;
    # retain its original inventory ID instead of manufacturing a delete/add pair
    # every time DATABASE_SCHEMA_VERSION changes.
    return "version:1" if step.startswith("version:") else step


@pytest.mark.parametrize(
    "crash_step",
    MIGRATION_STEP_NAMES,
    ids=_stable_migration_step_id,
)
def test_injected_crash_at_every_migration_edge_rolls_back_and_retry_succeeds(
    tmp_path: Path, crash_step: str
) -> None:
    """Every named migration edge rolls back fully and remains retryable."""

    observed: list[str] = []

    def crash_after_step(step: str) -> None:
        observed.append(step)
        if step == crash_step:
            raise RuntimeError(f"injected crash at {step}")

    db = tmp_path / f"crash-{crash_step.replace(':', '-')}.db"
    before = _create_crash_legacy_fixture(db)
    conn = sqlite3.connect(str(db))
    with pytest.raises(RuntimeError, match="injected crash at"):
        apply_schema(conn, after_step=crash_after_step)
    conn.close()

    conn = sqlite3.connect(str(db))
    assert "\n".join(conn.iterdump()) == before
    conn.close()

    conn = sqlite3.connect(str(db))
    version = apply_schema(conn)
    assert version == DATABASE_SCHEMA_VERSION
    preserved = conn.execute(
        "SELECT title FROM conversations WHERE conversation_id = 'legacy_conv'"
    ).fetchone()
    assert preserved == ("Legacy title",)
    conn.close()
    assert observed[-1] == crash_step


# ---- malformed and future database versions ---------------------------------


def test_future_database_version_rejected(tmp_path: Path) -> None:
    """A database with a version greater than DATABASE_SCHEMA_VERSION is
    rejected with DatabaseSchemaError before any schema/data writes."""
    db = tmp_path / "future.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    conn.execute(
        "INSERT INTO schema_meta VALUES ('schema_version', ?)", (DATABASE_SCHEMA_VERSION + 1,)
    )
    conn.commit()
    conn.close()

    with pytest.raises(DatabaseSchemaError, match="unsupported"):
        SqliteEventStore(db)


def test_malformed_version_rejected_by_validate_schema(tmp_path: Path) -> None:
    """validate_schema raises DatabaseSchemaError on a DB with no metadata."""
    db = tmp_path / "malformed.db"
    conn = sqlite3.connect(str(db))
    # No schema_meta at all — validate_schema should raise.
    with pytest.raises(DatabaseSchemaError, match="apply_schema must run first"):
        validate_schema(conn)
    conn.close()


@pytest.mark.parametrize(
    "schema_sql, expected",
    [
        ("CREATE TABLE schema_meta (wrong INTEGER)", "incompatible shape"),
        (
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL);"
            "INSERT INTO schema_meta VALUES ('schema_version', x'01')",
            "non-negative integer",
        ),
        (
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);"
            "INSERT INTO schema_meta VALUES ('schema_version', -1)",
            "non-negative integer",
        ),
    ],
)
def test_malformed_schema_metadata_fails_typed_without_writes(
    tmp_path: Path, schema_sql: str, expected: str
) -> None:
    db = tmp_path / "malformed-meta.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(schema_sql)
    conn.commit()
    before = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    with pytest.raises(DatabaseSchemaError, match=expected):
        apply_schema(conn)
    after = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    assert after == before
    conn.close()


def test_future_version_rejected_without_schema_data_change(tmp_path: Path) -> None:
    """A future-version database is rejected without modifying existing tables."""
    db = tmp_path / "future2.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    conn.execute(
        "INSERT INTO schema_meta VALUES ('schema_version', ?)", (DATABASE_SCHEMA_VERSION + 5,)
    )
    conn.execute("CREATE TABLE existing (x INTEGER)")
    conn.execute("INSERT INTO existing VALUES (42)")
    conn.commit()
    before = "\n".join(conn.iterdump())
    with pytest.raises(DatabaseSchemaError):
        apply_schema(conn)
    assert "\n".join(conn.iterdump()) == before
    conn.close()


# ---- future Event schema version --------------------------------------------


def test_future_event_schema_version_rejected_with_typed_error() -> None:
    """migrate_event raises EventMigrationError for a schema_version above the
    canonical Event SCHEMA_VERSION."""
    from disco.core._event_types import SCHEMA_VERSION as CURRENT_EVENT_SCHEMA_VERSION

    future_event = {"schema_version": CURRENT_EVENT_SCHEMA_VERSION + 1, "kind": "message"}
    with pytest.raises(EventMigrationError, match="newer than the canonical"):
        migrate_event(future_event)


def test_current_event_schema_version_passes_through() -> None:
    """An event at the current Event SCHEMA_VERSION passes through migrate_event."""
    from disco.core._event_types import SCHEMA_VERSION as CURRENT_EVENT_SCHEMA_VERSION

    event = {"schema_version": CURRENT_EVENT_SCHEMA_VERSION, "kind": "message"}
    result = migrate_event(event)
    assert result["schema_version"] == CURRENT_EVENT_SCHEMA_VERSION


def test_missing_schema_version_defaults_to_one() -> None:
    """An event without schema_version defaults to 1 and passes through."""
    event = {"kind": "message"}
    result = migrate_event(event)
    assert result.get("schema_version", 1) == 1


# ---- raw event payload remains JSON text and read path calls migration ------


def test_raw_event_payload_is_json_text(tmp_path: Path) -> None:
    """The events table stores payload as JSON text, not a blob or object."""
    db = tmp_path / "payload.db"
    store = SqliteEventStore(db)
    import asyncio

    from event_fakes import user_msg

    asyncio.run(store.append("conv_json", user_msg("test payload")))
    row = store._conn.execute(
        "SELECT payload FROM events WHERE conversation_id = ?", ("conv_json",)
    ).fetchone()
    assert row is not None
    payload = row["payload"]
    assert isinstance(payload, str)
    parsed = json.loads(payload)
    assert parsed["kind"] == "message"
    store.close()


def test_read_path_calls_migration(tmp_path: Path, monkeypatch) -> None:
    """The read path (_row_to_event) calls migrate_event on the stored payload."""
    db = tmp_path / "read_migration.db"
    store = SqliteEventStore(db)
    import asyncio

    from event_fakes import user_msg

    asyncio.run(store.append("conv_rm", user_msg("migration check")))

    call_count = 0
    original_migrate = migrate_event

    def counting_migrate(raw: dict) -> dict:
        nonlocal call_count
        call_count += 1
        return original_migrate(raw)

    monkeypatch.setattr("disco.core.store.sqlite.migrate_event", counting_migrate)
    events = asyncio.run(store.get_events("conv_rm"))
    assert len(events) == 1
    assert call_count == 1
    store.close()


# ---- MCP table shape equivalence --------------------------------------------


def _table_pragma_shape(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Return the PRAGMA table_info shape for a table as comparable tuples."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # Normalize to plain tuples so sqlite3.Row vs tuple differences don't matter.
    return [tuple(row) for row in rows]


def test_bare_connection_and_store_created_mcp_tables_have_identical_pragma_shapes(
    tmp_path: Path,
) -> None:
    """Bare-connection and store-created MCP tables have identical PRAGMA shapes."""
    # Bare connection via the Core schema delegate.
    bare_db = tmp_path / "bare.db"
    bare_conn = sqlite3.connect(str(bare_db))
    ensure_mcp_approval_tables(bare_conn)

    # Store-created schema.
    store_db = tmp_path / "store.db"
    store = SqliteEventStore(store_db)

    for table in ("mcp_approvals", "mcp_approval_pending", "mcp_config_approvals"):
        bare_shape = _table_pragma_shape(bare_conn, table)
        store_shape = _table_pragma_shape(store._conn, table)
        assert bare_shape == store_shape, f"PRAGMA shape mismatch for {table}"

    bare_conn.close()
    store.close()


def test_tools_compatibility_entrypoint_creates_identical_tables(tmp_path: Path) -> None:
    """The Tools ensure_mcp_approval_tables delegate creates tables with the
    same PRAGMA shape as the Core schema authority."""
    tools_db = tmp_path / "tools.db"
    tools_conn = sqlite3.connect(str(tools_db))
    tools_ensure_mcp_approval_tables(tools_conn)

    core_db = tmp_path / "core.db"
    core_conn = sqlite3.connect(str(core_db))
    ensure_mcp_approval_tables(core_conn)

    for table in ("mcp_approvals", "mcp_approval_pending", "mcp_config_approvals"):
        tools_shape = _table_pragma_shape(tools_conn, table)
        core_shape = _table_pragma_shape(core_conn, table)
        assert tools_shape == core_shape, f"PRAGMA shape mismatch for {table}"

    tools_conn.close()
    core_conn.close()


# ---- static source proof: MCP CREATE only in Core --------------------------


def test_mcp_create_definitions_found_only_in_core() -> None:
    """Static source proof: each MCP CREATE definition appears only in the Core
    schema module, not in the Tools migrations module."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[4]
    core_schema = (repo_root / "current/packages/core/src/disco/core/store/schema.py").read_text(
        encoding="utf-8"
    )
    tools_migrations = (repo_root / "current/packages/tools/src/disco/tools/mcp/migrations.py").read_text(
        encoding="utf-8"
    )

    # The three MCP CREATE statements are defined in Core.
    ddl_statements = mcp_approval_ddl()
    assert len(ddl_statements) == 3

    for ddl in ddl_statements:
        # The CREATE TABLE statement must appear in Core.
        assert "CREATE TABLE IF NOT EXISTS" in ddl
        # Extract the table name.
        table_name = ddl.split("CREATE TABLE IF NOT EXISTS")[1].split("(")[0].strip()
        assert table_name in (
            "mcp_approvals",
            "mcp_approval_pending",
            "mcp_config_approvals",
        )
        # The full DDL must appear in Core.
        assert ddl.strip() in core_schema, f"DDL for {table_name} not found in Core schema.py"

    # The Tools migrations module must NOT contain any CREATE TABLE for the
    # three MCP tables — it delegates to Core.
    for table in ("mcp_approvals", "mcp_approval_pending", "mcp_config_approvals"):
        create_pattern = f"CREATE TABLE IF NOT EXISTS {table}"
        assert create_pattern not in tools_migrations, (
            f"Tools migrations.py must not define CREATE TABLE for {table}"
        )


def test_tools_mcp_migrations_imports_core_schema() -> None:
    """The Tools migrations module imports from Core schema, not the reverse."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[4]
    tools_migrations = (repo_root / "current/packages/tools/src/disco/tools/mcp/migrations.py").read_text(
        encoding="utf-8"
    )
    core_schema = (repo_root / "current/packages/core/src/disco/core/store/schema.py").read_text(
        encoding="utf-8"
    )

    # Tools imports from Core.
    assert "from disco.core.store.schema import" in tools_migrations
    # Core does NOT import from Tools.
    assert "from disco.tools" not in core_schema
    assert "import disco.tools" not in core_schema


# ---- Tools compatibility entrypoint functional -------------------------------


def test_tools_ensure_mcp_approval_tables_functional(tmp_path: Path) -> None:
    """The Tools ensure_mcp_approval_tables entrypoint creates the tables and
    allows CRUD operations."""
    db = tmp_path / "tools_func.db"
    conn = sqlite3.connect(str(db))
    tools_ensure_mcp_approval_tables(conn)

    # Can insert and read.
    conn.execute(
        "INSERT INTO mcp_approvals (server, description_hash, approved_at, approved_by) "
        "VALUES (?, ?, ?, ?)",
        ("test_srv", "abc123", "2026-01-01", "operator"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT server, description_hash FROM mcp_approvals WHERE server = ?", ("test_srv",)
    ).fetchone()
    assert row[0] == "test_srv"
    assert row[1] == "abc123"
    conn.close()


def test_mcp_table_delegate_rolls_back_partial_schema_on_malformed_guard(tmp_path: Path) -> None:
    db = tmp_path / "mcp_partial.db"
    conn = sqlite3.connect(str(db))
    conn.execute(mcp_approval_ddl()[0])
    conn.execute(
        "CREATE TRIGGER schema_writer_guard_mcp_approvals_insert "
        "BEFORE INSERT ON mcp_approvals BEGIN SELECT 1; END"
    )
    conn.commit()

    with pytest.raises(DatabaseSchemaError, match="malformed writer guards"):
        ensure_mcp_approval_tables(conn)

    objects = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        ).fetchall()
    }
    assert "mcp_approvals" in objects
    assert "schema_writer_guard_mcp_approvals_insert" in objects
    assert "mcp_approval_pending" not in objects
    assert "mcp_config_approvals" not in objects
    conn.close()


def test_validate_schema_registers_current_writer_on_fresh_connection(tmp_path: Path) -> None:
    db = tmp_path / "validated-writer.db"
    SqliteEventStore(db).close()
    conn = sqlite3.connect(str(db))

    validate_schema(conn)
    conn.execute(
        "INSERT INTO mcp_approvals (server, description_hash, approved_at, approved_by) "
        "VALUES ('validated', 'hash', '2026-08-12', 'operator')"
    )
    conn.commit()

    assert conn.execute("SELECT server FROM mcp_approvals").fetchone() == ("validated",)
    conn.close()
