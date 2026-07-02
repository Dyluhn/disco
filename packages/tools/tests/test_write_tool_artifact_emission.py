"""REL-2a step3: generic write tools persist artifact path + post-write sha."""

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
        self._fs = {
            strip_redundant_workspace_prefix(k): v for k, v in (existing or {}).items()
        }

    async def read_file(self, path: str) -> bytes:
        key = strip_redundant_workspace_prefix(path)
        if key not in self._fs:
            raise FileNotFoundError(path)
        return self._fs[key]

    async def write_file(self, path: str, data: bytes) -> None:
        self._fs[strip_redundant_workspace_prefix(path)] = data


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
