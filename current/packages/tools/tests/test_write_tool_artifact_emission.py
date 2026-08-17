"""Generic write tools persist artifact identity and exact mutation evidence."""

from __future__ import annotations

import hashlib

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.files import (
    ExactReplaceTool,
    FileAppendTool,
    FileEditTool,
    FileInsertLinesTool,
    FileReplaceLinesTool,
    FileStrReplaceTool,
    FileWriteTool,
    SafeWriteFileTool,
    reset_read_tracker,
)
from disco.tools.sandbox.base import strip_redundant_workspace_prefix


class _FakeSandbox:
    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        self._fs = {strip_redundant_workspace_prefix(k): v for k, v in (existing or {}).items()}

    async def read_file(self, path: str) -> bytes:
        key = strip_redundant_workspace_prefix(path)
        if key not in self._fs:
            raise FileNotFoundError(path)
        return self._fs[key]

    async def write_file(self, path: str, data: bytes) -> None:
        self._fs[strip_redundant_workspace_prefix(path)] = data


class _ReadFailureSandbox(_FakeSandbox):
    async def read_file(self, path: str) -> bytes:
        raise PermissionError(f"denied: {path}")


class _AliasSandbox(_FakeSandbox):
    async def resolve_relpath(self, path: str) -> str:
        clean = strip_redundant_workspace_prefix(path)
        return "real/" + clean.removeprefix("alias/") if clean.startswith("alias/") else clean

    async def read_file(self, path: str) -> bytes:
        clean = strip_redundant_workspace_prefix(path)
        if clean.startswith("alias/"):
            clean = "real/" + clean.removeprefix("alias/")
        return await super().read_file(clean)


class _ConcurrentChangeSandbox(_FakeSandbox):
    def __init__(self) -> None:
        super().__init__({"race.txt": b"A"})
        self._reads = 0
        self.writes = 0

    async def read_file(self, path: str) -> bytes:
        self._reads += 1
        if self._reads == 2:
            self._fs["race.txt"] = b"B"
        return await super().read_file(path)

    async def write_file(self, path: str, data: bytes) -> None:
        self.writes += 1
        await super().write_file(path, data)


def _ctx(sbx: _FakeSandbox) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=10,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="rel-2a-step3",
    )


@pytest.fixture(autouse=True)
def _clean_tracker():
    reset_read_tracker()
    yield
    reset_read_tracker()


@pytest.mark.parametrize(
    ("tool", "args", "existing", "expected_path", "expected_bytes"),
    [
        (
            FileWriteTool(),
            FileWriteTool.definition.args_model(path="workspace/file_write.txt", content="alpha\n"),
            {},
            "file_write.txt",
            b"alpha\n",
        ),
        (
            FileAppendTool(),
            FileAppendTool.definition.args_model(
                path="workspace/file_append.txt", content="beta\n"
            ),
            {"file_append.txt": b"alpha\n"},
            "file_append.txt",
            b"alpha\nbeta\n",
        ),
        (
            FileEditTool(),
            FileEditTool.definition.args_model(
                path="workspace/file_edit.txt", old="alpha", new="bravo"
            ),
            {"file_edit.txt": b"alpha\n"},
            "file_edit.txt",
            b"bravo\n",
        ),
        (
            FileStrReplaceTool(),
            FileStrReplaceTool.definition.args_model(
                path="workspace/file_str_replace.txt", old_str="alpha", new_str="bravo"
            ),
            {"file_str_replace.txt": b"alpha\n"},
            "file_str_replace.txt",
            b"bravo\n",
        ),
        (
            ExactReplaceTool(),
            ExactReplaceTool.definition.args_model(
                path="workspace/exact_replace.txt",
                edits=[{"old_string": "alpha", "new_string": "bravo"}],
            ),
            {"exact_replace.txt": b"alpha\n"},
            "exact_replace.txt",
            b"bravo\n",
        ),
        (
            FileReplaceLinesTool(),
            FileReplaceLinesTool.definition.args_model(
                path="workspace/file_replace_lines.txt",
                start_line=2,
                end_line=2,
                new_text="bravo",
            ),
            {"file_replace_lines.txt": b"alpha\nbeta\n"},
            "file_replace_lines.txt",
            b"alpha\nbravo\n",
        ),
        (
            FileInsertLinesTool(),
            FileInsertLinesTool.definition.args_model(
                path="workspace/file_insert_lines.txt", after_line=1, text="bravo"
            ),
            {"file_insert_lines.txt": b"alpha\n"},
            "file_insert_lines.txt",
            b"alpha\nbravo\n",
        ),
        (
            SafeWriteFileTool(),
            SafeWriteFileTool.definition.args_model(
                path="workspace/safe_write_file.txt", content="alpha\n"
            ),
            {},
            "safe_write_file.txt",
            b"alpha\n",
        ),
    ],
    ids=[
        "file_write",
        "file_append",
        "file_edit",
        "file_str_replace",
        "exact_replace",
        "file_replace_lines",
        "file_insert_lines",
        "safe_write_file",
    ],
)
async def test_write_tool_success_emits_path_and_post_write_sha256(
    tool,
    args,
    existing: dict[str, bytes],
    expected_path: str,
    expected_bytes: bytes,
) -> None:
    sbx = _FakeSandbox(existing)

    res = await tool.run(args, _ctx(sbx))

    assert res.success, res.content
    assert sbx._fs[expected_path] == expected_bytes
    assert res.structured is not None
    assert res.structured["path"] == expected_path
    assert res.structured["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
    assert len(res.effect_receipts) == 1
    receipt = res.effect_receipts[0]
    assert receipt.kind.value == "mutation"
    assert receipt.capability.value == "workspace.mutate"
    assert receipt.resource.namespace == "workspace.file"
    assert receipt.resource.identifier == expected_path
    assert receipt.after is not None
    assert receipt.after.resource == receipt.resource
    assert receipt.after.digest == hashlib.sha256(expected_bytes).hexdigest()
    assert receipt.after_size_bytes == len(expected_bytes)
    old_bytes = existing.get(expected_path)
    if old_bytes is None:
        assert receipt.before is None
    else:
        assert receipt.before is not None
        assert receipt.before.resource == receipt.resource
        assert receipt.before.digest == hashlib.sha256(old_bytes).hexdigest()


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            FileStrReplaceTool(),
            FileStrReplaceTool.definition.args_model(
                path="same.txt", old_str="alpha", new_str="alpha"
            ),
        ),
        (
            ExactReplaceTool(),
            ExactReplaceTool.definition.args_model(
                path="same.txt",
                edits=[{"old_string": "alpha", "new_string": "alpha"}],
            ),
        ),
        (
            SafeWriteFileTool(),
            SafeWriteFileTool.definition.args_model(
                path="same.txt",
                content="alpha\n",
                expected_sha256=hashlib.sha256(b"alpha\n").hexdigest(),
            ),
        ),
    ],
    ids=["file_str_replace", "exact_replace", "safe_write_file"],
)
async def test_byte_identical_mutators_refuse_without_receipts(tool, args) -> None:
    sbx = _FakeSandbox({"same.txt": b"alpha\n"})

    res = await tool.run(args, _ctx(sbx))

    assert not res.success
    assert res.error in {"no_op_edit", "no_op_write"}
    assert res.effect_receipts == ()
    assert sbx._fs["same.txt"] == b"alpha\n"


