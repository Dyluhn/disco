"""REL-3 ToolScope audit mode.

Audit mode is deny-log only: default build loops get an observe-only contract
guard when DISCO_TOOLSCOPE_AUDIT=1, but no tool is blocked by that guard.
"""

from __future__ import annotations

from unittest import mock

import pytest
from disco.agent_server.runtime import (
    ConversationRuntime,
    toolscope_audit_enabled,
)
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import DefaultLLMRouter, ModelExecutionPolicy
from disco.core.loop import RouterAgent
from disco.tools import AGENT_TOOLS, DefaultToolExecutor, agent_scope, build_default_registry
from disco.tools.sandbox import ExecResult, SandboxError, SandboxSpec


class _MemSandbox:
    id = "mem-sbx"
    owner_id = "local"

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.spec = SandboxSpec()
        self.files: dict[str, bytes] = {}

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise SandboxError(f"no such file: {path}")
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def list_dir(self, path: str) -> list[str]:
        return sorted(self.files)

    async def file_exists(self, path: str) -> bool:
        return path in self.files

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return None

    async def destroy(self) -> None:
        pass


def _runtime() -> ConversationRuntime:
    return ConversationRuntime(SqliteEventStore(":memory:"))


def _compose(rt: ConversationRuntime, cid: str):
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)
    with mock.patch.object(rt, "_sandbox_service_now"):
        return rt._compose_build_loop(cid, router, agent)


def test_toolscope_audit_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCO_TOOLSCOPE_AUDIT", raising=False)
    monkeypatch.delenv("PMX_TOOLSCOPE_AUDIT", raising=False)
    assert toolscope_audit_enabled() is False

    monkeypatch.setenv("DISCO_TOOLSCOPE_AUDIT", "on")
    assert toolscope_audit_enabled() is True

    monkeypatch.setenv("DISCO_TOOLSCOPE_AUDIT", "0")
    assert toolscope_audit_enabled() is False


def test_flag_off_default_build_keeps_existing_executor_wiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCO_TOOLSCOPE_AUDIT", raising=False)
    monkeypatch.delenv("PMX_TOOLSCOPE_AUDIT", raising=False)
    rt = _runtime()
    cid = "audit_off_default"
    rt.set_surface(cid, "build")

    loop = _compose(rt, cid)

    assert loop.executor._scope.allowed_tools == AGENT_TOOLS
    assert loop.executor._scope_guard is None
    assert loop.executor._on_tool_success is None
    assert cid not in rt._build_trackers
    assert cid not in rt._build_audit_trackers


@pytest.mark.asyncio
async def test_flag_on_default_build_uses_observe_guard_without_artifact_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_TOOLSCOPE_AUDIT", "1")
    rt = _runtime()
    cid = "audit_on_default"
    rt.set_surface(cid, "build")
    rt._store.create_conversation(cid, surface="build")
    await rt._store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="build a static site"),
        ),
    )

    guard, on_success = rt._build_scope_audit_guard(cid)

    assert rt._effective_artifact_mode(cid) is False
    assert guard is not None
    assert on_success is not None
    assert cid not in rt._build_trackers
    assert rt._build_audit_trackers[cid][0].kind.value == "static.site"


@pytest.mark.asyncio
async def test_flag_on_would_deny_file_write_in_edit_but_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DISCO_TOOLSCOPE_AUDIT", "1")
    rt = _runtime()
    cid = "audit_write_edit"
    rt.set_surface(cid, "build")
    guard, on_success = rt._build_scope_audit_guard(cid)
    executor = DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=_MemSandbox(cid),
        conversation_id=cid,
        scope_guard=guard,
        on_tool_success=on_success,
    )

    with caplog.at_level("INFO", logger="disco.agent_server.runtime"):
        first = await executor.execute(
            _call("file_write", path="index.html", content="<h1>one</h1>")
        )
        # CONTRACT-ACTIVATE repin (2026-07-10): the CUSTOM escape hatch now
        # carries the full working set, so a second file_write in EDIT is
        # legitimately ALLOWED. The would-deny case that remains real under
        # CUSTOM is a CROSS-KIND mutator (deck/doc/app_* tools stay excluded).
        second = await executor.execute(
            _call("doc_set_section", section="intro", title="Two", body="two")
        )

    assert first.success is True
    denies = [
        r for r in caplog.records if getattr(r, "event", "") == "toolscope_audit_would_deny"
    ]
    assert len(denies) == 1
    assert getattr(denies[0], "conversation") == cid
    assert getattr(denies[0], "phase") == "edit"
    assert getattr(denies[0], "tool") == "doc_set_section"
    assert "out of contract scope" in getattr(denies[0], "reason")


@pytest.mark.asyncio
async def test_terminal_summary_emitted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DISCO_TOOLSCOPE_AUDIT", "1")
    rt = _runtime()
    cid = "audit_summary"
    rt.set_surface(cid, "build")
    guard, on_success = rt._build_scope_audit_guard(cid)
    executor = DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=_MemSandbox(cid),
        conversation_id=cid,
        scope_guard=guard,
        on_tool_success=on_success,
    )
    await executor.execute(_call("file_write", path="index.html", content="one"))
    await executor.execute(_call("doc_set_section", section="intro", title="Two", body="two"))

    class _DoneLoop:
        async def run(self):
            await rt._store.append(
                cid,
                StatusEvent(status=ConversationStatus.FINISHED),
            )
            return await rt._store.get_state(cid)

    rt._store.create_conversation(cid, surface="build")
    with (
        mock.patch.object(rt, "_preflight_driver", return_value=None),
        mock.patch.object(rt, "_preflight_sandbox", return_value=None),
        mock.patch.object(rt, "_maybe_rehydrate", return_value=None),
        mock.patch.object(rt, "_rematerialize_uploads", return_value=None),
        mock.patch.object(rt, "_maybe_shadow_fold_finished_manifest", return_value=None),
        mock.patch.object(rt, "_maybe_snapshot", return_value=None),
        caplog.at_level("INFO", logger="disco.agent_server.runtime"),
    ):
        await rt._run_with_persistence(cid, _DoneLoop())

    summaries = [
        r for r in caplog.records if getattr(r, "event", "") == "toolscope_audit_summary"
    ]
    assert len(summaries) == 1
    assert getattr(summaries[0], "conversation") == cid
    assert getattr(summaries[0], "total_tools") == 2
    assert getattr(summaries[0], "would_denies_by_phase") == {"edit": 1}


def _call(tool_name: str, **arguments):
    from disco.core import ToolCall

    return ToolCall(tool_name=tool_name, arguments=arguments)
