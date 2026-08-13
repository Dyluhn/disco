"""R-DOWNGRADE old/new SQLite reader-writer controls."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from disco.core.store.schema import DATABASE_SCHEMA_VERSION, DatabaseSchemaError, apply_schema

_WRITER_FUNCTION = "disco_schema_writer_version"


def _version_one_fixture(path: Path) -> None:
    """Create the exact pre-guard schema shape with one committed row."""
    connection = sqlite3.connect(path)
    apply_schema(connection)
    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall()
    for (name,) in triggers:
        connection.execute(f'DROP TRIGGER "{name}"')
    connection.execute("UPDATE schema_meta SET value = 1 WHERE key = 'schema_version'")
    connection.execute(
        "INSERT INTO conversations "
        "(conversation_id, owner_id, title, created_at) "
        "VALUES ('from-v1', 'local', 'preserved', '2026-08-12')"
    )
    connection.commit()
    connection.close()


def test_version_one_reader_to_current_writer_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    _version_one_fixture(path)

    current = sqlite3.connect(path)
    assert apply_schema(current) == DATABASE_SCHEMA_VERSION
    assert current.execute(
        "SELECT title FROM conversations WHERE conversation_id = 'from-v1'"
    ).fetchone() == ("preserved",)
    current.execute(
        "INSERT INTO conversations "
        "(conversation_id, owner_id, title, created_at) "
        "VALUES ('from-v2', 'local', 'new', '2026-08-12')"
    )
    current.commit()
    current.close()

    old_reader = sqlite3.connect(path)
    assert old_reader.execute(
        "SELECT conversation_id, title FROM conversations ORDER BY conversation_id"
    ).fetchall() == [("from-v1", "preserved"), ("from-v2", "new")]
    old_reader.close()


def test_current_database_blocks_unversioned_old_writer_before_change(tmp_path: Path) -> None:
    path = tmp_path / "old-writer.db"
    current = sqlite3.connect(path)
    apply_schema(current)
    current.close()

    old_writer = sqlite3.connect(path)
    old_writer.execute("BEGIN")
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        old_writer.execute(
            "INSERT INTO conversations "
            "(conversation_id, owner_id, title, created_at) "
            "VALUES ('unsafe', 'local', 'must-not-land', '2026-08-12')"
        )
    old_writer.rollback()
    assert old_writer.execute(
        "SELECT COUNT(*) FROM conversations WHERE conversation_id = 'unsafe'"
    ).fetchone() == (0,)
    old_writer.close()


@pytest.mark.parametrize("writer_version", [None, 1, DATABASE_SCHEMA_VERSION + 1])
def test_current_database_blocks_noncurrent_explicit_writer(
    tmp_path: Path, writer_version: int | None
) -> None:
    path = tmp_path / "known-old-writer.db"
    current = sqlite3.connect(path)
    apply_schema(current)
    current.close()

    old_writer = sqlite3.connect(path)
    old_writer.create_function(_WRITER_FUNCTION, 0, lambda: writer_version, deterministic=True)
    with pytest.raises(sqlite3.IntegrityError, match="unsupported database schema writer"):
        old_writer.execute(
            "INSERT INTO conversations "
            "(conversation_id, owner_id, title, created_at) "
            "VALUES ('unsafe', 'local', 'must-not-land', '2026-08-12')"
        )
    assert old_writer.execute("SELECT COUNT(*) FROM conversations").fetchone() == (0,)
    old_writer.close()


@pytest.mark.parametrize("writer_version", [None, 1, DATABASE_SCHEMA_VERSION + 1])
def test_current_database_blocks_noncurrent_schema_version_rewrite(
    tmp_path: Path, writer_version: int | None
) -> None:
    path = tmp_path / "schema-writer.db"
    current = sqlite3.connect(path)
    apply_schema(current)
    current.close()

    old_writer = sqlite3.connect(path)
    old_writer.create_function(_WRITER_FUNCTION, 0, lambda: writer_version, deterministic=True)
    with pytest.raises(sqlite3.IntegrityError, match="unsupported database schema writer"):
        old_writer.execute("UPDATE schema_meta SET value = 1 WHERE key = 'schema_version'")
    assert old_writer.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone() == (DATABASE_SCHEMA_VERSION,)
    old_writer.close()


def test_current_schema_rejects_expected_name_with_noop_guard(tmp_path: Path) -> None:
    path = tmp_path / "no-op-guard.db"
    current = sqlite3.connect(path)
    apply_schema(current)
    current.execute("DROP TRIGGER schema_writer_guard_conversations_insert")
    current.execute(
        "CREATE TRIGGER schema_writer_guard_conversations_insert "
        "BEFORE INSERT ON conversations BEGIN SELECT 1; END"
    )
    current.commit()

    with pytest.raises(DatabaseSchemaError, match="malformed writer guards"):
        apply_schema(current)
    current.close()


def test_version_one_migration_rejects_noop_guard_without_partial_promotion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-no-op-guard.db"
    _version_one_fixture(path)
    poisoned = sqlite3.connect(path)
    poisoned.execute(
        "CREATE TRIGGER schema_writer_guard_conversations_insert "
        "BEFORE INSERT ON conversations BEGIN SELECT 1; END"
    )
    poisoned.commit()

    with pytest.raises(DatabaseSchemaError, match="malformed writer guards"):
        apply_schema(poisoned)

    assert poisoned.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone() == (1,)
    names = {
        str(row[0])
        for row in poisoned.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    assert names == {"schema_writer_guard_conversations_insert"}
    poisoned.close()


def test_interrupted_one_to_two_guard_migration_is_atomic_and_retryable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interrupted.db"
    _version_one_fixture(path)
    before = sqlite3.connect(path)
    before_dump = "\n".join(before.iterdump())
    before.close()

    def interrupt(step: str) -> None:
        if step == "guard:events:insert":
            raise RuntimeError("injected guard migration interruption")

    interrupted = sqlite3.connect(path)
    with pytest.raises(RuntimeError, match="injected guard migration interruption"):
        apply_schema(interrupted, after_step=interrupt)
    interrupted.close()

    unchanged = sqlite3.connect(path)
    assert "\n".join(unchanged.iterdump()) == before_dump
    assert unchanged.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone() == (1,)
    unchanged.close()

    resumed = sqlite3.connect(path)
    assert apply_schema(resumed) == DATABASE_SCHEMA_VERSION
    resumed.close()


def test_missing_version_row_migrates_without_partial_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "missing-version.db"
    _version_one_fixture(path)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM schema_meta WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    migrated = sqlite3.connect(path)
    assert apply_schema(migrated) == DATABASE_SCHEMA_VERSION
    assert migrated.execute(
        "SELECT title FROM conversations WHERE conversation_id = 'from-v1'"
    ).fetchone() == ("preserved",)
    migrated.close()