@pytest.mark.parametrize(
    ("existing", "content", "expected_before"),
    [
        ({}, "", None),
        ({"empty.txt": b""}, "x", hashlib.sha256(b"").hexdigest()),
    ],
    ids=["absent-to-empty", "empty-to-nonempty"],
)
async def test_mutation_receipt_distinguishes_absence_from_empty_file(
    existing: dict[str, bytes],
    content: str,
    expected_before: str | None,
) -> None:
    sbx = _FakeSandbox(existing)

    res = await FileAppendTool().run(
        FileAppendTool.definition.args_model(path="empty.txt", content=content),
        _ctx(sbx),
    )

    assert res.success
    receipt = res.effect_receipts[0]
    if expected_before is None:
        assert receipt.before is None
    else:
        assert receipt.before is not None
        assert receipt.before.digest == expected_before
    assert receipt.after is not None
    assert receipt.after.digest == hashlib.sha256(content.encode()).hexdigest()
    assert receipt.after_size_bytes == len(content.encode())


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            FileWriteTool(),
            FileWriteTool.definition.args_model(path="denied.txt", content="new"),
        ),
        (
            FileAppendTool(),
            FileAppendTool.definition.args_model(path="denied.txt", content="new"),
        ),
        (
            SafeWriteFileTool(),
            SafeWriteFileTool.definition.args_model(path="denied.txt", content="new"),
        ),
    ],
    ids=["file_write", "file_append", "safe_write_file"],
)
async def test_read_failures_are_not_mislabeled_as_absent(tool, args) -> None:
    sbx = _ReadFailureSandbox()

    with pytest.raises(PermissionError, match="denied"):
        await tool.run(args, _ctx(sbx))

    assert sbx._fs == {}


async def test_receipt_uses_sandbox_resolved_resource_identity() -> None:
    sbx = _AliasSandbox({"real/x.txt": b"A"})

    res = await FileAppendTool().run(
        FileAppendTool.definition.args_model(path="alias/x.txt", content="B"),
        _ctx(sbx),
    )

    assert res.success
    assert sbx._fs["real/x.txt"] == b"AB"
    assert "alias/x.txt" not in sbx._fs
    assert res.effect_receipts[0].resource.identifier == "real/x.txt"


async def test_concurrent_change_refuses_before_commit_without_receipt() -> None:
    sbx = _ConcurrentChangeSandbox()

    res = await FileAppendTool().run(
        FileAppendTool.definition.args_model(path="race.txt", content="C"),
        _ctx(sbx),
    )

    assert not res.success
    assert res.error == "STALE_FILE_CONTEXT"
    assert res.effect_receipts == ()
    assert sbx._fs["race.txt"] == b"B"
    assert sbx.writes == 0
