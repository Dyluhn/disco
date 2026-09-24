"""Sealed replay batches writes but still checks exact final bytes after staging."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from _preview_manager_fakes import _LifecycleMutationSession
from disco.agent_server.preview_sealed_restore import replace_with_sealed_workspace
from disco.tools.projects.archive import WorkspaceArchiveError
from disco.tools.sandbox.file_batch import (
    SandboxFileBatchFailure,
    SandboxFileBatchResult,
    SandboxFileChange,
)


class BatchSession(_LifecycleMutationSession):
    def __init__(self):
        super().__init__()
        self.batch_calls = 0
        self.reading = 0
        self.max_reading = 0
        self.read_paths = []
        self.failure = None
        self.corrupt_after_restore = False

    async def exec_shell(self, command, **kwargs):
        if command.startswith("find ."):
            self.files.clear()
            return SimpleNamespace(exit_code=0, timed_out=False)
        result = await super().exec_shell(command, **kwargs)
        if self.corrupt_after_restore and command.startswith("mkdir"):
            self.files["web/src/main.ts"] = b"changed during dependency restore"
        return result

    async def _commit_file_batch(self, mutations):
        self.batch_calls += 1
        changes = []
        for mutation in mutations:
            self.files[mutation.path] = mutation.after
            changes.append(
                SandboxFileChange(
                    mutation.path,
                    None,
                    hashlib.sha256(mutation.after).hexdigest(),
                    len(mutation.after),
                )
            )
            if self.failure:
                return SandboxFileBatchResult(tuple(changes), self.failure)
        return SandboxFileBatchResult(tuple(changes))

    async def write_file(self, path, data):
        raise AssertionError("supported batch must not degrade to individual writes")

    async def read_file(self, path):
        self.read_paths.append(path)
        self.reading += 1
        self.max_reading = max(self.max_reading, self.reading)
        try:
            await asyncio.sleep(0.001)
            return self.files[path]
        finally:
            self.reading -= 1


async def test_clear_batch_and_bounded_final_reads_preserve_every_byte():
    session = BatchSession()
    files = tuple((f"file-{index}.bin", bytes(range(256))) for index in range(12))
    await replace_with_sealed_workspace(session, files)
    assert session.batch_calls == 1
    assert session.files == dict(files)
    assert sorted(session.read_paths) == sorted(dict(files))
    assert session.max_reading == 4
    assert session.reading == 0


async def test_dependency_staging_survives_and_final_readback_detects_late_change():
    files = (("web/src/main.ts", b"original"), ("web/package-lock.json", b"lock"))
    session = BatchSession()
    await replace_with_sealed_workspace(
        session,
        files,
        dependency_dir="web/node_modules",
        contract_id="owned",
    )
    assert session.files == {**dict(files), "web/node_modules/vite/package.json": b"installed"}
    session.corrupt_after_restore = True
    with pytest.raises(RuntimeError, match="read-back changed: web/src/main.ts"):
        await replace_with_sealed_workspace(
            session,
            files,
            dependency_dir="web/node_modules",
            contract_id="owned",
        )
    assert session.reading == 0


async def test_partial_batch_failure_is_not_success_or_fallback_and_retry_completes():
    session = BatchSession()
    session.failure = SandboxFileBatchFailure("commit", "broken", "broken", "transport lost")
    files = (("first.txt", b"one"), ("second.txt", b"two"))
    with pytest.raises(WorkspaceArchiveError, match="transport lost"):
        await replace_with_sealed_workspace(session, files)
    assert session.files == {"first.txt": b"one"}
    assert not session.read_paths
    session.failure = None
    await replace_with_sealed_workspace(session, files)
    assert session.files == dict(files)


async def test_missing_final_file_fails_and_drains_other_reads(monkeypatch):
    session = BatchSession()
    original = session._commit_file_batch

    async def drop_file(mutations):
        evidence = await original(mutations)
        del session.files["lost.bin"]
        return evidence

    monkeypatch.setattr(session, "_commit_file_batch", drop_file)
    with pytest.raises(KeyError, match="lost.bin"):
        await replace_with_sealed_workspace(session, (("lost.bin", b"saved"), ("other", b"x")))
    assert session.reading == 0


async def test_cancelled_final_read_drains_readers(monkeypatch):
    session = BatchSession()
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def read(path):
        session.reading += 1
        entered.set()
        try:
            await blocked.wait()
            return session.files[path]
        finally:
            session.reading -= 1

    monkeypatch.setattr(session, "read_file", read)
    pending = asyncio.create_task(
        replace_with_sealed_workspace(session, tuple((str(i), b"saved") for i in range(8)))
    )
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert session.reading == 0
