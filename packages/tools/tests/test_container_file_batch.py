"""Container file-batch guest semantics and bounded protocol."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from disco.tools.sandbox import ProcessSandboxService, SandboxSession
from disco.tools.sandbox._container_parts.file_batch import _FILE_BATCH_GUEST_SCRIPT
from disco.tools.sandbox.file_batch import SandboxFileBatchResult, SandboxFileMutation


def _run_guest(
    tmp_path: Path,
    mutations: list[tuple[str, bytes | None]],
    *,
    commit_last: tuple[str, ...] = (),
) -> tuple[Path, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    stage = tmp_path / "stage"
    payload_dir = stage / "payload"
    payload_dir.mkdir(parents=True)
    rows: list[dict[str, str | None]] = []
    for index, (path, after) in enumerate(mutations):
        payload: str | None = None
        digest: str | None = None
        if after is not None:
            payload = f"payload/{index:06d}"
            digest = hashlib.sha256(after).hexdigest()
            (stage / payload).write_bytes(after)
        rows.append({"path": path, "payload": payload, "after_sha256": digest})
    manifest = json.dumps(
        {"version": 1, "mutations": rows, "commit_last": list(commit_last)},
        separators=(",", ":"),
    ).encode()
    (stage / "manifest.json").write_bytes(manifest)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _FILE_BATCH_GUEST_SCRIPT,
            str(workspace),
            str(stage),
            hashlib.sha256(manifest).hexdigest(),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return workspace, json.loads(completed.stdout)


def test_guest_prevalidates_then_commits_canonical_order_and_marker_last(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"old-a")
    (workspace / "same.txt").write_bytes(b"same")
    (workspace / "stale.txt").write_bytes(b"stale")
    (workspace / "marker.json").write_bytes(b"old-marker")

    workspace, result = _run_guest(
        tmp_path,
        [
            ("marker.json", b"new-marker"),
            ("same.txt", b"same"),
            ("stale.txt", None),
            ("a.txt", b"new-a"),
        ],
        commit_last=("marker.json",),
    )

    assert result["status"] == "ok"
    assert [change["path"] for change in result["changes"]] == [
        "a.txt",
        "stale.txt",
        "marker.json",
    ]
    assert (workspace / "a.txt").read_bytes() == b"new-a"
    assert not (workspace / "stale.txt").exists()
    assert (workspace / "marker.json").read_bytes() == b"new-marker"
    assert (workspace / "same.txt").read_bytes() == b"same"
    assert not (tmp_path / "stage").exists()


def test_guest_refuses_external_symlink_before_any_mutation(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "owned.txt").write_bytes(b"preserve")
    (workspace / "escape").symlink_to(external, target_is_directory=True)

    _workspace, result = _run_guest(
        tmp_path,
        [("safe.txt", b"must-not-land"), ("escape/owned.txt", b"overwrite")],
    )

    assert result["status"] == "failed"
    assert result["failure"]["phase"] == "prevalidation"
    assert result["failure"]["error"] == "BATCH_PATH_RESOLUTION_FAILED"
    assert not (workspace / "safe.txt").exists()
    assert (external / "owned.txt").read_bytes() == b"preserve"


def test_guest_refuses_delete_alias_without_deleting_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_bytes(b"preserve")
    (workspace / "stale.txt").symlink_to("target.txt")

    _workspace, result = _run_guest(tmp_path, [("stale.txt", None)])

    assert result["status"] == "failed"
    assert result["failure"]["error"] == "BATCH_DELETE_ALIAS_REFUSED"
    assert (workspace / "target.txt").read_bytes() == b"preserve"
    assert (workspace / "stale.txt").is_symlink()


def test_guest_reports_exact_prefix_when_later_commit_fails(tmp_path: Path):
    workspace = tmp_path / "workspace"
    locked = workspace / "locked"
    locked.mkdir(parents=True)
    (workspace / "a.txt").write_bytes(b"old-a")
    (locked / "b.txt").write_bytes(b"old-b")
    locked.chmod(0o555)
    try:
        workspace, result = _run_guest(
            tmp_path,
            [("a.txt", b"new-a"), ("locked/b.txt", b"new-b")],
        )
    finally:
        locked.chmod(0o755)

    assert result["status"] == "failed"
    assert result["failure"]["phase"] == "commit"
    assert result["failure"]["failed_path_state"] == "unchanged"
    assert [change["path"] for change in result["changes"]] == ["a.txt"]
    assert (workspace / "a.txt").read_bytes() == b"new-a"
    assert (workspace / "locked/b.txt").read_bytes() == b"old-b"


@pytest.mark.asyncio
async def test_session_forwards_optional_batch_capability_once():
    session = SandboxSession(
        ProcessSandboxService(),
        owner_id="local",
        conversation_id="batch-session-forward",
    )
    instance = await session._ensure()
    calls = []

    async def _batch(mutations, *, commit_last):
        calls.append((mutations, commit_last))
        return SandboxFileBatchResult(changes=())

    instance._commit_file_batch = _batch  # type: ignore[attr-defined]
    try:
        result = await session._commit_file_batch(
            (SandboxFileMutation("a.txt", b"a"),),
            commit_last=("a.txt",),
        )
    finally:
        await session.destroy()

    assert result == SandboxFileBatchResult(changes=())
    assert calls == [((SandboxFileMutation("a.txt", b"a"),), ("a.txt",))]
