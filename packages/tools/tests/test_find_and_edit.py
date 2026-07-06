from __future__ import annotations

import json
import posixpath
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from disco.core.llm import ConfigStore, ModelExecutionPolicy
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.appkit_scope import APPKIT_MUTATORS, APPKIT_PROBES, APPKIT_READ_TOOLS
from disco.tools.builtin import FindAndEditTool, build_default_registry
from disco.tools.builtin.files import reset_read_tracker
from disco.tools.registry import agent_scope
from disco.tools.sandbox.base import strip_redundant_workspace_prefix


class _TreeSandbox:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._fs = {self._norm(path): data for path, data in files.items()}

    @staticmethod
    def _norm(path: str) -> str:
        normalized = posixpath.normpath(strip_redundant_workspace_prefix(path))
        return "." if normalized in ("", ".") else normalized

    async def read_file(self, path: str) -> bytes:
        key = self._norm(path)
        if key not in self._fs:
            raise FileNotFoundError(path)
        return self._fs[key]

    async def write_file(self, path: str, data: bytes) -> None:
        self._fs[self._norm(path)] = data

    async def list_dir(self, path: str) -> list[str]:
        key = self._norm(path)
        if key in self._fs:
            raise NotADirectoryError(path)
        prefix = "" if key == "." else f"{key.rstrip('/')}/"
        children: set[str] = set()
        for file_path in self._fs:
            if prefix and not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix):] if prefix else file_path
            if rest:
                children.add(rest.split("/", 1)[0])
        if not children and key != ".":
            raise FileNotFoundError(path)
        return sorted(children)

    async def file_exists(self, path: str) -> bool:
        return self._norm(path) in self._fs

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return None

    async def destroy(self) -> None:
        return None


def _ctx(sandbox: _TreeSandbox, *, conv: str = "find-edit-test") -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id=conv,
        driver_llm=("http://driver.test/v1", "driver-model", None),
    )


def _args(**kwargs: Any):
    data: dict[str, Any] = {
        "pattern": "old_name",
        "instruction": "Rename old_name to new_name.",
        "paths": None,
        "glob": None,
    }
    data.update(kwargs)
    return FindAndEditTool.definition.args_model(**data)


@pytest.fixture(autouse=True)
def _clean_read_tracker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ConfigStore, "origin_approved", lambda *args, **kwargs: True)
    reset_read_tracker()
    yield
    reset_read_tracker()


async def _read(sandbox: _TreeSandbox, path: str) -> str:
    return (await sandbox.read_file(path)).decode("utf-8")


@pytest.mark.asyncio
async def test_multi_site_edit_across_globbed_files() -> None:
    sandbox = _TreeSandbox(
        {
            "src/a.py": b"value = old_name\n",
            "src/b.py": b"return old_name\n",
            "notes.txt": b"old_name\n",
        }
    )
    mock_llm = AsyncMock(
        return_value=json.dumps({"action": "edit", "replacement": "new_name"})
    )

    with patch("disco.tools.builtin.find_and_edit._call_llm", mock_llm):
        outcome = await FindAndEditTool().run(_args(glob="src/**/*.py"), _ctx(sandbox))

    assert outcome.success, outcome.content
    assert await _read(sandbox, "src/a.py") == "value = new_name\n"
    assert await _read(sandbox, "src/b.py") == "return new_name\n"
    assert await _read(sandbox, "notes.txt") == "old_name\n"
    assert outcome.artifacts == ["src/a.py", "src/b.py"]
    assert outcome.structured is not None
    assert outcome.structured["scanned_files"] == 2
    assert outcome.structured["matches"] == 2
    assert outcome.structured["edited"] == 2
    assert outcome.structured["skipped"] == 0
    assert mock_llm.await_count == 2


