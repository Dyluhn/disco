"""Backup archive and logical-data verification for fresh-device certification."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import sqlite3
import stat
import tarfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .fresh_device_host import ProductError

BACKUP_MANIFEST_NAME = "disco-backup-v1/manifest.json"
BACKUP_DATA_PREFIX = "disco-backup-v1/data/"

_RPO_DATABASE_EXCLUSIONS = frozenset(
    {
        "schema_meta",
        "preview_redemptions",
        "local_preview_leases",
        "local_preview_origin_state",
    }
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_backup_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ProductError("backup manifest contains an invalid entry path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or value == "." or ".." in path.parts:
        raise ProductError("backup manifest contains a non-canonical entry path")
    return value


def _validate_backup_symlink(path: str, target: Any) -> str:
    if not isinstance(target, str) or not target or PurePosixPath(target).is_absolute():
        raise ProductError("backup manifest contains an unsafe symlink target")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == ".." or resolved.startswith("../"):
        raise ProductError("backup manifest symlink escapes the data root")
    return target


def _manifest_entries(manifest: Any) -> list[Any]:
    if not isinstance(manifest, dict):
        raise ProductError("backup manifest has the wrong shape")
    if manifest.get("format") != 1 or not isinstance(manifest.get("entries"), list):
        raise ProductError("backup manifest has the wrong shape")
    database = manifest.get("database")
    if not isinstance(database, dict):
        raise ProductError("backup manifest has invalid database metadata")
    if database.get("path") != "disco.db" or database.get("method") != "sqlite3_backup":
        raise ProductError("backup manifest has invalid database metadata")
    return manifest["entries"]


def _validated_mode(raw: dict[Any, Any]) -> tuple[Any, int]:
    kind = raw.get("type")
    if kind not in {"file", "directory", "symlink"}:
        raise ProductError("backup manifest contains invalid entry metadata")
    mode = raw.get("mode")
    if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o7777:
        raise ProductError("backup manifest contains invalid entry metadata")
    return kind, mode


def _validate_file_evidence(raw: dict[Any, Any]) -> None:
    size = raw.get("size")
    digest = raw.get("sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ProductError("backup manifest contains invalid file evidence")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ProductError("backup manifest contains invalid file evidence")


def _validated_backup_entry(raw: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ProductError("backup manifest contains a malformed entry")
    path = _canonical_backup_path(raw.get("path"))
    kind, _mode = _validated_mode(raw)
    expected_keys = {"path", "type", "mode"}
    if kind == "file":
        expected_keys |= {"size", "sha256"}
        _validate_file_evidence(raw)
    elif kind == "symlink":
        expected_keys.add("target")
        _validate_backup_symlink(path, raw.get("target"))
    if set(raw) != expected_keys:
        raise ProductError("backup manifest entry has an invalid shape")
    return path, dict(raw)


def _backup_entry_rows(manifest: Any) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    ordered_paths: list[str] = []
    for raw in _manifest_entries(manifest):
        path, row = _validated_backup_entry(raw)
        if path in rows:
            raise ProductError("backup manifest repeats an entry path")
        rows[path] = row
        ordered_paths.append(path)
    if ordered_paths != sorted(ordered_paths):
        raise ProductError("backup manifest entries are not canonically ordered")
    database_row = rows.get("disco.db")
    if database_row is None or database_row.get("type") != "file":
        raise ProductError("backup manifest has no SQLite database entry")
    return rows


def _canonical_archive_member_name(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or value != path.as_posix() or value == ".":
        raise ProductError("backup archive contains an unsafe member path")
    if ".." in path.parts:
        raise ProductError("backup archive contains an unsafe member path")
    return value


def _archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        name = _canonical_archive_member_name(member.name)
        if name in members:
            raise ProductError("backup archive contains a duplicate member")
        if not (member.isfile() or member.isdir() or member.issym()):
            raise ProductError("backup archive contains an unsafe member type")
        members[name] = member
    return members


def _archive_manifest(
    archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo]
) -> dict[str, Any]:
    member = members.get(BACKUP_MANIFEST_NAME)
    if member is None or not member.isfile() or stat.S_IMODE(member.mode) != 0o600:
        raise ProductError("backup archive has no valid manifest member")
    manifest_file = archive.extractfile(member)
    if manifest_file is None:
        raise ProductError("backup manifest is not a regular file")
    raw = json.loads(manifest_file.read().decode("utf-8"))
    if not isinstance(raw, dict):
        raise ProductError("backup manifest has the wrong shape")
    return raw


def _read_file_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, *, retain: bool
) -> tuple[int, str, bytes | None]:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ProductError("backup file member cannot be read")
    digest = hashlib.sha256()
    size = 0
    parts: list[bytes] | None = [] if retain else None
    while chunk := extracted.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
        if parts is not None:
            parts.append(chunk)
    return size, digest.hexdigest(), None if parts is None else b"".join(parts)


def _verify_file_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    row: dict[str, Any],
    *,
    retain: bool,
) -> bytes | None:
    if not member.isfile():
        raise ProductError("backup entry type does not match the manifest")
    size, digest, retained = _read_file_member(archive, member, retain=retain)
    if size != row["size"] or member.size != row["size"] or digest != row["sha256"]:
        raise ProductError("backup entry size or checksum does not match the manifest")
    return retained


def _verify_entry(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    row: dict[str, Any],
    entry_path: str,
) -> bytes | None:
    if stat.S_IMODE(member.mode) != row["mode"]:
        raise ProductError("backup entry mode does not match the manifest")
    if row["type"] == "file":
        return _verify_file_member(
            archive,
            member,
            row,
            retain=entry_path == "disco.db",
        )
    if row["type"] == "directory":
        if not member.isdir() or member.size != 0:
            raise ProductError("backup entry type does not match the manifest")
        return None
    if not member.issym() or member.size != 0 or member.linkname != row["target"]:
        raise ProductError("backup symlink does not match the manifest")
    _validate_backup_symlink(entry_path, member.linkname)
    return None


def _verified_archive_contents(
    archive: tarfile.TarFile,
    *,
    entry_rows: Callable[[Any], dict[str, dict[str, Any]]],
) -> tuple[dict[str, Any], bytes | None]:
    members = _archive_members(archive)
    manifest = _archive_manifest(archive, members)
    rows = entry_rows(manifest)
    expected = {BACKUP_MANIFEST_NAME} | {f"{BACKUP_DATA_PREFIX}{path}" for path in rows}
    if set(members) != expected:
        raise ProductError("backup archive members do not match the manifest")
    database_bytes: bytes | None = None
    for entry_path, row in rows.items():
        retained = _verify_entry(
            archive,
            members[f"{BACKUP_DATA_PREFIX}{entry_path}"],
            row,
            entry_path,
        )
        if retained is not None:
            database_bytes = retained
    return manifest, database_bytes


def _verified_backup_archive(
    path: Path,
    *,
    entry_rows: Callable[[Any], dict[str, dict[str, Any]]] = _backup_entry_rows,
) -> tuple[dict[str, Any], bytes]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            manifest, database_bytes = _verified_archive_contents(
                archive,
                entry_rows=entry_rows,
            )
    except ProductError:
        raise
    except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductError(f"backup archive has no readable manifest: {exc}") from exc
    if database_bytes is None:
        raise ProductError("backup archive has no readable SQLite database")
    return manifest, database_bytes


def _sqlite_value(value: Any) -> list[str]:
    if value is None:
        return ["null", ""]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    raise ProductError(f"SQLite backup produced an unsupported value type: {type(value).__name__}")


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_logical_digests(database_bytes: bytes) -> tuple[str, str]:
    """Return stable committed-data and schema digests for a SQLite backup."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(database_bytes)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise ProductError(f"backup SQLite integrity check failed: {integrity!r}")
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        schema_digest = _sha256_bytes(
            json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":")).encode()
        )
        tables = _committed_tables(connection)
        committed = [_committed_table(connection, table) for table in tables]
        committed_digest = _sha256_bytes(
            json.dumps(committed, ensure_ascii=False, separators=(",", ":")).encode()
        )
        return committed_digest, schema_digest
    except sqlite3.DatabaseError as exc:
        raise ProductError(f"backup database is not readable SQLite: {exc}") from exc
    finally:
        connection.close()


