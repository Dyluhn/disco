"""Self-host data lifecycle: WAL-safe archives, safe restore, and uninstall posture."""

from __future__ import annotations

import io
import json
import sqlite3
import stat
import subprocess
import tarfile
from contextlib import closing
from pathlib import Path

import pytest

from scripts import self_host_data as lifecycle


def _data_tree(root: Path) -> sqlite3.Connection:
    root.mkdir()
    connection = sqlite3.connect(root / "disco.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
    connection.execute("INSERT INTO projects VALUES ('project-1', 'Restored project')")
    connection.commit()
    (root / ".secret_key").write_text("test-master-key\n", encoding="utf-8")
    (root / "disco-config.json").write_text('{"default_model":"test"}\n', encoding="utf-8")
    (root / "secrets.json").write_text('{"provider":"ciphertext"}\n', encoding="utf-8")
    (root / "disco-approved-origins.json").write_text('{"approvals":[]}\n', encoding="utf-8")
    (root / "projects" / "app").mkdir(parents=True)
    (root / "projects" / "app" / "index.html").write_text("restored preview", encoding="utf-8")
    (root / "projects" / "app" / "private").mkdir()
    (root / "projects" / "app" / "private").chmod(0o700)
    (root / "projects" / "app" / "private" / "state.json").write_text("{}\n")
    (root / "skills").mkdir()
    (root / "skills" / "review.md").write_text("# Review\n", encoding="utf-8")
    (root / "cache" / "tts" / "conv-1").mkdir(parents=True)
    (root / "cache" / "tts" / "conv-1" / "overview.mp3").write_bytes(b"ID3test-audio")
    (root / "project-link").symlink_to("projects/app", target_is_directory=True)
    return connection


def test_backup_uses_sqlite_api_and_restores_complete_data_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    writer = _data_tree(source)
    assert (source / "disco.db-wal").is_file()

    archive = io.BytesIO()
    manifest = lifecycle.create_archive(source, archive)
    paths = {row["path"] for row in manifest["entries"]}
    assert {
        ".secret_key",
        "disco.db",
        "disco-config.json",
        "secrets.json",
        "disco-approved-origins.json",
        "projects/app/index.html",
        "skills/review.md",
        "cache/tts/conv-1/overview.mp3",
        "project-link",
    } <= paths
    assert "disco.db-wal" not in paths
    assert "disco.db-shm" not in paths

    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:gz") as backup:
        database_member = backup.extractfile(f"{lifecycle._DATA_PREFIX}disco.db")
        assert database_member is not None
        database_bytes = database_member.read()
    assert database_bytes[18:20] == b"\x01\x01"
    with closing(sqlite3.connect(":memory:")) as database:
        database.deserialize(database_bytes)
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("SELECT id, title FROM projects").fetchall() == [
            ("project-1", "Restored project")
        ]

    restored = tmp_path / "restored"
    archive.seek(0)
    lifecycle.restore_archive(archive, restored)
    assert not (restored / "disco.db-wal").exists()
    with closing(sqlite3.connect(restored / "disco.db")) as database:
        assert database.execute("SELECT id, title FROM projects").fetchall() == [
            ("project-1", "Restored project")
        ]
    assert (restored / "projects" / "app" / "index.html").read_text() == "restored preview"
    assert stat.S_IMODE((restored / "projects" / "app" / "private").stat().st_mode) == 0o700
    assert (restored / "skills" / "review.md").is_file()
    assert (restored / "cache" / "tts" / "conv-1" / "overview.mp3").read_bytes().startswith(b"ID3")
    assert (restored / "project-link").is_symlink()
    writer.close()


def test_restore_refuses_nonempty_target_without_changing_it(tmp_path: Path) -> None:
    source = tmp_path / "source"
    writer = _data_tree(source)
    archive = io.BytesIO()
    lifecycle.create_archive(source, archive)
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    archive.seek(0)
    with pytest.raises(lifecycle.DataLifecycleError, match="not empty"):
        lifecycle.restore_archive(archive, target)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    writer.close()


def test_restore_rejects_traversal_before_writing_target(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        manifest = json.dumps({"format": 1, "entries": []}).encode()
        info = tarfile.TarInfo(lifecycle._MANIFEST_NAME)
        info.size = len(manifest)
        output.addfile(info, io.BytesIO(manifest))
        escape = tarfile.TarInfo(f"{lifecycle._DATA_PREFIX}../../escape")
        escape.size = 1
        output.addfile(escape, io.BytesIO(b"x"))
    target = tmp_path / "target"
    archive.seek(0)

    with pytest.raises(lifecycle.DataLifecycleError, match="escapes restore root"):
        lifecycle.restore_archive(archive, target)
    assert list(target.iterdir()) == []
    assert not (tmp_path / "escape").exists()


def test_restore_rejects_checksum_drift_without_partial_promotion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    writer = _data_tree(source)
    original = io.BytesIO()
    lifecycle.create_archive(source, original)
    original.seek(0)
    tampered = io.BytesIO()
    with (
        tarfile.open(fileobj=original, mode="r:gz") as input_archive,
        tarfile.open(fileobj=tampered, mode="w:gz") as output_archive,
    ):
        for member in input_archive:
            payload = input_archive.extractfile(member) if member.isfile() else None
            if member.name.endswith("/disco-config.json"):
                replacement = b'{"tampered":true}\n'
                member.size = len(replacement)
                output_archive.addfile(member, io.BytesIO(replacement))
            else:
                output_archive.addfile(member, payload)
    target = tmp_path / "target"
    tampered.seek(0)

    with pytest.raises(lifecycle.DataLifecycleError, match="checksums or metadata"):
        lifecycle.restore_archive(tampered, target)
    assert list(target.iterdir()) == []
    writer.close()


def test_backup_refuses_symlink_that_escapes_data_volume(tmp_path: Path) -> None:
    source = tmp_path / "source"
    writer = _data_tree(source)
    (source / "escape").symlink_to("../outside")

    with pytest.raises(lifecycle.DataLifecycleError, match="symlink escapes"):
        lifecycle.create_archive(source, io.BytesIO())
    writer.close()


class _FakeCompose:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def volume_name(self) -> str:
        return "campaign_disco-data"

    def run(self, *arguments: str) -> None:
        self.calls.append(arguments)


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_compose_resource_discovery_uses_native_project_labels(monkeypatch, engine) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1:3] == ["volume", "ls"]:
            stdout = b"campaign_disco-data\n"
        elif command[-1] == "label=com.docker.compose.service=app-server":
            stdout = b"aaaaaaaaaaaa\n"
        else:
            stdout = b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    compose = lifecycle._Compose(engine, Path("/repo/compose.yaml"), "campaign")

    assert compose.volume_name() == "campaign_disco-data"
    assert compose.running_data_services() == ("app-server",)
    assert compose.frontend_running() is False
    assert all(command[0] == engine and command[1] != "compose" for command in calls)
    assert calls[0] == [
        engine,
        "volume",
        "ls",
        "--quiet",
        "--filter",
        "label=com.docker.compose.project=campaign",
    ]


def test_compose_volume_discovery_fails_closed_on_wrong_or_ambiguous_ownership(
    monkeypatch,
) -> None:
    compose = lifecycle._Compose("podman", Path("/repo/compose.yaml"), "campaign")
    monkeypatch.setattr(compose, "_engine_text", lambda *_args: "campaign_other-data\n")
    with pytest.raises(lifecycle.DataLifecycleError, match="owns unexpected volume"):
        compose.volume_name()

    monkeypatch.setattr(
        compose,
        "_engine_text",
        lambda *_args: "campaign_disco-data\ncampaign_other-data\n",
    )

    with pytest.raises(lifecycle.DataLifecycleError, match="2 data-volume candidates"):
        compose.volume_name()


def test_compose_missing_volume_distinguishes_new_from_unowned(monkeypatch) -> None:
    compose = lifecycle._Compose("podman", Path("/repo/compose.yaml"), "campaign")
    monkeypatch.setattr(compose, "_engine_text", lambda *_args: "")
    monkeypatch.setattr(lifecycle, "_volume_exists", lambda *_args: False)
    assert compose.volume_name(required=False) is None

    monkeypatch.setattr(lifecycle, "_volume_exists", lambda *_args: True)
    with pytest.raises(lifecycle.DataLifecycleError, match="without Compose project ownership"):
        compose.volume_name(required=False)


def test_container_helper_executes_the_invoking_controller_not_the_service_image(
    monkeypatch,
) -> None:
    compose = lifecycle._Compose("podman", Path("/repo/compose.yaml"), "campaign")
    calls = []
    monkeypatch.setattr(
        compose,
        "run",
        lambda *arguments, **options: calls.append((arguments, options)),
    )
    stdin = object()
    stdout = object()

    lifecycle._container_helper(
        compose,
        "_archive-restore",
        stdin=stdin,
        stdout=stdout,
    )

    assert len(calls) == 1
    arguments, options = calls[0]
    assert arguments[:8] == (
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "app-server",
        "-c",
    )
    assert arguments[8] == Path(lifecycle.__file__).read_text(encoding="utf-8")
    assert arguments[9:] == ("_archive-restore", "--data-dir", "/data")
    assert options == {"stdin": stdin, "stdout": stdout}


def test_compose_running_service_discovery_rejects_multiple_owners(monkeypatch) -> None:
    compose = lifecycle._Compose("podman", Path("/repo/compose.yaml"), "campaign")
    monkeypatch.setattr(compose, "_engine_text", lambda *_args: "a" * 12 + "\n" + "b" * 12)

    with pytest.raises(lifecycle.DataLifecycleError, match="2 running containers"):
        compose.running_data_services()


def test_snapshot_coordinates_frontend_after_both_writers() -> None:
    class _SnapshotCompose:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def running_data_services(self) -> tuple[str, ...]:
            return ("app-server", "agent-server")

        def frontend_running(self) -> bool:
            return True

        def run(self, *arguments: str) -> None:
            self.calls.append(arguments)

    compose = _SnapshotCompose()
    running = lifecycle._stop_for_snapshot(compose)
    lifecycle._restart_snapshot_services(compose, running)

    assert compose.calls == [
        ("stop", "frontend"),
        ("stop", "app-server", "agent-server"),
        ("start", "app-server", "agent-server"),
        ("up", "-d", "frontend"),
    ]


def test_snapshot_preserves_an_intentionally_stopped_frontend() -> None:
    class _SnapshotCompose:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def running_data_services(self) -> tuple[str, ...]:
            return ("app-server", "agent-server")

        def frontend_running(self) -> bool:
            return False

        def run(self, *arguments: str) -> None:
            self.calls.append(arguments)

    compose = _SnapshotCompose()
    running = lifecycle._stop_for_snapshot(compose)
    lifecycle._restart_snapshot_services(compose, running)

    assert compose.calls == [
        ("stop", "app-server", "agent-server"),
        ("start", "app-server", "agent-server"),
    ]


def test_snapshot_preserves_a_partial_writer_state() -> None:
    class _SnapshotCompose:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def running_data_services(self) -> tuple[str, ...]:
            return ("app-server",)

        def frontend_running(self) -> bool:
            return True

        def run(self, *arguments: str) -> None:
            self.calls.append(arguments)

    compose = _SnapshotCompose()
    running = lifecycle._stop_for_snapshot(compose)
    lifecycle._restart_snapshot_services(compose, running)

    assert compose.calls == [
        ("stop", "frontend"),
        ("stop", "app-server"),
        ("start", "app-server"),
        ("start", "frontend"),
    ]


def test_restore_lets_compose_create_and_then_binds_a_missing_volume(
    tmp_path: Path, monkeypatch
) -> None:
    class _RestoreCompose:
        def __init__(self) -> None:
            self.volume_names = iter((None, "campaign_disco-data"))
            self.calls: list[tuple[str, ...]] = []

        def volume_name(self, *, required: bool = True) -> str | None:
            value = next(self.volume_names)
            assert not required or value is not None
            return value

        def running_data_services(self) -> tuple[str, ...]:
            return ()

        def frontend_running(self) -> bool:
            return False

        def run(self, *arguments: str) -> None:
            self.calls.append(arguments)

    compose = _RestoreCompose()
    helpers: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "_container_helper",
        lambda _compose, command, **_kwargs: helpers.append(command),
    )
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"backup")

    lifecycle._host_restore(compose, archive)

    assert helpers == ["_archive-restore"]
    assert compose.calls == [("up", "-d", "app-server", "agent-server", "frontend")]


