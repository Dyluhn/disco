"""Cross-contract acceptance gate — tool-sandbox-contract.md §11.7.

End-to-end with the `process` backend and fakes elsewhere: the loop proposes a
file_write then file_read (via the REAL executor) → observations pair by call_id
→ the agent reads back what it wrote. Then a search resolves via a fake
capability (provider key never in context). Proves the executor fulfills the
loop's ToolExecutor boundary and the secrets discipline holds end to end.
"""

from __future__ import annotations

from conftest import ScriptedAgent
from disco.core import (
    ActionEvent,
    ConversationStatus,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
)
from disco.core.llm import OperatingMode
from disco.core.loop import AgentLoop, AgentStep, NeverConfirm, NullSecurityAnalyzer
from disco.core.view import NoOpCondenser
from disco.tools import (
    CapabilityBroker,
    DefaultToolExecutor,
    InMemorySecretsStore,
    ProcessSandboxService,
    SandboxSpec,
    agent_scope,
    build_default_registry,
)

SECRET = "PROVIDER-KEY-DO-NOT-LEAK"


class _FakeSummarizer:
    async def summarize(self, messages):
        return "[summary]"


async def test_loop_drives_real_tools_with_secrets_held_out():
    store = SqliteEventStore(":memory:")
    cid = "conv"

    # Real sandbox (process) + real executor + a fake search capability.
    sandbox = await ProcessSandboxService().create(
        SandboxSpec(), owner_id="local", conversation_id=cid
    )
    secrets = InMemorySecretsStore({"SEARCH_KEY": SECRET})

    async def search_handler(*, query, limit=5):
        _ = secrets.get("SEARCH_KEY")  # used orchestrator-side only
        return [f"hit for {query}"]

    broker = CapabilityBroker()
    broker.register("search", search_handler)
    executor = DefaultToolExecutor(
        build_default_registry(), agent_scope(), sandbox=sandbox, broker=broker
    )

    agent = ScriptedAgent(
        [
            AgentStep(
                thought="write the file",
                tool_call=ToolCall(
                    tool_name="file_write", arguments={"path": "out.txt", "content": "hello world"}
                ),
                llm_response_id="req-1",
            ),
            AgentStep(
                thought="read it back",
                tool_call=ToolCall(tool_name="file_read", arguments={"path": "out.txt"}),
            ),
            AgentStep(
                thought="search the web",
                tool_call=ToolCall(tool_name="search", arguments={"query": "perplexity"}),
            ),
            AgentStep(thought="done", finished=True),
        ]
    )

    loop = AgentLoop(
        cid,
        store,
        agent,
        executor,
        None,
        NullSecurityAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        _FakeSummarizer(),
        mode=OperatingMode.INTERACTIVE,
    )

    await loop.send_message("do the task")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED

    events = await store.get_events(cid)
    actions = [e for e in events if isinstance(e, ActionEvent)]
    observations = [e for e in events if isinstance(e, ObservationEvent)]
    assert [a.tool_call.tool_name for a in actions] == ["file_write", "file_read", "search"]
    assert len(observations) == 3

    # Observations pair to their actions by call_id (the loop's correlation).
    for action, obs in zip(actions, observations, strict=True):
        assert obs.tool_result.call_id == action.tool_call.call_id

    # The agent read back exactly what it wrote (file_write → file_read round-trip).
    read_obs = observations[1]
    assert read_obs.tool_result.success
    assert "hello world" in read_obs.tool_result.content  # numbered read

    # The search resolved via the capability; the provider key never appears.
    search_obs = observations[2]
    assert search_obs.tool_result.success
    assert "perplexity" in search_obs.tool_result.content
    assert SECRET not in search_obs.tool_result.content

    # request_id flowed from the agent into the ActionEvent (cross-contract).
    assert actions[0].llm_response_id == "req-1"

    await sandbox.destroy()