def _committed_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in _RPO_DATABASE_EXCLUSIONS
    ]


def _committed_table(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    quoted = _quoted_identifier(table)
    columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")]
    row_digests = sorted(
        _sha256_bytes(
            json.dumps(
                [_sqlite_value(value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        for row in connection.execute(f"SELECT * FROM {quoted}")
    )
    return {"table": table, "columns": columns, "rows": row_digests}


def _manifest_digest(manifest: dict[str, Any], *, include_database: bool) -> str:
    """Digest committed backup rows and logical SQLite state."""
    entries = manifest["entries"]
    if not isinstance(entries, list):
        raise ProductError("backup manifest entries are not a list")
    selected = [
        row for row in entries if not isinstance(row, dict) or row.get("path") != "disco.db"
    ]
    database = manifest.get("database")
    if not isinstance(database, dict) or not isinstance(database.get("committed_sha256"), str):
        raise ProductError("backup manifest lacks the logical committed-data digest")
    logical_database = {"committed_sha256": database["committed_sha256"]}
    if include_database:
        schema_digest = database.get("schema_sha256")
        if not isinstance(schema_digest, str):
            raise ProductError("backup manifest lacks the logical schema digest")
        logical_database["schema_sha256"] = schema_digest
    payload = {
        "format": manifest.get("format"),
        "database": logical_database,
        "scope": manifest.get("scope"),
        "entries": selected,
    }
    return _sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
