
import pytest
from disco.core.events import AgentErrorEvent, LLMMessage
from disco.core.llm.openai_provider import OpenAIProvider, _sanitize_tool_name
from disco.core.llm.types import ToolSpec
from loop_fakes import ScriptedAgent, action_step, build_loop


def test_tool_name_sanitization():
    """DEFECT-6: tool names must be sanitized (no dots)."""
    assert _sanitize_tool_name("sandbox.shell") == "shell"
    assert _sanitize_tool_name("file_read") == "file_read"
    assert _sanitize_tool_name("my.tool.with.dots") == "dots"
    assert _sanitize_tool_name("tool!@#") == "tool"

def test_tool_call_repair_and_coercion():
    """Rung 5: JSON repair and type coercion."""
    provider = OpenAIProvider("http://localhost")
    
    # 1. JSON Repair
    raw_json = "```json\n{\"arg\": \"val\",}\n```"
    repaired = provider._repair_json(raw_json)
    assert repaired == "{\"arg\": \"val\"}"
    
    # 2. Type Coercion
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "flag": {"type": "boolean"}
        }
    }
    args = {"count": "42", "flag": "true", "other": "val"}
    coerced = provider._coerce_args(args, schema)
    assert coerced["count"] == 42
    assert coerced["flag"] is True
    assert coerced["other"] == "val"

def test_tool_calls_mapping_back():
    """Rung 5/DEFECT-6: Sanitized names map back to original specs."""
    provider = OpenAIProvider("http://localhost")
    tools = [ToolSpec(name="sandbox.shell", description="...", parameters_schema={})]
    
    # Model returns sanitized name
    raw_calls = [{"function": {"name": "shell", "arguments": "{}"}}]
    calls = provider._tool_calls(raw_calls, tools=tools)
    
    assert len(calls) == 1
    assert calls[0].tool_name == "sandbox.shell" # Mapped back!

def test_agent_error_serialization_defense():
    """F1 (c): Regression test for tool_call_id defense in OpenAIProvider."""
    provider = OpenAIProvider("http://localhost")

    # 1. AgentErrorEvent WITHOUT tool_call_id -> should be downgraded to user role
    err_no_id = AgentErrorEvent(error="boom", action_id="a1", tool_call_id=None)
    msg_no_id = err_no_id.to_llm_message()
    serialized_no_id = provider._message(msg_no_id)

    assert serialized_no_id["role"] == "user"
    assert "Tool error:" in serialized_no_id["content"]
    assert "tool_call_id" not in serialized_no_id

    # 2. AgentErrorEvent WITH tool_call_id -> should remain tool role
    err_with_id = AgentErrorEvent(error="boom", action_id="a1", tool_call_id="call_123")
    msg_with_id = err_with_id.to_llm_message()
    serialized_with_id = provider._message(msg_with_id)

    assert serialized_with_id["role"] == "tool"
    assert serialized_with_id["tool_call_id"] == "call_123"
    assert "ERROR: boom" in serialized_with_id["content"]

@pytest.mark.asyncio
async def test_tool_calls_sanitization_both_ways():
    """F3: DEFECT-6: Sanitize both ways (outgoing and incoming)."""
    provider = OpenAIProvider("http://localhost")
    tools = [ToolSpec(name="sandbox.shell", description="...", parameters_schema={})]

    # 1. Outgoing: Assistant tool_call in history should be sanitized
    history_msg = LLMMessage(
        role="assistant",
        content="thinking",
        tool_calls=[{"id": "c1", "name": "sandbox.shell", "arguments": {}}]
    )

    serialized = provider._message(history_msg)
    assert serialized["tool_calls"][0]["function"]["name"] == "shell" # Sanitized!

    # 2. Incoming: Model returns sanitized name, should map back to original
    raw_response_calls = [
        {"id": "c2", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
    ]
    parsed_calls = provider._tool_calls(raw_response_calls, tools=tools)

    assert len(parsed_calls) == 1
    assert parsed_calls[0].tool_name == "sandbox.shell" # Mapped back!

def test_tool_call_xml_leak_defense():
    """E3: Regression test for stray </parameter> (or other XML tags) at the end of JSON."""
    provider = OpenAIProvider("http://localhost")
    raw_json = '{"command": "ls"}</parameter>'
    
    # Verify _repair_json handles it
    repaired = provider._repair_json(raw_json)
    import json
    parsed = json.loads(repaired)
    assert parsed == {"command": "ls"}

    # Verify _tool_calls handles it (integrated test)
    raw_calls = [{"function": {"name": "shell", "arguments": raw_json}}]
    calls = provider._tool_calls(raw_calls)
    assert calls[0].arguments == {"command": "ls"}

@pytest.mark.asyncio
async def test_exhausted_scripted_agent_during_requery():
    """DC-05: an exhausted ScriptedAgent in a requery must not hang.
    If the script is too short for the requery attempts, it fall through
    or errors out gracefully (the 'exhausted' turn)."""
    # 1. Agent returns an unknown tool "unknown_me", triggering requery.
    # 2. ScriptedAgent only has ONE step. Requery calls it again.
    # 3. ScriptedAgent repeats the last step (exhausted).
    # 4. Requery continues for 2 attempts, then breaks and emits.
    # 5. This should NOT HANG.
    agent = ScriptedAgent([
        action_step("unknown_me"),
    ])
    # unknown_me is not in virtual_names, and FakeExecutor only has shell.
    loop, store = build_loop(agent)
    await loop.send_message("go")
    
    # This must not hang. It will requery 2x, then eventually emit unknown_me.
    import asyncio
    try:
        await asyncio.wait_for(loop.run(), timeout=5.0)
    except TimeoutError:
        pytest.fail("AgentLoop.run() hung on exhausted ScriptedAgent during requery")
