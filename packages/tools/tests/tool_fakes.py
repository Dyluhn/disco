"""Fakes + builders for the Tool/Sandbox tests (tool-sandbox-contract.md §11).

Headless: the `process` backend (real subprocess) for sandbox tests, an
in-memory fake backend to prove backend-agnosticism, and a scripted Agent for
the §11.7 cross-contract gate. No Firecracker, no real network, no real provider.
"""

from __future__ import annotations

from disco.core import ToolCall
from disco.core.loop import AgentStep
from disco.tools.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxFileNotFoundError,
    SandboxSpec,
)
from disco.tools.sandbox.kernel import KernelResult


class FakeKernel:
    def __init__(self):
        self.restarts = 0

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        if "while True" in code:
            return KernelResult(ok=False, stdout="", stderr="", timed_out=True)
        return KernelResult(ok=True, stdout=f"[fake-kernel] {code}", stderr="")

    async def interrupt(self) -> None:
        pass

    async def restart(self) -> None:
        self.restarts += 1

    async def shutdown(self) -> None:
        pass


class FakeSandboxInstance:
    """An in-memory SandboxInstance — proves tools depend only on the protocol,
    not on the process backend (the §11.3 backend-swap test)."""

    def __init__(self, owner_id: str = "local", conversation_id: str = "conv") -> None:
        self.id = "fake-sbx"
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = SandboxSpec()
        self._fs: dict[str, bytes] = {}
        self._destroyed = False
        self._kernel = FakeKernel()

    @property
    async def kernel(self) -> FakeKernel:
        return self._kernel

    def _alive(self) -> None:
        if self._destroyed:
            raise SandboxError("instance is destroyed")

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self._alive()
        return ExecResult(exit_code=0, stdout=f"[fake] {cmd}", stderr="")

    async def read_file(self, path: str) -> bytes:
        self._alive()
        if path not in self._fs:
            raise SandboxFileNotFoundError(f"no such file: {path}")
        return self._fs[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._alive()
        self._fs[path] = data

    async def list_dir(self, path: str) -> list[str]:
        self._alive()
        return sorted(self._fs)

    async def file_exists(self, path: str) -> bool:
        self._alive()
        return path in self._fs

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return None

    async def destroy(self) -> None:
        self._destroyed = True


class ScriptedAgent:
    """Minimal loop Agent for the cross-contract gate: returns pre-scripted
    AgentSteps (repeats the last)."""

    def __init__(self, steps: list[AgentStep]) -> None:
        self._steps = list(steps)
        self.calls = 0

    async def step(
        self,
        view,
        tools,
        *,
        mode,
        overflow_signal,
        on_stream=None,
        temperature=None,
        assist=False,
        attempt: int = 1,
        provider_prefs=None,
    ) -> AgentStep:
        i = self.calls
        self.calls += 1
        return self._steps[min(i, len(self._steps) - 1)]


def call(tool_name: str, **arguments) -> ToolCall:
    return ToolCall(tool_name=tool_name, arguments=arguments)
