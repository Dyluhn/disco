"""CD-TOOLS-7 — buffered prevalidation plus exact per-file commit evidence."""

from __future__ import annotations

import hashlib
from typing import Literal

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import RunProjectScriptTool
from disco.tools.builtin.files import _read_state, mark_read, reset_read_tracker
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.process import ProcessSandboxService

pytestmark = pytest.mark.asyncio


class _CommitFailureSandbox:
    def __init__(
        self,
        *,
        failure_mode: Literal["before", "after", "third", "unreadable"] = "before",
        fail_on: int = 2,
    ) -> None:
        self._fs = {"a.txt": b"A", "b.txt": b"B"}
        self._commits = 0
        self._failure_mode = failure_mode
        self._fail_on = fail_on
        self._failed_path: str | None = None

    async def resolve_relpath(self, path: str) -> str:
        return path

    async def read_file(self, path: str) -> bytes:
        if self._failure_mode == "unreadable" and path == self._failed_path:
            raise PermissionError("injected unreadable post-failure state")
        if path not in self._fs:
            raise FileNotFoundError(path)
        return self._fs[path]

    async def list_dir(self, path: str) -> list[str]:
        return sorted(self._fs)

    async def write_file(self, path: str, data: bytes) -> None:
        await self.atomic_write(path, data)

    async def atomic_write(self, path: str, data: bytes) -> None:
        self._commits += 1
        if self._commits == self._fail_on:
            if self._failure_mode == "after":
                self._fs[path] = data
            elif self._failure_mode == "third":
                self._fs[path] = b"third-party-state"
            self._failed_path = path
            raise OSError("injected second commit failure")
        self._fs[path] = data


def _fake_ctx(sandbox: _CommitFailureSandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="partial",
    )


@pytest.fixture(autouse=True)
def _clean():
    reset_read_tracker()
    yield
    reset_read_tracker()


async def _ctx():
    svc = ProcessSandboxService()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    ctx = ToolContext(
        sandbox=inst,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="c",
    )
    return ctx, inst


def _run(ops):
    return RunProjectScriptTool().run(
        RunProjectScriptTool.definition.args_model(operations=ops), None
    )  # type: ignore


async def _go(ctx, ops):
    return await RunProjectScriptTool().run(
        RunProjectScriptTool.definition.args_model(operations=ops), ctx
    )


async def _read(inst, p):
    return (await inst.read_file(p)).decode("utf-8")


async def test_all_writes_commit_with_exact_receipts():
    ctx, inst = await _ctx()
    await inst.write_file("b.txt", b"hello world here\n")
    res = await _go(
        ctx,
        [
            {"op": "save", "path": "a.txt", "content": "brand new\n"},
            {"op": "read", "path": "b.txt"},
            {"op": "replace_text", "path": "b.txt", "old": "world", "new": "there"},
        ],
    )
    assert res.success, res.content
    assert await _read(inst, "a.txt") == "brand new\n"
    assert await _read(inst, "b.txt") == "hello there here\n"
    assert set(res.structured["applied"]) == {"a.txt", "b.txt"}
    assert [receipt.resource.identifier for receipt in res.effect_receipts] == [
        "a.txt",
        "b.txt",
    ]
    assert res.effect_receipts[0].before is None
    assert res.effect_receipts[1].before is not None
    assert res.effect_receipts[1].before.digest == hashlib.sha256(b"hello world here\n").hexdigest()
    assert res.effect_receipts[1].after is not None
    assert res.effect_receipts[1].after.digest == hashlib.sha256(b"hello there here\n").hexdigest()
    await inst.destroy()


async def test_precommit_failure_writes_nothing():
    # A later validation failure occurs before the commit phase, so nothing is written.
    ctx, inst = await _ctx()
    await inst.write_file("b.txt", b"content\n")
    res = await _go(
        ctx,
        [
            {"op": "save", "path": "new.txt", "content": "should not persist\n"},
            {
                "op": "replace_text",
                "path": "b.txt",
                "old": "ABSENT",
                "new": "x",
            },  # no match → abort
        ],
    )
    assert not res.success and res.error == "SCRIPT_NO_MATCH"
    assert not await inst.file_exists("new.txt")
    assert res.effect_receipts == ()
    await inst.destroy()


async def test_shrink_guard_rejects_truncation_no_write():
    ctx, inst = await _ctx()
    body = b"x" * 1000 + b"\n"
    await inst.write_file("big.txt", body)
    res = await _go(
        ctx,
        [
            {"op": "read", "path": "big.txt"},  # ground it
            {"op": "save", "path": "big.txt", "content": "tiny\n"},
        ],
    )
    assert not res.success and res.error == "SAFE_WRITE_SHRINK_REJECTED"
    assert (await inst.read_file("big.txt")) == body
    await inst.destroy()


