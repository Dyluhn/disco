import json

import pytest
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.server import ServerStatusArgs, ServerStatusTool
from disco.tools.sandbox.base import ExecResult

pytestmark = pytest.mark.asyncio


class MockSandbox:
    def __init__(self, proc_dir: str):
        self.proc_dir = proc_dir

        async def _list(*args, **kwargs):
            return []

        self.sessions = type("S", (), {"list": _list, "namespace": ""})()

    async def exec_shell(self, cmd: str, *args, **kwargs) -> ExecResult:
        return ExecResult(exit_code=0, stdout=self._run_probe_logic(cmd), stderr="")

    def _run_probe_logic(self, cmd: str) -> str:
        # Extract ports from cmd: "python3 -c ... <port1> <port2> ..."
        # The ports are the trailing space-separated integers.
        parts = cmd.split()
        ports = [int(p) for p in parts if p.isdigit()]

        results = []
        for port in ports:
            if port == 8000:
                results.append(
                    {
                        "port": 8000,
                        "pid": 1234,
                        "cmdline": "python3 -m http.server 8000",
                        "session": "disco-preview",
                    }
                )
            elif port == 3000:
                results.append(
                    {"port": 3000, "pid": 5678, "cmdline": "node server.js", "session": "disco-api"}
                )
            else:
                results.append({"port": port, "pid": None})
        return json.dumps(results)


def _ctx(sb) -> ToolContext:
    return ToolContext(
        sandbox=sb,
        workspace_path="/work",
        timeout_s=30,
        capabilities={Capability.SHELL},
        owner_id="o",
        conversation_id="c",
    )


async def test_server_status_formatting(tmp_path):
    # This tests the formatting logic in ServerStatusTool.run
    sb = MockSandbox(str(tmp_path))

    # Override list_sessions to return a preview session
    async def _list(*args, **kwargs):
        return [type("S", (), {"name": "preview", "busy": True, "last_lines": "Serving at 8000"})()]

    sb.sessions.list = _list

    out = await ServerStatusTool().run(ServerStatusArgs(), _ctx(sb))
    assert out.success
    assert "SERVER STATUS" in out.content
    assert "- preview: running — last: Serving at 8000" in out.content
    assert (
        "- 8000: OWNED by pid 1234 (python3 -m http.server 8000) [session: preview]" in out.content
    )
    assert "- 3000: OWNED by pid 5678 (node server.js) [session: api]" in out.content
    assert "- 5173: FREE" in out.content


async def test_server_status_free(tmp_path):
    sb = MockSandbox(str(tmp_path))
    # Return all ports as free
    sb._run_probe_logic = lambda cmd: json.dumps(
        [{"port": int(p), "pid": None} for p in cmd.split() if p.isdigit()]
    )

    out = await ServerStatusTool().run(ServerStatusArgs(), _ctx(sb))
    assert out.success
    assert "- 8000: FREE" in out.content
    assert "- 3000: FREE" in out.content
