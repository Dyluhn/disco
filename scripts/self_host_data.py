#!/usr/bin/env python3
"""Backup, restore, upgrade, and uninstall the Compose self-host data volume."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

_ARCHIVE_ROOT = "disco-backup-v1"
_MANIFEST_NAME = f"{_ARCHIVE_ROOT}/manifest.json"
_DATA_PREFIX = f"{_ARCHIVE_ROOT}/data/"
_DB_NAME = "disco.db"
_DB_SIDECARS = frozenset({f"{_DB_NAME}-wal", f"{_DB_NAME}-shm"})
_DESTRUCTIVE_CONFIRMATION = "DELETE_DISCO_DATA"


class DataLifecycleError(RuntimeError):
    """A safe lifecycle precondition or archive validation failed."""


@dataclass(frozen=True)
class _ArchiveItem:
    path: str
    kind: str
    source: Path
    mode: int
    size: int = 0
    sha256: str = ""
    target: str = ""

    def manifest_row(self) -> dict[str, object]:
        row: dict[str, object] = {
            "path": self.path,
            "type": self.kind,
            "mode": self.mode,
        }
        if self.kind == "file":
            row.update({"size": self.size, "sha256": self.sha256})
        elif self.kind == "symlink":
            row["target"] = self.target
        return row


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_symlink(path: Path, data_dir: Path) -> str:
    target = os.readlink(path)
    if os.path.isabs(target):
        raise DataLifecycleError(f"absolute symlink is not portable: {path}")
    resolved = (path.parent / target).resolve(strict=False)
    try:
        resolved.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise DataLifecycleError(f"symlink escapes the data volume: {path}") from exc
    return target


def _item(
    path: Path, relative: str, data_dir: Path, *, mode_from: Path | None = None
) -> _ArchiveItem:
    metadata = (mode_from or path).lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        return _ArchiveItem(
            path=relative,
            kind="symlink",
            source=path,
            mode=mode,
            target=_safe_relative_symlink(path, data_dir),
        )
    if path.is_dir():
        return _ArchiveItem(path=relative, kind="directory", source=path, mode=mode)
    if path.is_file():
        return _ArchiveItem(
            path=relative,
            kind="file",
            source=path,
            mode=mode,
            size=path.stat().st_size,
            sha256=_sha256(path),
        )
    raise DataLifecycleError(f"special filesystem entry is not backup-safe: {path}")


def _scan_items(
    data_dir: Path,
    *,
    database_copy: Path | None = None,
    staging_dir: Path | None = None,
) -> list[_ArchiveItem]:
    items: list[_ArchiveItem] = []
    for path in sorted(data_dir.rglob("*"), key=lambda item: item.as_posix()):
        if staging_dir is not None and (path == staging_dir or staging_dir in path.parents):
            continue
        relative = path.relative_to(data_dir).as_posix()
        if relative in _DB_SIDECARS:
            continue
        if relative == _DB_NAME and database_copy is not None:
            continue
        items.append(_item(path, relative, data_dir))
    database = data_dir / _DB_NAME
    if database_copy is not None:
        if not database.is_file():
            raise DataLifecycleError(f"missing required SQLite database: {database}")
        items.append(_item(database_copy, _DB_NAME, data_dir, mode_from=database))
    items.sort(key=lambda item: item.path)
    return items


def _sqlite_backup(source: Path, destination: Path) -> None:
    try:
        source_uri = f"file:{source}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
        with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise DataLifecycleError(f"could not create SQLite backup for {source}: {exc}") from exc
    if result != ("ok",):
        raise DataLifecycleError(f"SQLite backup integrity check failed: {result!r}")


def _manifest(items: Sequence[_ArchiveItem]) -> dict[str, object]:
    return {
        "format": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "database": {"path": _DB_NAME, "method": "sqlite3_backup"},
        "entries": [item.manifest_row() for item in items],
        "scope": "complete Compose /data volume",
    }


def create_archive(data_dir: Path, output: BinaryIO) -> dict[str, object]:
    """Write a consistent, checksummed archive of ``data_dir`` to ``output``."""

    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise DataLifecycleError(f"data directory does not exist: {data_dir}")
    staging = Path(tempfile.mkdtemp(prefix=".disco-backup-", dir=data_dir))
    try:
        database_copy = staging / _DB_NAME
        _sqlite_backup(data_dir / _DB_NAME, database_copy)
        items = _scan_items(data_dir, database_copy=database_copy, staging_dir=staging)
        manifest = _manifest(items)
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        with tarfile.open(fileobj=output, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
            manifest_info = tarfile.TarInfo(_MANIFEST_NAME)
            manifest_info.size = len(manifest_bytes)
            manifest_info.mode = 0o600
            manifest_info.mtime = int(datetime.now(UTC).timestamp())
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
            for item in items:
                arcname = f"{_DATA_PREFIX}{item.path}"
                info = archive.gettarinfo(str(item.source), arcname=arcname)
                info.mode = item.mode
                if item.kind == "file":
                    with item.source.open("rb") as handle:
                        archive.addfile(info, handle)
                else:
                    archive.addfile(info)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _safe_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise DataLifecycleError(f"archive member escapes restore root: {member.name!r}")
    if member.islnk() or member.isdev() or member.isfifo():
        raise DataLifecycleError(f"archive member type is not restore-safe: {member.name!r}")
    if member.issym():
        target = member.linkname
        if os.path.isabs(target):
            raise DataLifecycleError(
                f"absolute archive symlink is not restore-safe: {member.name!r}"
            )
        parent = posixpath.dirname(member.name)
        normalized = posixpath.normpath(posixpath.join(parent, target))
        if not normalized.startswith(_DATA_PREFIX):
            raise DataLifecycleError(f"archive symlink escapes restore root: {member.name!r}")


def _data_children(data_dir: Path, *, ignore: frozenset[str] = frozenset()) -> list[Path]:
    return [child for child in data_dir.iterdir() if child.name not in ignore]


def _verified_manifest(raw: bytes) -> dict[str, object]:
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataLifecycleError("backup manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise DataLifecycleError("unsupported backup manifest format")
    if not isinstance(manifest.get("entries"), list):
        raise DataLifecycleError("backup manifest has no entries list")
    return manifest


def _extract_archive_members(
    source: BinaryIO, staging: Path
) -> tuple[bytes, dict[PurePosixPath, int]]:
    """Validate and extract every archive member into ``staging``.

    Returns the raw manifest bytes and the per-directory mode recorded for
    each directory member (streaming extraction does not reliably apply it).
    """
    manifest_raw: bytes | None = None
    seen: set[str] = set()
    directory_modes: dict[PurePosixPath, int] = {}
    with tarfile.open(fileobj=source, mode="r|gz") as archive:
        for member in archive:
            _safe_member(member)
            if member.name in seen:
                raise DataLifecycleError(f"duplicate archive member: {member.name!r}")
            seen.add(member.name)
            if member.name == _MANIFEST_NAME:
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise DataLifecycleError("backup manifest is not a regular file")
                manifest_raw = extracted.read()
                continue
            if not member.name.startswith(_DATA_PREFIX):
                raise DataLifecycleError(f"unexpected archive member: {member.name!r}")
            if member.isdir():
                relative = PurePosixPath(member.name).relative_to(PurePosixPath(_DATA_PREFIX))
                directory_modes[relative] = member.mode
            archive.extract(member, path=staging, filter="data")
    if manifest_raw is None:
        raise DataLifecycleError("backup archive has no manifest")
    return manifest_raw, directory_modes


def _apply_directory_modes(extracted_data: Path, directory_modes: dict[PurePosixPath, int]) -> None:
    # Streaming TarFile.extract() creates existing parent directories with
    # the process umask and does not reliably apply their archived mode.
    # Reapply the safe member metadata before comparing it to the manifest;
    # a tar/manifest disagreement still fails the exact row comparison.
    for relative, mode in sorted(
        directory_modes.items(), key=lambda item: len(item[0].parts), reverse=True
    ):
        (extracted_data / relative).chmod(mode)


def _verify_restored_entries(extracted_data: Path, manifest: dict[str, object]) -> None:
    actual = [item.manifest_row() for item in _scan_items(extracted_data)]
    expected = manifest["entries"]
    if actual != expected:
        raise DataLifecycleError("backup entry checksums or metadata do not match the manifest")


def _verify_restored_database(extracted_data: Path) -> None:
    with sqlite3.connect(f"file:{extracted_data / _DB_NAME}?mode=ro", uri=True) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise DataLifecycleError(f"restored SQLite integrity check failed: {result!r}")
    # A database whose persisted journal mode is WAL may create fresh empty
    # sidecars even for the integrity-check connection. They are not backup
    # data and must not be promoted into the restored volume.
    for sidecar in _DB_SIDECARS:
        (extracted_data / sidecar).unlink(missing_ok=True)


def _promote_restored_data(extracted_data: Path, data_dir: Path) -> None:
    for child in sorted(extracted_data.iterdir(), key=lambda item: item.name):
        child.replace(data_dir / child.name)


def restore_archive(source: BinaryIO, data_dir: Path) -> dict[str, object]:
    """Validate and restore ``source`` into an existing empty data directory."""

    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    if _data_children(data_dir):
        raise DataLifecycleError(f"restore target is not empty: {data_dir}")
    staging = Path(tempfile.mkdtemp(prefix=".disco-restore-", dir=data_dir))
    try:
        manifest_raw, directory_modes = _extract_archive_members(source, staging)
        manifest = _verified_manifest(manifest_raw)
        extracted_data = staging / _ARCHIVE_ROOT / "data"
        if not (extracted_data / _DB_NAME).is_file():
            raise DataLifecycleError("backup archive has no SQLite database")
        _apply_directory_modes(extracted_data, directory_modes)
        _verify_restored_entries(extracted_data, manifest)
        _verify_restored_database(extracted_data)
        _promote_restored_data(extracted_data, data_dir)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class _Compose:
    def __init__(self, engine: str, compose_file: Path, project_name: str) -> None:
        self.engine = engine
        self.command = [engine, "compose", "-f", str(compose_file), "-p", project_name]

    def run(
        self,
        *arguments: str,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess:
        completed = subprocess.run(  # noqa: S603 - fixed engine + typed argv
            [*self.command, *arguments],
            stdin=stdin,
            stdout=subprocess.PIPE if capture else stdout,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise DataLifecycleError(
                f"Compose command failed ({' '.join(arguments)}): {detail or completed.returncode}"
            )
        return completed

    def text(self, *arguments: str) -> str:
        return self.run(*arguments, capture=True).stdout.decode("utf-8", errors="strict")

    def volume_name(self) -> str:
        config = json.loads(self.text("config", "--format", "json"))
        try:
            name = config["volumes"]["disco-data"]["name"]
        except (KeyError, TypeError) as exc:
            raise DataLifecycleError("Compose config has no named disco-data volume") from exc
        if not isinstance(name, str) or not name:
            raise DataLifecycleError("Compose resolved an invalid disco-data volume name")
        return name

    def running_data_services(self) -> tuple[str, ...]:
        running = set(self.text("ps", "--status", "running", "--services").splitlines())
        return tuple(name for name in ("app-server", "agent-server") if name in running)


def _engine(name: str) -> str:
    candidates = ("podman", "docker") if name == "auto" else (name,)
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is None:
            continue
        probe = subprocess.run(  # noqa: S603 - fixed executable/probe argv
            [executable, "compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return executable
    raise DataLifecycleError(f"no working Compose engine found for {name!r}")


def _volume_exists(engine: str, name: str) -> bool:
    return (
        subprocess.run(  # noqa: S603 - fixed engine/inspect argv
            [engine, "volume", "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _container_helper(compose: _Compose, command: str, *, stdin=None, stdout=None) -> None:
    compose.run(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "app-server",
        "/app/scripts/self_host_data.py",
        command,
        "--data-dir",
        "/data",
        stdin=stdin,
        stdout=stdout,
    )


def _stop_for_snapshot(compose: _Compose) -> tuple[str, ...]:
    running = compose.running_data_services()
    if running:
        compose.run("stop", *running)
    return running


def _restart_snapshot_services(compose: _Compose, running: Sequence[str]) -> None:
    if running:
        compose.run("start", *running)


def _host_backup(compose: _Compose, engine: str, output: Path) -> None:
    volume = compose.volume_name()
    if not _volume_exists(engine, volume):
        raise DataLifecycleError(f"data volume does not exist: {volume}")
    output = output.expanduser().resolve()
    if output.exists():
        raise DataLifecycleError(f"refusing to overwrite existing backup: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    running = _stop_for_snapshot(compose)
    try:
        with temporary.open("wb") as handle:
            _container_helper(compose, "_archive-create", stdout=handle)
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
        _restart_snapshot_services(compose, running)
    print(f"Backup created: {output} (source volume: {volume})")


def _host_restore(compose: _Compose, engine: str, archive: Path) -> None:
    archive = archive.expanduser().resolve()
    if not archive.is_file() or archive.is_symlink():
        raise DataLifecycleError(f"backup is not a regular file: {archive}")
    volume = compose.volume_name()
    if not _volume_exists(engine, volume):
        subprocess.run([engine, "volume", "create", volume], check=True)  # noqa: S603
    running = _stop_for_snapshot(compose)
    try:
        with archive.open("rb") as handle:
            _container_helper(compose, "_archive-restore", stdin=handle)
        compose.run("up", "-d", "app-server", "agent-server", "frontend")
    except BaseException:
        _restart_snapshot_services(compose, running)
        raise
    print(f"Restore completed: {archive} -> {volume}; services started")


def _host_uninstall(
    compose: _Compose,
    engine: str,
    *,
    destroy_data: bool,
    confirmation: str,
) -> None:
    volume = compose.volume_name()
    if destroy_data:
        if confirmation != _DESTRUCTIVE_CONFIRMATION:
            raise DataLifecycleError(
                f"destructive uninstall requires --confirm {_DESTRUCTIVE_CONFIRMATION}"
            )
        compose.run("down", "--volumes", "--remove-orphans")
        if _volume_exists(engine, volume):
            raise DataLifecycleError(f"Compose reported success but data volume remains: {volume}")
        print(f"Destructive uninstall removed containers, network, and data volume: {volume}")
        return
    compose.run("down", "--remove-orphans")
    if not _volume_exists(engine, volume):
        raise DataLifecycleError(f"non-destructive uninstall lost its data volume: {volume}")
    print(f"Uninstalled services; retained data volume: {volume}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("auto", "podman", "docker"), default="auto")
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yaml"))
    parser.add_argument("--project-name", default="disco")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="stop writers and create an atomic backup")
    backup.add_argument("--output", required=True, type=Path)

    restore = subparsers.add_parser("restore", help="restore into a new empty data volume")
    restore.add_argument("--archive", required=True, type=Path)

    upgrade = subparsers.add_parser("upgrade", help="backup, rebuild/pull, and restart")
    upgrade.add_argument("--backup", required=True, type=Path)

    uninstall = subparsers.add_parser(
        "uninstall", help="remove services but retain data by default"
    )
    uninstall.add_argument("--destroy-data", action="store_true")
    uninstall.add_argument("--confirm", default="")

    for internal in ("_archive-create", "_archive-restore"):
        command = subparsers.add_parser(internal, help=argparse.SUPPRESS)
        command.add_argument("--data-dir", type=Path, default=Path("/data"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "_archive-create":
        create_archive(args.data_dir, sys.stdout.buffer)
        return 0
    if args.command == "_archive-restore":
        restore_archive(sys.stdin.buffer, args.data_dir)
        return 0

    engine = _engine(args.engine)
    compose_file = args.compose_file.expanduser().resolve()
    if not compose_file.is_file():
        raise DataLifecycleError(f"Compose file does not exist: {compose_file}")
    compose = _Compose(engine, compose_file, args.project_name)
    if args.command == "backup":
        _host_backup(compose, engine, args.output)
    elif args.command == "restore":
        _host_restore(compose, engine, args.archive)
    elif args.command == "upgrade":
        _host_backup(compose, engine, args.backup)
        compose.run("build", "--pull", "app-server", "frontend", "sandbox-image")
        compose.run("up", "-d")
        print("Upgrade completed after backup; inspect logs and run disco-verify --quick")
    elif args.command == "uninstall":
        _host_uninstall(
            compose,
            engine,
            destroy_data=args.destroy_data,
            confirmation=args.confirm,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataLifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
