"""CXT-5: browser console/network truncation is now RECOVERABLE — the full
diagnostics spill to a workspace file named in the observation (no destructive loss)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

from disco.core.llm import ModelExecutionPolicy
from disco.tools.sandbox.base import ExecResult
from disco.core.observations import scan_for_destructive_elision
from disco.tools import DefaultToolExecutor, agent_scope
from disco.tools.builtin import build_default_registry

from tool_fakes import FakeSandboxInstance, call


class _PageSandbox(FakeSandboxInstance):
    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__()
        self._data = data
        self.execs: list[str] = []
        self.files: dict[str, bytes] = {}
        self.sessions = AsyncMock()

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.execs.append(cmd)
        if "health" in cmd:
            return ExecResult(exit_code=0, stdout="OK", stderr="")
        if "POST" in cmd:
            return ExecResult(exit_code=0, stdout=json.dumps(self._data), stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")


def _exec(sandbox: _PageSandbox) -> DefaultToolExecutor:
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=sandbox,
    )


async def test_browser_diagnostics_spill_when_over_cap() -> None:
    data = {
        "ok": True,
        "url": "http://x.example",
        "title": "Lots of errors",
        "text": "page",
        "elements": [],
        # over the caps (_MAX_CONSOLE_LINES=40, _MAX_NETWORK_LINES=20)
        "console": [{"type": "error", "text": f"err {i}", "stack": ""} for i in range(80)],
        "network": [{"url": f"http://x/{i}", "status": 500} for i in range(30)],
    }
    sandbox = _PageSandbox(data)
    res = await _exec(sandbox).execute(call("browser", action="navigate", url="http://x.example"))
    assert res.success
    # a spill file was written with the FULL diagnostics
    spills = [p for p in sandbox.files if p.startswith(".disco-spill-browser-")]
    assert len(spills) == 1
    full = json.loads(sandbox.files[spills[0]].decode())
    assert len(full["console"]) == 80 and len(full["network"]) == 30
    # the observation names the spill + tells how to recover
    assert spills[0] in res.content
    assert "file_read" in res.content
    # nothing destructive (no-recover) remains in the observation
    assert scan_for_destructive_elision(res.content) == []


async def test_browser_no_spill_when_under_cap() -> None:
    data = {
        "ok": True,
        "url": "http://x.example",
        "title": "ok",
        "text": "page",
        "elements": [],
        "console": [{"type": "error", "text": "one", "stack": ""}],
        "network": [],
    }
    sandbox = _PageSandbox(data)
    res = await _exec(sandbox).execute(call("browser", action="navigate", url="http://x.example"))
    assert res.success
    assert not [p for p in sandbox.files if p.startswith(".disco-spill-browser-")]
