import pytest
from disco.core import MessageEvent, StatusEvent
from disco.core.events import EventSource
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"

class RecordingAgent(ScriptedAgent):
    def __init__(self, steps):
        super().__init__(steps)
        self.captured_tools = []
    
    async def step(self, view, tools, **kwargs):
        self.captured_tools.append({t.name for t in tools if hasattr(t, "name")})
        return await super().step(view, tools, **kwargs)

@pytest.mark.asyncio
async def test_twelve_consecutive_reads_no_reminder_no_withholding():
    # 12 UNIQUE reads then 1 finish (to avoid StuckDetector)
    steps = [action_step(tool="file_read", args={"path": f"test{i}.txt"}) for i in range(12)]
    steps.append(finish_step())
    
    agent = RecordingAgent(steps)
    
    from disco.core.llm import ToolSpec
    tools = [
        ToolSpec(name="file_read", description="read", parameters_schema={}),
        ToolSpec(name="file_write", description="write", parameters_schema={}),
        ToolSpec(name="shell", description="shell", parameters_schema={}),
    ]
    executor = FakeExecutor(tools=tools)
    
    loop, store = build_loop(agent, executor=executor)
    await loop.send_message("start")
    await loop.run()
    
    events = await store.get_events(CID)
    
    # (a) ZERO MessageEvent(source=ENVIRONMENT) containing "STOP reading"
    reminders = [
        e for e in events 
        if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
        and "STOP reading" in (e.message.content if e.message else "")
    ]
    got = [r.message.content for r in reminders]
    assert len(reminders) == 0, f"Found unexpected read-streak reminders: {got}"
    
    # (b) the tool list the agent receives on the step AFTER the streak (step 13)
    # still contains every read tool it had on step 2. (Step 1 is the session's
    # first turn — the meta virtuals are withheld there BY DESIGN, not as a
    # read penalty; see _tools_for_step(suppress_meta_tools=...).)
    assert len(agent.captured_tools) == 13
    assert "file_read" in agent.captured_tools[0]
    assert "file_read" in agent.captured_tools[12]
    assert agent.captured_tools[1] == agent.captured_tools[12]

@pytest.mark.asyncio
async def test_stuck_escape_still_fires():
    # script 4 identical action→observation cycles. W1 raised the
    # repeat_action_observation threshold 3→4 (the OpenHands value), so genuine
    # spam now trips at 4 — the escape must STILL fire, just one cycle later.
    agent = ScriptedAgent([
        action_step(tool="shell", args={"command": "ls"}),
        action_step(tool="shell", args={"command": "ls"}),
        action_step(tool="shell", args={"command": "ls"}),
        action_step(tool="shell", args={"command": "ls"}),
        finish_step()
    ])
    
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()
    
    events = await store.get_events(CID)
    
    escapes = [e for e in events if isinstance(e, StatusEvent) and e.detail == "stuck_escape"]
    assert len(escapes) > 0
