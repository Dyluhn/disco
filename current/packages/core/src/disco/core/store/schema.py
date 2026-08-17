"""Sole database DDL/version authority for the SQLite event store.

Owns ``DATABASE_SCHEMA_VERSION``, every current table/index definition, the
explicit conditional legacy-column migrations, data repairs, exact shape
validation, and the three MCP approval CREATE statements. A single source of
truth removes the duplicate CREATE authorities that could drift (DM-020) and
gives the additive SQLite migration a real database version with rollback
proof (DM-014).

Contract:

* Missing schema metadata means legacy version 0. ``apply_schema`` applies
  0->2 in an explicit SQLite transaction and writes the version row last.
* Version 2 adds connection-bound writer guards. A version-1/legacy reader can
  still read additive tables, but a writer that has not registered the current
  schema version is rejected by SQLite before changing a protected table.
* Missing columns are determined with ``PRAGMA table_info``; no catch-all
  ``except sqlite3.OperationalError: pass`` remains.
* On any exception during migration the complete transaction rolls back so a
  reopen retries from version 0.
* On version 1, ``validate_schema`` checks required tables/columns without
  writes. Malformed metadata or a version greater than 1 raises a typed error
  before any schema/data writes.
* WAL and ``synchronous=FULL`` are preserved by the caller (``SqliteEventStore``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

DATABASE_SCHEMA_VERSION = 2
_PREVIOUS_DATABASE_SCHEMA_VERSION = 1
_WRITER_VERSION_FUNCTION = "disco_schema_writer_version"

_SCHEMA_VERSION_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
)


class DatabaseSchemaError(RuntimeError):
    """Typed error for a malformed or unsupported database schema version."""


# ---------------------------------------------------------------------------
# DDL — every current table/index definition lives here and only here.
# ---------------------------------------------------------------------------

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    conversation_id TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    id              TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    PRIMARY KEY (conversation_id, seq),
    UNIQUE (conversation_id, id)
);
"""

_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    space_id        TEXT,
    title           TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT,
    surface         TEXT,
    origin          TEXT,
    appkit_mode     INTEGER NOT NULL DEFAULT 0
);
"""

_CONVERSATIONS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_conversations_owner
    ON conversations (owner_id, created_at DESC);
"""

_SHARE_TOKENS_DDL = """
CREATE TABLE IF NOT EXISTS share_tokens (
    token          TEXT    PRIMARY KEY,
    conversation_id TEXT   NOT NULL,
    owner_id       TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    revoked_at     TEXT,
    bundle_seq     INTEGER NOT NULL,
    bundle_title   TEXT,
    bundle_surface TEXT NOT NULL DEFAULT 'research',
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
"""

_SHARE_TOKENS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_share_tokens_conv
    ON share_tokens (conversation_id);
"""

_PREVIEW_REDEMPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS preview_redemptions (
    jti         TEXT PRIMARY KEY,
    expires_at  INTEGER NOT NULL,
    consumed_at INTEGER
);
"""

_PREVIEW_REDEMPTIONS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_preview_redemptions_expiry
    ON preview_redemptions (expires_at);
