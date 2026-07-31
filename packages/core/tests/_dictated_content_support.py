"""Shared fixtures and helpers for the dictated-content finish-gate suite (REL-RC-O).

Split out of the former single test_dictated_content_finish_gate.py so each
thematic module stays under the architecture size cap. This module is
deliberately not named test_* so pytest does not collect it directly.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import ToolSpec
from disco.core.selection_edit import SourceSelectionRef, build_scoped_edit_directive
from loop_fakes import FakeExecutor, action_step


class _Sandbox:
    def __init__(self, root: Path) -> None:
        self.workspace_path = str(root)
        self._root = root
        self.read_paths: list[str] = []
        self.list_paths: list[str] = []

    def _target(self, path: str) -> Path:
        relative = path.removeprefix("/workspace/") if path.startswith("/workspace/") else path
        return self._root / relative

    async def file_exists(self, path: str) -> bool:
        return self._target(path).is_file()

    async def read_file(self, path: str) -> bytes:
        self.read_paths.append(path)
        return self._target(path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_dir(self, path: str) -> list[str]:
        self.list_paths.append(path)
        target = self._target(path)
        return [p.name for p in target.iterdir()]

    async def list_dir_bounded(
        self,
        path: str,
        limit: int,
    ) -> tuple[list[tuple[str, str]], bool]:
        names = await self.list_dir(path)
        entries: list[tuple[str, str]] = []
        for name in sorted(names)[:limit]:
            target = self._target(f"{path}/{name}")
            kind = (
                "other"
                if target.is_symlink()
                else "file"
                if target.is_file()
                else "directory"
                if target.is_dir()
                else "other"
            )
            entries.append((name, kind))
        return entries, len(names) > limit

    async def resolve_relpath(self, path: str) -> str:
        return self._target(path).resolve().relative_to(self._root.resolve()).as_posix()

    async def exec_shell(self, command: str, timeout_s: int = 5):  # noqa: ANN201, ARG002
        """Implement the DoD sandbox's non-empty-file evidence probe."""

        relative = shlex.split(command)[-1]
        target = self._target(relative)
        passed = target.is_file() and target.stat().st_size > 0
        return SimpleNamespace(
            exit_code=0 if passed else 1,
            stdout="",
            stderr="",
            timed_out=False,
        )


class _FSBuildExecutor(FakeExecutor):
    def __init__(self, root: Path) -> None:
        super().__init__(
            tools=[
                ToolSpec(name="submit_plan", description="plan", parameters_schema={}),
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="shell", description="shell", parameters_schema={}),
            ]
        )
        self.sandbox = _Sandbox(root)
        self.root = root

    async def execute(self, call):
        self.calls.append(call)
        if call.tool_name == "file_write":
            path = self.sandbox._target(str(call.arguments.get("path") or ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(call.arguments.get("content") or ""), encoding="utf-8")
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="ok",
        )


def _submit_plan(summary: str = "p") -> object:
    return action_step(
        "submit_plan",
        {
            "summary": summary,
            "steps": [
                {
                    "title": "write artifact",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "artifact.txt",
                    },
                }
            ],
        },
    )


def _submit_mixed_plan() -> object:
    return action_step(
        "submit_plan",
        {
            "summary": "text plus binary deliverables",
            "steps": [
                {
                    "title": "write artifact",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "artifact.txt",
                    },
                },
                {
                    "title": "include font",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "fonts/proof.woff2",
                    },
                },
                {
                    "title": "include opaque binary",
                    "done_condition": {
                        "kind": "file_exists",
                        "path": "assets/blob.dat",
                    },
                },
            ],
        },
    )


def _submit_absolute_multifile_plan() -> object:
    paths = [
        "/workspace/index.html",
        "/workspace/release/index.html",
        "/workspace/release/assets/theme.css",
        "/workspace/release/scripts/app.js",
    ]
    return action_step(
        "submit_plan",
        {
            "summary": "multi-file selected release",
            "steps": [
                {
                    "title": f"write {path}",
                    "done_condition": {"kind": "file_exists", "path": path},
                }
                for path in paths
            ],
        },
    )


def _write(content: str) -> object:
    return action_step("file_write", {"path": "artifact.txt", "content": content})


def _finish() -> object:
    return action_step("finish", {"summary": "done"})


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


def _user(text: str, seq: int) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=text),
    ).model_copy(update={"seq": seq})


def _plan(revision: int, seq: int) -> PlanEvent:
    return PlanEvent(summary=f"rev {revision}", steps=[], revision=revision).model_copy(
        update={"seq": seq}
    )


def _status(detail: str, seq: int) -> StatusEvent:
    return StatusEvent(status=ConversationStatus.RUNNING, detail=detail).model_copy(
        update={"seq": seq}
    )


def _scoped_edit_directive(
    *,
    human_label: str | None,
    instruction: str = "Change it to Midnight Coffee.",
) -> str:
    return build_scoped_edit_directive(
        SourceSelectionRef(oid="index.html:1", file="index.html", line=1),
        instruction,
        human_label=human_label,
    )

