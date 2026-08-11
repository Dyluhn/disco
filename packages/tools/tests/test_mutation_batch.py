"""Target-agnostic deterministic batch commit evidence and failure truth."""

from __future__ import annotations

import hashlib

import pytest
from disco.core.effects import MutationReceipt, OpaqueEffectReceipt
from disco.tools.anatomy import Capability, ToolContext, ToolOutcome
from disco.tools.builtin.mutation_batch import (
    DeterministicBatchResult,
    PlannedFileMutation,
    commit_deterministic_file_batch,
)
from disco.tools.sandbox import ProcessSandboxService, SandboxSpec
from disco.tools.sandbox.file_batch import (
    SandboxFileBatchFailure,
    SandboxFileBatchResult,
    SandboxFileChange,
    SandboxFileMutation,
)
from tool_fakes import FakeSandboxInstance

pytestmark = pytest.mark.asyncio


def _ctx(sandbox: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="batch",
    )


class _RecordingSandbox(FakeSandboxInstance):
    def __init__(self) -> None:
        super().__init__()
        self.commits: list[tuple[str, str]] = []
        self.fail_path: str | None = None
        self.fail_mode: str = "before"
        self.fail_reads: set[str] = set()

    async def read_file(self, path: str) -> bytes:
        if path in self.fail_reads:
            raise PermissionError(f"cannot read {path}")
        return await super().read_file(path)

    async def atomic_write(self, path: str, data: bytes) -> None:
        self.commits.append(("write", path))
        if path == self.fail_path:
            if self.fail_mode == "after":
                self._fs[path] = data
            elif self.fail_mode == "third":
                self._fs[path] = b"third-state"
            raise OSError(f"injected failure for {path}")
        self._fs[path] = data

    async def delete_file(self, path: str) -> None:
        self.commits.append(("delete", path))
        await super().delete_file(path)


class _BatchSandbox(_RecordingSandbox):
    def __init__(self, result: SandboxFileBatchResult) -> None:
        super().__init__()
        self.batch_result = result
        self.batch_calls: list[tuple[tuple[SandboxFileMutation, ...], tuple[str, ...]]] = []

    async def _commit_file_batch(
        self,
        mutations: tuple[SandboxFileMutation, ...],
        *,
        commit_last: tuple[str, ...],
    ) -> SandboxFileBatchResult:
        self.batch_calls.append((mutations, commit_last))
        return self.batch_result


