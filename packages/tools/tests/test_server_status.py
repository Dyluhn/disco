import json
import os
import pytest
from perpleximanus.tools.anatomy import Capability, ToolContext
from perpleximanus.tools.builtin.server import ServerStatusTool, ServerStatusArgs
from perpleximanus.tools.sandbox.base import ExecResult
from perpleximanus.tools.sandbox.port_owner import port_owner

pytestmark = pytest.mark.asyncio

class MockSandbox:
    def __init__(self, proc_dir: str):
        self.proc_dir = proc_dir
        async def _list(*args, **kwargs):
            return []
        self.sessions = type('S', (), {'list': _list, 'namespace': ''})()

    async def exec_shell(self, cmd: str, *args, **kwargs) -> ExecResult:
        return ExecResult(exit_code=0, stdout=self._run_probe_logic(cmd), stderr="")

    def _run_probe_logic(self, cmd: str) -> str:
        # Extract port from cmd: "python3 -c ... <port>"
        port = int(cmd.split()[-1])
        
        if port == 8000:
            return json.dumps({
                "port": 8000,
                "pid": 1234,
                "cmdline": "python3 -m http.server 8000",
                "session": "preview"
            })
        return json.dumps({"port": port, "pid": None})

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
        return [type('S', (), {'name': 'preview', 'busy': True, 'last_lines': 'Serving at 8000'})()]
    sb.sessions.list = _list
    
    out = await ServerStatusTool().run(ServerStatusArgs(), _ctx(sb))
    assert out.success
    assert "SERVER STATUS" in out.content
    assert "- preview: running — last: Serving at 8000" in out.content
    assert "- 8000: OWNED by pid 1234 (python3 -m http.server 8000) [session: preview]" in out.content

async def test_server_status_free(tmp_path):
    sb = MockSandbox(str(tmp_path))
    sb._run_probe_logic = lambda cmd: json.dumps({"port": 8000, "pid": None})
    
    out = await ServerStatusTool().run(ServerStatusArgs(), _ctx(sb))
    assert out.success
    assert "- 8000: FREE" in out.content