"""

_LOCAL_PREVIEW_LEASES_DDL = """
CREATE TABLE IF NOT EXISTS local_preview_leases (
    conversation_id TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    listener_port   INTEGER NOT NULL UNIQUE,
    target_port     INTEGER NOT NULL,
    authority_id    TEXT NOT NULL,
    expires_at      INTEGER NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
"""

_LOCAL_PREVIEW_LEASES_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_local_preview_leases_expiry
    ON local_preview_leases (expires_at);
"""

_LOCAL_PREVIEW_ORIGIN_STATE_DDL = """
CREATE TABLE IF NOT EXISTS local_preview_origin_state (
    listener_port INTEGER PRIMARY KEY,
    authority_id  TEXT NOT NULL
);
"""

_SCHEDULES_DDL = """
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id     TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    rrule           TEXT NOT NULL,
    description     TEXT NOT NULL,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    depth           TEXT,
    model_override  TEXT,
    created_at      TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    next_run        TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
"""

_SCHEDULES_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_schedules_owner
    ON schedules (owner_id, conversation_id);
"""

_SCHEDULE_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS schedule_runs (
    run_id          TEXT PRIMARY KEY,
    schedule_id     TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    fired_at        TEXT NOT NULL,
    coalesced       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id)
);
"""

_SCHEDULE_RUNS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_schedule_runs_sched
    ON schedule_runs (schedule_id, fired_at DESC);
"""

# The three MCP approval CREATE statements live here (one Core source
# location only — DM-020). Tools imports Core, never the reverse.
_MCP_APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_approvals (
    server          TEXT PRIMARY KEY,
    description_hash TEXT NOT NULL,
    approved_at     TEXT NOT NULL,
    approved_by     TEXT NOT NULL
);
"""

_MCP_APPROVAL_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS mcp_approval_pending (
    server          TEXT PRIMARY KEY,
    old_hash        TEXT NOT NULL,
    new_hash        TEXT NOT NULL,
    detected_at     TEXT NOT NULL
);
"""

_MCP_CONFIG_APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_config_approvals (
    server          TEXT PRIMARY KEY,
    config_hash     TEXT NOT NULL,
    approved_at     TEXT NOT NULL,
    approved_by     TEXT NOT NULL
);
"""

_DOD_SPECS_DDL = """
CREATE TABLE IF NOT EXISTS dod_specs (
    conversation_id TEXT PRIMARY KEY,
    spec            TEXT NOT NULL,
    set_at          TEXT NOT NULL,
    set_by          TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
);
"""

_NAMED_DDL = (
    ("events", _EVENTS_DDL),
    ("conversations", _CONVERSATIONS_DDL),
    ("idx_conversations_owner", _CONVERSATIONS_INDEX_DDL),
    ("share_tokens", _SHARE_TOKENS_DDL),
    ("idx_share_tokens_conv", _SHARE_TOKENS_INDEX_DDL),
    ("preview_redemptions", _PREVIEW_REDEMPTIONS_DDL),
    ("idx_preview_redemptions_expiry", _PREVIEW_REDEMPTIONS_INDEX_DDL),
    ("local_preview_leases", _LOCAL_PREVIEW_LEASES_DDL),
    ("idx_local_preview_leases_expiry", _LOCAL_PREVIEW_LEASES_INDEX_DDL),
    ("local_preview_origin_state", _LOCAL_PREVIEW_ORIGIN_STATE_DDL),
    ("schedules", _SCHEDULES_DDL),
    ("idx_schedules_owner", _SCHEDULES_INDEX_DDL),
    ("schedule_runs", _SCHEDULE_RUNS_DDL),
    ("idx_schedule_runs_sched", _SCHEDULE_RUNS_INDEX_DDL),
    ("mcp_approvals", _MCP_APPROVALS_DDL),
    ("mcp_approval_pending", _MCP_APPROVAL_PENDING_DDL),
    ("mcp_config_approvals", _MCP_CONFIG_APPROVALS_DDL),
    ("dod_specs", _DOD_SPECS_DDL),
)
_WRITER_GUARD_TABLES = (
    "schema_meta",
    *(name for name, _ddl in _NAMED_DDL if not name.startswith("idx_")),
)
_WRITER_GUARD_OPERATIONS = ("INSERT", "UPDATE", "DELETE")

# The three MCP CREATE statements, exposed for the Tools compatibility
# delegate so bare connections and store-created schemas are identical.
_MCP_DDL = (_MCP_APPROVALS_DDL, _MCP_APPROVAL_PENDING_DDL, _MCP_CONFIG_APPROVALS_DDL)

# Required tables and their required columns for version-1 validation.
_REQUIRED_TABLES: dict[str, tuple[str, ...]] = {
    "events": (
        "conversation_id",
        "seq",
        "id",
        "kind",
        "source",
        "created_at",
        "payload",
    ),
    "conversations": (
        "conversation_id",
        "owner_id",
        "created_at",
        "space_id",
        "title",
        "status",
        "surface",
        "origin",
        "appkit_mode",
    ),
    "share_tokens": (
        "token",
        "conversation_id",
        "owner_id",
        "created_at",
        "revoked_at",
        "bundle_seq",
        "bundle_title",
        "bundle_surface",
    ),
    "preview_redemptions": ("jti", "expires_at", "consumed_at"),
    "local_preview_leases": (
        "conversation_id",
        "owner_id",
        "listener_port",
        "target_port",
        "authority_id",
        "expires_at",
    ),
    "local_preview_origin_state": ("listener_port", "authority_id"),
    "schedules": (
        "schedule_id",
        "conversation_id",
        "owner_id",
        "rrule",
        "description",
        "timezone",
        "depth",
        "model_override",
        "created_at",
        "enabled",
        "next_run",
    ),
    "schedule_runs": (
        "run_id",
        "schedule_id",
        "conversation_id",
        "fired_at",
        "coalesced",
    ),
    "mcp_approvals": ("server", "description_hash", "approved_at", "approved_by"),
    "mcp_approval_pending": ("server", "old_hash", "new_hash", "detected_at"),
    "mcp_config_approvals": ("server", "config_hash", "approved_at", "approved_by"),
    "dod_specs": ("conversation_id", "spec", "set_at", "set_by"),
}
_REQUIRED_INDEXES: dict[str, tuple[tuple[str, bool], ...]] = {
    "idx_conversations_owner": (("owner_id", False), ("created_at", True)),
    "idx_local_preview_leases_expiry": (("expires_at", False),),
    "idx_preview_redemptions_expiry": (("expires_at", False),),
    "idx_schedule_runs_sched": (("schedule_id", False), ("fired_at", True)),
    "idx_schedules_owner": (("owner_id", False), ("conversation_id", False)),
    "idx_share_tokens_conv": (("conversation_id", False),),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _read_version(conn: sqlite3.Connection) -> int:
    """Read the schema version. Missing metadata means legacy version 0."""
    if not _table_exists(conn, "schema_meta"):
        return 0
    if _table_columns(conn, "schema_meta") != {"key", "value"}:
        raise DatabaseSchemaError("schema_meta has an incompatible shape")
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise DatabaseSchemaError("schema_meta cannot be read") from exc
    if row is None:
        return 0
    value = row[0]
    if type(value) is not int or value < 0:
        raise DatabaseSchemaError("schema_version must be a non-negative integer")
    return value


def _index_shape(conn: sqlite3.Connection, index: str) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (str(row[2]), bool(row[3]))
        for row in conn.execute(f"PRAGMA index_xinfo({index})").fetchall()
        if row[5]
    )


MigrationStepHook = Callable[[str], None]


def _emit_step(after_step: MigrationStepHook | None, step: str) -> None:
    if after_step is not None:
        after_step(step)


# ---------------------------------------------------------------------------
# Conditional legacy-column migrations (0 -> 1)
# ---------------------------------------------------------------------------

# Each legacy column that must be added via ALTER TABLE when upgrading a
# pre-existing table. The CREATE TABLE IF NOT EXISTS above already includes
# these columns for fresh databases; these ALTERs handle old databases where
# the table exists but is missing columns added after the original CREATE.
_LEGACY_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("conversations", "space_id", "TEXT"),
    ("share_tokens", "bundle_title", "TEXT"),
    ("share_tokens", "bundle_surface", "TEXT"),
    ("conversations", "surface", "TEXT"),
    ("conversations", "status", "TEXT"),
    ("conversations", "appkit_mode", "INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "origin", "TEXT"),
    ("schedules", "timezone", "TEXT NOT NULL DEFAULT 'UTC'"),
    ("local_preview_leases", "authority_id", "TEXT NOT NULL DEFAULT ''"),
)
MIGRATION_STEP_NAMES = (
    "ddl:schema_meta",
    *(f"ddl:{name}" for name, _ddl in _NAMED_DDL),
    *(f"column:{table}.{column}" for table, column, _definition in _LEGACY_COLUMN_MIGRATIONS),
    "repair:preview_leases",
    "repair:share_titles",
    "repair:share_surfaces",
    *(
        f"guard:{table}:{operation.lower()}"
        for table in _WRITER_GUARD_TABLES
        for operation in _WRITER_GUARD_OPERATIONS
    ),
    "before:version",
    f"version:{DATABASE_SCHEMA_VERSION}",
)


def _apply_legacy_column_migrations(
    conn: sqlite3.Connection, after_step: MigrationStepHook | None
) -> None:
    """Add missing legacy columns using PRAGMA table_info (no catch-all)."""
    for table, column, definition in _LEGACY_COLUMN_MIGRATIONS:
        if _table_exists(conn, table) and column not in _table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        _emit_step(after_step, f"column:{table}.{column}")


def _apply_data_repairs(conn: sqlite3.Connection, after_step: MigrationStepHook | None) -> None:
    """Data repairs that must run during the 0->1 migration."""
    # Pre-authority local Preview leases cannot safely survive the upgrade:
    # their listener may refer to any process generation on the same port.
    if _table_exists(conn, "local_preview_leases"):
        conn.execute("DELETE FROM local_preview_leases WHERE authority_id = ''")
    _emit_step(after_step, "repair:preview_leases")
    # Legacy share rows could not preserve their original metadata. Freeze
    # the current values once at upgrade so they at least stop tracking
    # future title/surface changes after this security migration. The NULL
    # surface is the migration marker; newly issued rows always set it even
    # when their captured title is legitimately NULL.
    if _table_exists(conn, "share_tokens") and _table_exists(conn, "conversations"):
        conn.execute(
            "UPDATE share_tokens SET bundle_title = "
            "(SELECT title FROM conversations WHERE conversations.conversation_id = "
            "share_tokens.conversation_id) WHERE bundle_surface IS NULL"
        )
    _emit_step(after_step, "repair:share_titles")
    if _table_exists(conn, "share_tokens") and _table_exists(conn, "conversations"):
        conn.execute(
            "UPDATE share_tokens SET bundle_surface = COALESCE("
            "(SELECT surface FROM conversations WHERE conversations.conversation_id = "
            "share_tokens.conversation_id), 'research') WHERE bundle_surface IS NULL"
        )
    _emit_step(after_step, "repair:share_surfaces")


def _create_all_tables(conn: sqlite3.Connection, after_step: MigrationStepHook | None) -> None:
    """Create every current table and index (idempotent for fresh DBs)."""
    conn.execute(_SCHEMA_VERSION_TABLE_SQL)
    _emit_step(after_step, "ddl:schema_meta")
    for name, ddl in _NAMED_DDL:
        conn.execute(ddl)
        _emit_step(after_step, f"ddl:{name}")


def _register_writer_version(conn: sqlite3.Connection) -> None:
    """Identify this connection to the persistent downgrade-write guards."""
    conn.create_function(
        _WRITER_VERSION_FUNCTION,
        0,
        lambda: DATABASE_SCHEMA_VERSION,
        deterministic=True,
    )


def _writer_guard_sql(table: str, operation: str) -> str:
    trigger = f"schema_writer_guard_{table}_{operation.lower()}"
    return f"""
CREATE TRIGGER IF NOT EXISTS {trigger}
BEFORE {operation} ON {table}
WHEN {_WRITER_VERSION_FUNCTION}() IS NOT {DATABASE_SCHEMA_VERSION}
BEGIN
    SELECT RAISE(ABORT, 'unsupported database schema writer');
END;
"""


def _normalized_sql(sql: str) -> str:
    normalized = " ".join(sql.rstrip().removesuffix(";").split()).casefold()
    # sqlite_master preserves the trigger body but drops the idempotent creation
    # modifier. It is execution policy, not part of the stored trigger semantics.
    return normalized.replace("create trigger if not exists ", "create trigger ", 1)


def _create_writer_guards(
    conn: sqlite3.Connection,
    after_step: MigrationStepHook | None,
    *,
    tables: tuple[str, ...] = _WRITER_GUARD_TABLES,
) -> None:
    for table in tables:
        for operation in _WRITER_GUARD_OPERATIONS:
            conn.execute(_writer_guard_sql(table, operation))
            _emit_step(after_step, f"guard:{table}:{operation.lower()}")
    _validate_writer_guards(conn, tables=tables)


def _validate_writer_guards(
    conn: sqlite3.Connection,
    *,
    tables: tuple[str, ...] = _WRITER_GUARD_TABLES,
) -> None:
    triggers = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    expected_triggers = {
        f"schema_writer_guard_{table}_{operation.lower()}": _writer_guard_sql(table, operation)
        for table in tables
        for operation in _WRITER_GUARD_OPERATIONS
    }
    missing_triggers = sorted(set(expected_triggers) - set(triggers))
    if missing_triggers:
        raise DatabaseSchemaError(
            "database is missing required writer guards: " + ", ".join(missing_triggers)
        )
    malformed_triggers = sorted(
        name
        for name, expected_sql in expected_triggers.items()
        if _normalized_sql(triggers[name]) != _normalized_sql(expected_sql)
    )
    if malformed_triggers:
        raise DatabaseSchemaError(
            "database has malformed writer guards: " + ", ".join(malformed_triggers)
        )


def _write_version_row(conn: sqlite3.Connection, version: int) -> None:
    """Write the schema version row last (after all DDL and data repairs)."""
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (version,),
    )