class _FailingBatchSandbox(_RecordingSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.dispatches = 0

    async def _commit_file_batch(self, *_args, **_kwargs):
        self.dispatches += 1
        raise OSError("transport ended after dispatch")


async def test_backend_batch_dispatches_once_and_rebuilds_exact_receipts():
    before = hashlib.sha256(b"old").hexdigest()
    after = hashlib.sha256(b"new").hexdigest()
    sandbox = _BatchSandbox(
        SandboxFileBatchResult(
            changes=(SandboxFileChange("a.txt", before, after, 3),),
        )
    )

    result = await commit_deterministic_file_batch(
        _ctx(sandbox),
        [PlannedFileMutation("a.txt", b"new")],
        commit_last=("a.txt",),
    )

    assert isinstance(result, DeterministicBatchResult)
    assert len(sandbox.batch_calls) == 1
    mutations, commit_last = sandbox.batch_calls[0]
    assert mutations == (SandboxFileMutation("a.txt", b"new"),)
    assert commit_last == ("a.txt",)
    assert sandbox.commits == []
    assert result.changed_paths == ("a.txt",)
    assert result.receipts[0].before is not None
    assert result.receipts[0].before.digest == before
    assert result.receipts[0].after is not None
    assert result.receipts[0].after.digest == after


async def test_backend_batch_stale_after_proven_prefix_preserves_partial_truth():
    first_before = hashlib.sha256(b"a").hexdigest()
    first_after = hashlib.sha256(b"a2").hexdigest()
    sandbox = _BatchSandbox(
        SandboxFileBatchResult(
            changes=(SandboxFileChange("a.txt", first_before, first_after, 2),),
            failure=SandboxFileBatchFailure(
                phase="stale",
                error="STALE_FILE_CONTEXT",
                kind="stale_file_context",
                message="b.txt changed before commit",
                path="b.txt",
                failed_path_state="unchanged",
            ),
        )
    )

    result = await commit_deterministic_file_batch(
        _ctx(sandbox),
        [PlannedFileMutation("a.txt", b"a2"), PlannedFileMutation("b.txt", b"b2")],
    )

    assert isinstance(result, ToolOutcome)
    assert result.error == "BATCH_PARTIAL_COMMIT"
    assert result.artifacts == ["a.txt"]
    assert len(result.effect_receipts) == 1
    assert isinstance(result.effect_receipts[0], MutationReceipt)


async def test_backend_batch_transport_failure_is_opaque_and_never_retried():
    sandbox = _FailingBatchSandbox()

    result = await commit_deterministic_file_batch(
        _ctx(sandbox),
        [PlannedFileMutation("a.txt", b"new")],
    )

    assert isinstance(result, ToolOutcome)
    assert result.error == "BATCH_COMMIT_UNCERTAIN"
    assert sandbox.dispatches == 1
    assert sandbox.commits == []
    assert len(result.effect_receipts) == 1
    assert isinstance(result.effect_receipts[0], OpaqueEffectReceipt)


async def test_success_commits_sorted_then_marker_last_with_exact_write_and_delete_receipts():
    sandbox = _RecordingSandbox()
    sandbox._fs = {"a.txt": b"old-a", "marker.json": b"old-marker", "stale.txt": b"old"}

    result = await commit_deterministic_file_batch(
        _ctx(sandbox),
        [
            PlannedFileMutation("marker.json", b"new-marker"),
            PlannedFileMutation("stale.txt", None),
            PlannedFileMutation("a.txt", b"new-a"),
        ],
        commit_last=("marker.json",),
    )

    assert isinstance(result, DeterministicBatchResult)
    assert sandbox.commits == [
        ("write", "a.txt"),
        ("delete", "stale.txt"),
        ("write", "marker.json"),
    ]
    assert result.changed_paths == ("a.txt", "stale.txt", "marker.json")
    assert all(isinstance(receipt, MutationReceipt) for receipt in result.receipts)
    deletion = result.receipts[1]
    assert deletion.before is not None and deletion.after is None
    assert result.receipts[0].after is not None
    assert result.receipts[0].after.digest == hashlib.sha256(b"new-a").hexdigest()


async def test_all_reads_finish_before_first_write_and_unreadable_is_not_absence():
    sandbox = _RecordingSandbox()
    sandbox._fs = {"a.txt": b"old-a", "b.txt": b"old-b"}
    sandbox.fail_reads.add("b.txt")

    result = await commit_deterministic_file_batch(
        _ctx(sandbox),
        [
            PlannedFileMutation("a.txt", b"new-a"),
            PlannedFileMutation("b.txt", b"new-b"),
        ],
    )

    assert isinstance(result, ToolOutcome)
    assert result.error == "BATCH_PREVALIDATION_READ_FAILED"
    assert sandbox.commits == []
    assert sandbox._fs == {"a.txt": b"old-a", "b.txt": b"old-b"}
    assert result.effect_receipts == ()


async def test_byte_identical_plan_is_zero_progress():
    sandbox = _RecordingSandbox()
    sandbox._fs = {"same.txt": b"same"}

    result = await commit_deterministic_file_batch(
        _ctx(sandbox), [PlannedFileMutation("same.txt", b"same")]
    )

    assert isinstance(result, DeterministicBatchResult)
    assert result.changed_paths == ()
    assert result.receipts == ()
    assert sandbox.commits == []


async def test_alias_collision_with_different_final_bytes_refuses_before_write():
    sandbox = _RecordingSandbox()

    async def _resolve(path: str) -> str:
        return "same.txt" if path in {"alias.txt", "same.txt"} else path

    sandbox.resolve_relpath = _resolve  # type: ignore[method-assign]
    result = await commit_deterministic_file_batch(
        _ctx(sandbox),
        [
            PlannedFileMutation("alias.txt", b"one"),
            PlannedFileMutation("same.txt", b"two"),
        ],
    )

    assert isinstance(result, ToolOutcome)
    assert result.error == "BATCH_PLAN_CONFLICT"
    assert sandbox.commits == []


async def test_process_batch_delete_refuses_symlink_without_deleting_its_target():
    service = ProcessSandboxService()
    sandbox = await service.create(
        SandboxSpec(), owner_id="local", conversation_id="batch-delete-symlink"
    )
    try:
        await sandbox.write_file("user-owned.txt", b"preserve-me")
        linked = await sandbox.exec_shell("ln -s user-owned.txt stale-generated.txt", timeout_s=10)
        assert linked.exit_code == 0, linked.stderr

        result = await commit_deterministic_file_batch(
            ToolContext(
                sandbox=sandbox,
                workspace_path=".",
                timeout_s=30,
                capabilities={Capability.FILESYSTEM},
                owner_id="local",
                conversation_id="batch-delete-symlink",
            ),
            [PlannedFileMutation("stale-generated.txt", None)],
        )

        assert isinstance(result, ToolOutcome)
        assert result.error == "BATCH_DELETE_ALIAS_REFUSED"
        assert result.effect_receipts == ()
        assert await sandbox.read_file("user-owned.txt") == b"preserve-me"
        still_linked = await sandbox.exec_shell(
            "test -L stale-generated.txt && readlink stale-generated.txt", timeout_s=10
        )
        assert still_linked.exit_code == 0
        assert still_linked.stdout.strip() == "user-owned.txt"
    finally:
        await sandbox.destroy()


async def test_process_batch_delete_refuses_alias_swapped_in_after_prevalidation():
    service = ProcessSandboxService()
    sandbox = await service.create(
        SandboxSpec(), owner_id="local", conversation_id="batch-delete-symlink-race"
    )
    try:
        await sandbox.write_file("stale-generated.txt", b"same-bytes")
        await sandbox.write_file("user-owned.txt", b"same-bytes")
        original_read = sandbox.read_file
        read_count = 0

        async def _read_then_swap(path: str) -> bytes:
            nonlocal read_count
            data = await original_read(path)
            if path == "stale-generated.txt" and read_count == 0:
                read_count += 1
                swapped = await sandbox.exec_shell(
                    "rm stale-generated.txt && ln -s user-owned.txt stale-generated.txt",
                    timeout_s=10,
                )
                assert swapped.exit_code == 0, swapped.stderr
            return data

        sandbox.read_file = _read_then_swap  # type: ignore[method-assign]
        result = await commit_deterministic_file_batch(
            ToolContext(
                sandbox=sandbox,
                workspace_path=".",
                timeout_s=30,
                capabilities={Capability.FILESYSTEM},
                owner_id="local",
                conversation_id="batch-delete-symlink-race",
            ),
            [PlannedFileMutation("stale-generated.txt", None)],
        )

        assert isinstance(result, ToolOutcome)
        assert result.error == "BATCH_COMMIT_UNCERTAIN"
        assert result.structured["failed_path_state"] == "unknown"
        assert len(result.effect_receipts) == 1
        assert isinstance(result.effect_receipts[0], OpaqueEffectReceipt)
        assert await original_read("user-owned.txt") == b"same-bytes"
        still_linked = await sandbox.exec_shell(
            "test -L stale-generated.txt && readlink stale-generated.txt", timeout_s=10
        )
        assert still_linked.exit_code == 0
        assert still_linked.stdout.strip() == "user-owned.txt"
    finally:
        await sandbox.destroy()


@pytest.mark.parametrize(
    ("mode", "error", "receipt_type"),
    [
        ("before", "BATCH_PARTIAL_COMMIT", MutationReceipt),
        ("after", "BATCH_COMMIT_INTERRUPTED", MutationReceipt),
        ("third", "BATCH_COMMIT_UNCERTAIN", OpaqueEffectReceipt),
    ],
)
async def test_second_commit_failure_reports_only_proven_state(
    mode: str,
    error: str,
    receipt_type: type[MutationReceipt] | type[OpaqueEffectReceipt],
) -> None:
    sandbox = _RecordingSandbox()
    sandbox._fs = {"a.txt": b"a", "b.txt": b"b"}
    sandbox.fail_path = "b.txt"
    sandbox.fail_mode = mode

    result = await commit_deterministic_file_batch(
        _ctx(sandbox),
        [
            PlannedFileMutation("a.txt", b"a2"),
            PlannedFileMutation("b.txt", b"b2"),
        ],
    )

    assert isinstance(result, ToolOutcome)
    assert result.error == error
    assert (
        result.structured["failed_path_state"]
        == {
            "before": "unchanged",
            "after": "committed",
            "third": "unknown",
        }[mode]
    )
    assert isinstance(result.effect_receipts[-1], receipt_type)
    assert "no file was committed" not in result.content