async def test_replace_text_is_literal():
    ctx, inst = await _ctx()
    await inst.write_file("t.txt", b"PH and padding here\n")
    literal = "$1 ${x} \\1 costs $5"
    res = await _go(ctx, [{"op": "replace_text", "path": "t.txt", "old": "PH", "new": literal}])
    assert res.success, res.content
    assert literal in await _read(inst, "t.txt")
    await inst.destroy()


async def test_governed_disco_path_rejected_no_write():
    ctx, inst = await _ctx()
    res = await _go(ctx, [{"op": "save", "path": ".disco/appspec.json", "content": "{}"}])
    assert not res.success and res.error in (
        "GOVERNED_ARTIFACT_REJECTED",
        "SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED",
    )
    assert not await inst.file_exists(".disco/appspec.json")
    await inst.destroy()


async def test_elision_marker_rejected_no_write():
    ctx, inst = await _ctx()
    res = await _go(ctx, [{"op": "save", "path": "e.txt", "content": "a <4500 chars elided> b"}])
    assert not res.success and res.error == "ELISION_MARKER_REJECTED"
    assert not await inst.file_exists("e.txt")
    await inst.destroy()


async def test_syntax_introducing_batch_rejected_no_write():
    ctx, inst = await _ctx()
    await inst.write_file("m.py", b"x = 1\ny = 2\n")
    res = await _go(
        ctx,
        [
            {"op": "replace_text", "path": "m.py", "old": "y = 2", "new": "y = ("},
        ],
    )
    assert not res.success and res.error == "SCRIPT_BATCH_FAILED"
    assert await _read(inst, "m.py") == "x = 1\ny = 2\n"
    await inst.destroy()


async def test_over_cap_rejected():
    ctx, inst = await _ctx()
    ops = [{"op": "ls", "path": "."} for _ in range(201)]
    res = await _go(ctx, ops)
    assert not res.success and res.error == "SCRIPT_TOO_LARGE"
    await inst.destroy()


async def test_read_and_ls_do_not_mutate():
    ctx, inst = await _ctx()
    await inst.write_file("r.txt", b"data\n")
    res = await _go(ctx, [{"op": "read", "path": "r.txt"}, {"op": "ls", "path": "."}])
    assert res.success and res.structured["applied"] == []
    assert res.structured["reads"]["r.txt"] == "data\n"
    assert "r.txt" in res.structured["reads"]["."]
    assert res.effect_receipts == ()
    await inst.destroy()


@pytest.mark.parametrize("op", ["replace_text", "save"])
async def test_byte_identical_mutation_is_refused_and_not_receipted(op: str):
    ctx, inst = await _ctx()
    body = "current content\n"
    await inst.write_file("same.txt", body.encode())
    operations = [{"op": "read", "path": "same.txt"}]
    operations.append(
        {
            "op": op,
            "path": "same.txt",
            **({"old": body, "new": body} if op == "replace_text" else {"content": body}),
        }
    )

    res = await _go(ctx, operations)

    assert not res.success
    assert res.error == "SCRIPT_NO_CHANGES"
    assert await _read(inst, "same.txt") == body
    assert res.effect_receipts == ()
    await inst.destroy()


async def test_mixed_batch_receipts_include_only_byte_changed_paths():
    ctx, inst = await _ctx()
    await inst.write_file("same.txt", b"same\n")
    await inst.write_file("changed.txt", b"before\n")
    res = await _go(
        ctx,
        [
            {"op": "read", "path": "same.txt"},
            {"op": "save", "path": "same.txt", "content": "same\n"},
            {"op": "replace_text", "path": "changed.txt", "old": "before", "new": "after"},
        ],
    )

    assert res.success, res.content
    assert res.structured["applied"] == ["changed.txt"]
    assert await _read(inst, "same.txt") == "same\n"
    assert await _read(inst, "changed.txt") == "after\n"
    assert [receipt.resource.identifier for receipt in res.effect_receipts] == ["changed.txt"]
    await inst.destroy()


async def test_new_empty_file_is_an_effective_mutation():
    ctx, inst = await _ctx()
    res = await _go(ctx, [{"op": "save", "path": "empty.txt", "content": ""}])

    assert res.success, res.content
    assert res.structured["applied"] == ["empty.txt"]
    assert await inst.file_exists("empty.txt")
    assert res.effect_receipts[0].before is None
    assert res.effect_receipts[0].after_size_bytes == 0
    await inst.destroy()