@pytest.mark.parametrize(
    ("raw_response", "reason"),
    [
        (json.dumps({"action": "skip", "replacement": ""}), "action_skip"),
        ("this is not json", "parse_failed"),
    ],
)
@pytest.mark.asyncio
async def test_no_op_responses_leave_file_byte_identical(
    raw_response: str,
    reason: str,
) -> None:
    original = b"value = old_name\n"
    sandbox = _TreeSandbox({"a.py": original})

    with patch(
        "disco.tools.builtin.find_and_edit._call_llm",
        AsyncMock(return_value=raw_response),
    ):
        outcome = await FindAndEditTool().run(_args(paths=["a.py"]), _ctx(sandbox))

    assert outcome.success, outcome.content
    assert await sandbox.read_file("a.py") == original
    assert outcome.structured is not None
    assert outcome.structured["edited"] == 0
    assert outcome.structured["skipped"] == 1
    match = outcome.structured["files"][0]["matches"][0]
    assert match["status"] == "skipped"
    assert match["reason"] == reason


@pytest.mark.asyncio
async def test_think_leak_is_stripped_before_json_parse() -> None:
    sandbox = _TreeSandbox({"a.py": b"value = old_name\n"})
    response = '<think>checking whether to edit</think>{"action":"edit","replacement":"new_name"}'

    with patch(
        "disco.tools.builtin.find_and_edit._call_llm",
        AsyncMock(return_value=response),
    ):
        outcome = await FindAndEditTool().run(_args(paths=["a.py"]), _ctx(sandbox))

    assert outcome.success, outcome.content
    assert await _read(sandbox, "a.py") == "value = new_name\n"
    assert outcome.structured is not None
    assert outcome.structured["edited"] == 1


@pytest.mark.asyncio
async def test_max_matches_refuses_before_llm_calls() -> None:
    original = b"x x x\n"
    sandbox = _TreeSandbox({"a.txt": original})
    mock_llm = AsyncMock(return_value=json.dumps({"action": "edit", "replacement": "y"}))

    with patch("disco.tools.builtin.find_and_edit._call_llm", mock_llm):
        outcome = await FindAndEditTool().run(
            _args(pattern="x", paths=["a.txt"], max_matches=2),
            _ctx(sandbox),
        )

    assert not outcome.success
    assert outcome.error == "FIND_AND_EDIT_MAX_MATCHES_EXCEEDED"
    assert await sandbox.read_file("a.txt") == original
    mock_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_sha_drift_skips_file_without_corrupting_current_bytes() -> None:
    sandbox = _TreeSandbox({"a.py": b"value = old_name\n"})

    async def drift_then_edit(*_args: Any, **_kwargs: Any) -> str:
        await sandbox.write_file("a.py", b"value = drifted_old_name\n")
        return json.dumps({"action": "edit", "replacement": "new_name"})

    with patch("disco.tools.builtin.find_and_edit._call_llm", drift_then_edit):
        outcome = await FindAndEditTool().run(_args(paths=["a.py"]), _ctx(sandbox))

    assert outcome.success, outcome.content
    assert await _read(sandbox, "a.py") == "value = drifted_old_name\n"
    assert outcome.structured is not None
    assert outcome.structured["edited"] == 0
    assert outcome.structured["skipped"] == 1
    match = outcome.structured["files"][0]["matches"][0]
    assert match["status"] == "skipped"
    assert match["reason"] == "exact_replace_failed:STALE_FILE_CONTEXT"


def test_registered_in_agent_scope_but_not_strict_appkit_scope() -> None:
    registry = build_default_registry()
    scope = agent_scope(model_policy=ModelExecutionPolicy.standard())
    tool = registry.get("find_and_edit", scope=scope)

    assert tool is not None
    assert tool.definition.name == "find_and_edit"
    assert tool.definition.runs_in == "sandbox"
    assert tool.definition.read_only is False
    assert Capability.FILESYSTEM in tool.definition.needs
    assert "find_and_edit" not in (APPKIT_READ_TOOLS | APPKIT_MUTATORS | APPKIT_PROBES)