def _validate_schema_shape(conn: sqlite3.Connection, *, require_guards: bool) -> None:
    for table, required_columns in _REQUIRED_TABLES.items():
        if not _table_exists(conn, table):
            raise DatabaseSchemaError(f"required table '{table}' is missing")
        actual_columns = _table_columns(conn, table)
        expected_columns = set(required_columns)
        if actual_columns != expected_columns:
            missing = sorted(expected_columns - actual_columns)
            extra = sorted(actual_columns - expected_columns)
            raise DatabaseSchemaError(
                f"table '{table}' column shape is incompatible (missing={missing}, extra={extra})"
            )
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    missing_indexes = sorted(set(_REQUIRED_INDEXES) - indexes)
    if missing_indexes:
        raise DatabaseSchemaError(
            f"database is missing required indexes: {', '.join(missing_indexes)}"
        )
    for index, expected_shape in _REQUIRED_INDEXES.items():
        actual_shape = _index_shape(conn, index)
        if actual_shape != expected_shape:
            raise DatabaseSchemaError(
                f"index '{index}' shape is incompatible "
                f"(expected={expected_shape}, actual={actual_shape})"
            )
    if require_guards:
        _validate_writer_guards(conn)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_schema(
    conn: sqlite3.Connection,
    *,
    after_step: MigrationStepHook | None = None,
) -> int:
    """Apply the schema to ``conn`` and return the active schema version.

    Missing schema metadata means legacy version 0. The 0->2 migration runs
    in an explicit SQLite transaction and writes the version row last. On
    any exception the complete migration rolls back so a reopen retries from
    version 0.

    ``after_step`` is a deterministic crash-test seam. It receives every name
    in ``MIGRATION_STEP_NAMES`` after that edge executes. Raising from any edge
    rolls the complete migration back.
    """
    _register_writer_version(conn)
    version = _read_version(conn)
    if version not in {0, _PREVIOUS_DATABASE_SCHEMA_VERSION, DATABASE_SCHEMA_VERSION}:
        raise DatabaseSchemaError(
            f"database schema version {version} is unsupported; "
            f"this binary requires {DATABASE_SCHEMA_VERSION}"
        )
    if version == DATABASE_SCHEMA_VERSION:
        # Already at the current version; validate without writes.
        validate_schema(conn)
        return version

    # Version 0 builds the current shape; version 1 adds only the persistent
    # connection-version guards. Both transitions are one SQLite transaction.
    # We use explicit BEGIN/COMMIT/ROLLBACK (not the `with conn:` context
    # manager) because Python's sqlite3 context manager does not reliably
    # roll back DDL statements in all journal modes. Explicit transaction
    # control ensures the complete migration (DDL + data + version row) is
    # atomic: on any exception the entire migration rolls back so a reopen
    # retries from version 0.
    if conn.in_transaction:
        raise DatabaseSchemaError("schema migration requires an idle connection")
    if version == _PREVIOUS_DATABASE_SCHEMA_VERSION:
        _validate_schema_shape(conn, require_guards=False)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if version == 0:
            _create_all_tables(conn, after_step)
            _apply_legacy_column_migrations(conn, after_step)
            _apply_data_repairs(conn, after_step)
        _create_writer_guards(conn, after_step)
        _emit_step(after_step, "before:version")
        _write_version_row(conn, DATABASE_SCHEMA_VERSION)
        _emit_step(after_step, f"version:{DATABASE_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return DATABASE_SCHEMA_VERSION


def validate_schema(conn: sqlite3.Connection) -> None:
    """Validate required tables/columns/guards on a current database without writes.

    Malformed metadata or a non-current version raises ``DatabaseSchemaError``
    before any schema/data writes.
    """
    _register_writer_version(conn)
    version = _read_version(conn)
    if version == 0:
        # No schema_meta table at all — this is a legacy/fresh DB that has
        # not been migrated. The caller should run apply_schema.
        raise DatabaseSchemaError("database has no schema metadata; apply_schema must run first")
    if version != DATABASE_SCHEMA_VERSION:
        raise DatabaseSchemaError(
            f"database schema version {version} is unsupported; "
            f"this binary requires {DATABASE_SCHEMA_VERSION}"
        )
    _validate_schema_shape(conn, require_guards=True)


def ensure_mcp_approval_tables(conn: Any) -> None:
    """Create the three MCP approval tables if they do not exist.

    This is the single Core authority for the MCP CREATE statements. The
    Tools compatibility delegate calls this; bare connections and
    store-created schemas produce identical PRAGMA shapes.
    """
    _register_writer_version(conn)
    savepoint = "disco_mcp_schema"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for ddl in _MCP_DDL:
            conn.execute(ddl)
        _create_writer_guards(
            conn,
            None,
            tables=("mcp_approvals", "mcp_approval_pending", "mcp_config_approvals"),
        )
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    conn.execute(f"RELEASE {savepoint}")
    conn.commit()


def mcp_approval_ddl() -> tuple[str, str, str]:
    """Return the three MCP approval CREATE statements (for static proof)."""
    return _MCP_DDL


__all__ = [
    "DATABASE_SCHEMA_VERSION",
    "DatabaseSchemaError",
    "MIGRATION_STEP_NAMES",
    "apply_schema",
    "ensure_mcp_approval_tables",
    "mcp_approval_ddl",
    "validate_schema",
]