async def test_save_over_unread_existing_requires_grounding():
    ctx, inst = await _ctx()
    await inst.write_file("f.txt", b"existing content that is fine\n")
    res = await _go(
        ctx, [{"op": "save", "path": "f.txt", "content": "new content that is also fine\n"}]
    )
    assert not res.success and res.error == "FRESH_READ_REQUIRED"
    assert await _read(inst, "f.txt") == "existing content that is fine\n"
    await inst.destroy()


async def test_save_after_read_is_grounded():
    ctx, inst = await _ctx()
    await inst.write_file("f.txt", b"existing content that is fine\n")
    res = await _go(
        ctx,
        [
            {"op": "read", "path": "f.txt"},
            {"op": "save", "path": "f.txt", "content": "new content that is also fine\n"},
        ],
    )
    assert res.success, res.content
    assert await _read(inst, "f.txt") == "new content that is also fine\n"
    await inst.destroy()


async def test_save_with_matching_sha_is_grounded():
    ctx, inst = await _ctx()
    body = b"existing content that is fine\n"
    await inst.write_file("f.txt", body)
    res = await _go(
        ctx,
        [
            {
                "op": "save",
                "path": "f.txt",
                "content": "replaced ok here\n",
                "expected_sha256": hashlib.sha256(body).hexdigest(),
            },
        ],
    )
    assert res.success, res.content
    await inst.destroy()


async def test_symlink_alias_to_governed_file_is_rejected_before_read():
    # S-W5: workspace reads reject symlinks at the boundary, which is stricter
    # than resolving the alias and then relying on the governed-file guard.
    ctx, inst = await _ctx()
    await inst.write_file(".disco/secret.json", b'{"real":1}')
    import os

    ws = inst._workspace  # type: ignore[attr-defined]
    os.symlink(".disco/secret.json", ws / "link.json")
    res = await _go(
        ctx,
        [
            {"op": "read", "path": "link.json"},  # grounds the REAL .disco path
            {"op": "save", "path": "link.json", "content": "{}"},  # → governed on the real path
        ],
    )
    assert not res.success and res.error == "SCRIPT_PATH_ESCAPE"
    assert (await inst.read_file(".disco/secret.json")) == b'{"real":1}'  # untouched
    await inst.destroy()


async def test_lexical_alias_commits_to_the_real_file():
    # two different path strings for the SAME real file → one key; a read of one grounds a save of
    # the other, and the commit writes the real file (no divergence).
    ctx, inst = await _ctx()
    await inst.write_file("dir/f.txt", b"original content here ok\n")
    res = await _go(
        ctx,
        [
            {"op": "read", "path": "dir/f.txt"},
            {"op": "save", "path": "dir/../dir/f.txt", "content": "rewritten content here ok\n"},
        ],
    )
    assert res.success, res.content
    assert await _read(inst, "dir/f.txt") == "rewritten content here ok\n"
    await inst.destroy()


async def test_symlink_alias_to_binary_is_rejected_before_read():
    # A symlink cannot disguise a binary deliverable because it cannot cross the
    # sandbox-to-host read boundary at all.
    ctx, inst = await _ctx()
    await inst.write_file("deck.pptx", b"PK\x03\x04 binary pptx bytes padding padding\n")
    import os

    ws = inst._workspace  # type: ignore[attr-defined]
    os.symlink("deck.pptx", ws / "alias.txt")
    res = await _go(
        ctx,
        [
            {"op": "read", "path": "alias.txt"},
            {"op": "save", "path": "alias.txt", "content": "text clobber"},
        ],
    )
    assert not res.success and res.error == "SCRIPT_PATH_ESCAPE"
    await inst.destroy()


async def test_save_new_file_needs_no_grounding():
    ctx, inst = await _ctx()
    res = await _go(ctx, [{"op": "save", "path": "fresh.txt", "content": "hi\n"}])
    assert res.success, res.content
    assert await _read(inst, "fresh.txt") == "hi\n"
    await inst.destroy()