def test_upgrade_removes_the_old_containers_before_starting_the_new_images() -> None:
    """Found on a fresh Ubuntu 24.04 install (2026-09-09): podman-compose 1.0.6
    rebuilds on `up --build` but cannot replace a running container ("the
    container name disco_frontend_1 is already in use", exit 125), then restarts
    the old one — the operator keeps running the old code. `down` first, and
    never with --volumes, which would delete disco-data."""
    compose = _FakeCompose()

    lifecycle._host_upgrade(compose)

    assert compose.calls == [
        ("build", "--pull", "app-server", "frontend", "sandbox-image"),
        ("down", "--remove-orphans"),
        ("up", "-d"),
    ]


def test_uninstall_retains_data_unless_exact_confirmation_is_present(monkeypatch) -> None:
    compose = _FakeCompose()
    monkeypatch.setattr(lifecycle, "_volume_exists", lambda *_args: True)

    lifecycle._host_uninstall(compose, "podman", destroy_data=False, confirmation="")
    assert compose.calls == [("down", "--remove-orphans")]

    with pytest.raises(lifecycle.DataLifecycleError, match="requires --confirm"):
        lifecycle._host_uninstall(compose, "podman", destroy_data=True, confirmation="DELETE")
    assert compose.calls == [("down", "--remove-orphans")]
