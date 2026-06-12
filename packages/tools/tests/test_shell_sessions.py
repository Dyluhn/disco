import asyncio

import pytest
from disco.tools.sandbox.base import ExecResult
from disco.tools.sandbox.shell_sessions import SessionBusy, ShellSessionManager


class FakeInstance:
    def __init__(self):
        self.cmd_log = []
        self.canned_outputs = {}
        self.default_output = ("", "")
        self.default_exit_code = 0
        self.capture_pane_calls = 0
    
    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.cmd_log.append(cmd)
        
        if "capture-pane" in cmd:
            self.capture_pane_calls += 1
            if "capture-pane" in self.canned_outputs:
                if isinstance(self.canned_outputs["capture-pane"], list):
                    val = self.canned_outputs["capture-pane"].pop(0) if self.canned_outputs["capture-pane"] else self.canned_outputs["capture-pane_default"]
                    return ExecResult(exit_code=val[0], stdout=val[1], stderr="", timed_out=False)
                
        for k, v in self.canned_outputs.items():
            if k in cmd and k != "capture-pane":
                if isinstance(v, list):
                    val = v.pop(0) if v else self.canned_outputs.get(f"{k}_default", (0, ""))
                    return ExecResult(exit_code=val[0], stdout=val[1], stderr="", timed_out=False)
                else:
                    exit_code, stdout = v
                    return ExecResult(exit_code=exit_code, stdout=stdout, stderr="", timed_out=False)
                
        if "capture-pane" in cmd and "capture-pane" in self.canned_outputs:
             val = self.canned_outputs["capture-pane"]
             return ExecResult(exit_code=val[0], stdout=val[1], stderr="", timed_out=False)
        
        return ExecResult(exit_code=self.default_exit_code, stdout=self.default_output[1], stderr="", timed_out=False)

@pytest.mark.asyncio
async def test_marker_parse_exit_0():
    inst = FakeInstance()
    async def get_inst(): return inst
    manager = ShellSessionManager(get_inst)
    
    # Fake ensure
    inst.canned_outputs["has-session"] = (0, "")
    
    # Fake view (busy -> not busy)
    inst.canned_outputs["capture-pane_default"] = (0, "__PMX_PS1__0__$ ")
    inst.canned_outputs["capture-pane"] = [
        (0, "__PMX_PS1__0__$ "), # view
        (0, "__PMX_PS1__0__$ "), # view in is_busy
        (0, "__PMX_PS1__0__$ "), # pre_cap
        (0, "__PMX_PS1__0__$ \necho hi\nhi\n__PMX_PS1__0__$ ") # post_cap
    ]
    
    view = await manager.view("main")
    assert not view.running
    
    out = await manager.exec("main", "echo hi", None)
    assert out.running == False
    assert out.exit_code == 0
    assert out.output == "echo hi\nhi"

@pytest.mark.asyncio
async def test_marker_parse_exit_7():
    inst = FakeInstance()
    async def get_inst(): return inst
    manager = ShellSessionManager(get_inst)
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane_default"] = (0, "__PMX_PS1__0__$ ")
    inst.canned_outputs["capture-pane"] = [
        (0, "__PMX_PS1__0__$ "), # view in is_busy
        (0, "__PMX_PS1__0__$ "), # pre_cap
        (0, "__PMX_PS1__0__$ \nexit 7\n__PMX_PS1__7__$ ") # post_cap
    ]
    
    out = await manager.exec("main", "exit 7", None)
    assert out.running == False
    assert out.exit_code == 7
    assert out.output == "exit 7"

@pytest.mark.asyncio
async def test_busy_detection():
    inst = FakeInstance()
    async def get_inst(): return inst
    manager = ShellSessionManager(get_inst)
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane"] = (0, "sleep 10\n")
    
    view = await manager.view("main")
    assert view.running == True
    
    # test SessionBusy verbatim message
    with pytest.raises(SessionBusy, match="Previous command not finished in session 'main'. Wait for it \\(shell_wait\\), interact with it \\(shell_write_to_process\\), kill it \\(shell_kill_process\\), or use a different session name."):
        await manager.exec("main", "ls", None)

@pytest.mark.asyncio
async def test_kill_then_recreate():
    inst = FakeInstance()
    async def get_inst(): return inst
    manager = ShellSessionManager(get_inst)
    inst.canned_outputs["has-session"] = [(0, ""), (1, "")]
    inst.canned_outputs["has-session_default"] = (0, "")
    inst.canned_outputs["capture-pane"] = (0, "sleep 10\n")
    
    res = await manager.kill_foreground("main")
    assert "Process ignored Ctrl-C; session 'main' was killed and recreated." in res
    assert "tmux kill-session" in " ".join(inst.cmd_log)
    assert "tmux new-session" in " ".join(inst.cmd_log)


@pytest.mark.asyncio
async def test_session_lost_after_recreate():
    """After a sandbox recreate, viewing a previously-known session must say WHY
    it is gone — not the generic 'not found'."""
    inst = FakeInstance()
    async def get_inst(): return inst
    manager = ShellSessionManager(get_inst)

    # Session 'main' becomes known (idle prompt visible).
    inst.canned_outputs["has-session"] = (1, "")
    inst.canned_outputs["capture-pane"] = (0, "__PMX_PS1__0__$ ")
    await manager.ensure("main")
    assert "main" in manager._known_sessions

    # Sandbox dies and is recreated -> manager is told; tmux on the new box
    # has no such session (capture-pane now fails).
    manager.reset_known_sessions()
    inst.canned_outputs["capture-pane"] = (1, "can't find session")

    view = await manager.view("main")
    assert view.running is False
    assert view.output == "session lost: sandbox was recreated"

    # A session that was NEVER known still gets the generic message.
    view2 = await manager.view("other")
    assert "Session not found or error" in view2.output


import urllib.request

from disco.tools.sandbox.process import ProcessSandboxService


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_scenarios():
    service = ProcessSandboxService()
    spec = None # unused in ProcessSandboxService create
    inst = await service.create(spec=None, owner_id="test", conversation_id="conv-int")
    
    try:
        async def get_inst(): return inst
        manager = ShellSessionManager(get_inst, namespace="conv-int-")
        
        # a) state persists
        out1 = await manager.exec("main", "x=42; echo started", None)
        assert out1.running == False
        
        out2 = await manager.exec("main", "echo $x", None)
        assert out2.running == False
        assert out2.output.strip() == "42"
        
        # b) start server, curl, kill
        out_srv = await manager.exec("srv", "python3 -m http.server 8123", None)
        assert out_srv.running == True
        
        # give it a second to start
        await asyncio.sleep(1)
        
        view_srv = await manager.view("srv")
        assert "Serving HTTP" in view_srv.output
        
        # verify from test
        req = urllib.request.Request("http://127.0.0.1:8123")
        with urllib.request.urlopen(req) as response:
            assert response.status == 200
        
        # actually curl 127.0.0.1:8123
        kill_res = await manager.kill_foreground("srv")
        assert "idle" in kill_res or "killed" in kill_res
        
        # c) busy session
        out_read = await manager.exec("main", "read -p 'name? ' n && echo hi-$n", None)
        assert out_read.running == True
        
        await manager.write("main", "dylan", press_enter=True)
        
        # Wait for it to finish
        view_read = await manager.wait("main", 5)
        assert view_read.running == False
        assert "hi-dylan" in view_read.output
        
        # d) SessionBusy
        out_busy = await manager.exec("main2", "sleep 20", None)
        assert out_busy.running == True
        
        with pytest.raises(SessionBusy):
            await manager.exec("main2", "echo nope", None)
            
    finally:
        await inst.destroy()