@pytest.mark.parametrize("failure_mode", ["before", "after"])
async def test_runtime_commit_failure_returns_exact_committed_prefix(
    failure_mode: Literal["before", "after"],
) -> None:
    sandbox = _CommitFailureSandbox(failure_mode=failure_mode)

    res = await _go(
        _fake_ctx(sandbox),
        [
            {"op": "replace_text", "path": "a.txt", "old": "A", "new": "A2"},
            {"op": "replace_text", "path": "b.txt", "old": "B", "new": "B2"},
        ],
    )

    assert not res.success
    expected_error = (
        "SCRIPT_COMMIT_INTERRUPTED" if failure_mode == "after" else "SCRIPT_PARTIAL_COMMIT"
    )
    assert res.error == expected_error
    expected_applied = ["a.txt", "b.txt"] if failure_mode == "after" else ["a.txt"]
    assert res.structured["applied"] == expected_applied
    assert res.artifacts == expected_applied
    assert [receipt.resource.identifier for receipt in res.effect_receipts] == expected_applied
    assert sandbox._fs["a.txt"] == b"A2"
    assert sandbox._fs["b.txt"] == (b"B2" if failure_mode == "after" else b"B")
    assert res.structured["failed_path_state"] == (
        "committed" if failure_mode == "after" else "unchanged"
    )
    assert res.structured["underlying"] == {
        "type": "OSError",
        "message": "injected second commit failure",
    }
    assert "injected second commit failure" in res.content


@pytest.mark.parametrize("failure_mode", ["third", "unreadable"])
async def test_first_commit_unknown_state_is_never_reported_as_no_commit(
    failure_mode: Literal["third", "unreadable"],
) -> None:
    sandbox = _CommitFailureSandbox(failure_mode=failure_mode, fail_on=1)
    mark_read("partial", "a.txt")

    res = await _go(
        _fake_ctx(sandbox),
        [{"op": "replace_text", "path": "a.txt", "old": "A", "new": "A2"}],
    )

    assert not res.success
    assert res.error == "SCRIPT_COMMIT_UNCERTAIN"
    assert res.structured["failed_path_state"] == "unknown"
    assert res.structured["applied"] == []
    assert len(res.effect_receipts) == 1
    assert res.effect_receipts[0].kind.value == "opaque"
    assert "no file was committed" not in res.content
    assert "a.txt" not in _read_state["partial"]["read_since_write"]


async def test_commit_ack_failure_clears_stale_grounding_for_written_path() -> None:
    sandbox = _CommitFailureSandbox(failure_mode="after", fail_on=1)
    mark_read("partial", "a.txt")

    res = await _go(
        _fake_ctx(sandbox),
        [{"op": "replace_text", "path": "a.txt", "old": "A", "new": "A2"}],
    )

    assert res.error == "SCRIPT_COMMIT_INTERRUPTED"
    assert res.structured["applied"] == ["a.txt"]
    assert "a.txt" not in _read_state["partial"]["read_since_write"]


class _StaleSecondPathSandbox(_CommitFailureSandbox):
    def __init__(self) -> None:
        super().__init__()
        self._b_reads = 0

    async def read_file(self, path: str) -> bytes:
        if path == "b.txt":
            self._b_reads += 1
            if self._b_reads >= 2:
                self._fs[path] = b"external"
        return await super().read_file(path)


async def test_stale_first_commit_preserves_original_recovery_guidance() -> None:
    sandbox = _StaleSecondPathSandbox()

    res = await _go(
        _fake_ctx(sandbox),
        [{"op": "replace_text", "path": "b.txt", "old": "B", "new": "B2"}],
    )

    assert not res.success
    assert res.error == "STALE_FILE_CONTEXT"
    assert res.structured["next_required_action"] == "file_read"
    assert "Read the current file" in res.content


async def test_stale_after_prefix_preserves_underlying_recovery_guidance() -> None:
    sandbox = _StaleSecondPathSandbox()

    res = await _go(
        _fake_ctx(sandbox),
        [
            {"op": "replace_text", "path": "a.txt", "old": "A", "new": "A2"},
            {"op": "replace_text", "path": "b.txt", "old": "B", "new": "B2"},
        ],
    )

    assert not res.success
    assert res.error == "SCRIPT_PARTIAL_COMMIT"
    assert res.structured["applied"] == ["a.txt"]
    assert res.structured["underlying"]["error"] == "STALE_FILE_CONTEXT"
    assert res.structured["underlying"]["structured"]["next_required_action"] == "file_read"
    assert "Read the current file" in res.content


async def test_existing_empty_file_has_real_before_revision() -> None:
    ctx, inst = await _ctx()
    await inst.write_file("empty.txt", b"")

    res = await _go(
        ctx,
        [
            {"op": "read", "path": "empty.txt"},
            {"op": "save", "path": "empty.txt", "content": "x"},
        ],
    )

    assert res.success
    assert res.effect_receipts[0].before is not None
    assert res.effect_receipts[0].before.digest == hashlib.sha256(b"").hexdigest()
    await inst.destroy()
