from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from disco.tools.sandbox import ProcessSandboxService, SandboxSpec
from disco.tools.sandbox._container import (
    bounded_list_argv,
    bounded_list_result,
)


def test_guest_directory_listing_stops_after_the_declared_limit(tmp_path: Path) -> None:
    for name in ("c.txt", "a.txt", "b.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    completed = subprocess.run(
        bounded_list_argv(str(tmp_path), 2),
        check=False,
        capture_output=True,
    )

    entries, truncated = bounded_list_result(
        str(tmp_path),
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )
    assert truncated is True
    assert len(entries) == 2
    assert {name for name, kind in entries if kind == "file"}.issubset({"a.txt", "b.txt", "c.txt"})


def test_bounded_directory_listing_rejects_invalid_limits_and_evidence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="list limit"):
        bounded_list_argv(str(tmp_path), 0)
    with pytest.raises(OSError, match="malformed evidence"):
        bounded_list_result(str(tmp_path), 0, b"not-json", b"")


def test_bounded_directory_listing_failure_preserves_sanitized_type_and_errno(
    tmp_path: Path,
) -> None:
    regular_file = tmp_path / "server.py"
    regular_file.write_text("print('ok')", encoding="utf-8")

    completed = subprocess.run(
        bounded_list_argv(str(regular_file), 256),
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 48
    assert completed.stdout == b""
    assert completed.stderr == b"DISCO_LIST_ERROR:NotADirectoryError:20"
    with pytest.raises(OSError, match="NotADirectoryError") as raised:
        bounded_list_result(
            "server.py",
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
    assert raised.value.errno == 20


@pytest.mark.asyncio
async def test_process_backend_bounded_listing_classifies_without_following_aliases() -> None:
    service = ProcessSandboxService()
    sandbox = await service.create(SandboxSpec(), owner_id="owner", conversation_id="listing")
    try:
        await sandbox.write_file("file.txt", b"text")
        await sandbox.write_file("nested/child.txt", b"text")
        workspace = Path(sandbox.workspace_path or "")
        (workspace / "alias").symlink_to(workspace / "nested", target_is_directory=True)

        entries, truncated = await sandbox.list_dir_bounded(".", 10)

        assert truncated is False
        assert entries == [
            ("alias", "other"),
            ("file.txt", "file"),
            ("nested", "directory"),
        ]
        assert (await sandbox.list_dir_bounded(".", 2))[1] is True
    finally:
        await sandbox.destroy()
