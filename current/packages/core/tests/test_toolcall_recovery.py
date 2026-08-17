from disco.core.llm.toolcall_recovery import recover_tool_calls


def test_recover_hermes_format():
    content = '<tool_call>{"name": "foo", "arguments": {"x": 1}}</tool_call>'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_recover_fenced_json():
    content = '```json\n{"name": "foo", "arguments": {"x": 1}}\n```'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_recover_fenced_tool_call():
    content = '```tool_call\n{"name": "foo", "arguments": {"x": 1}}\n```'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_recover_bare_json_object():
    content = '{"name": "foo", "arguments": {"x": 1}}'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_recover_bare_json_array_multicall():
    content = '[{"name": "foo", "arguments": {}}, {"name": "bar", "arguments": {"y": 2}}]'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 2
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {}
    assert calls[1].tool_name == "bar"
    assert calls[1].arguments == {"y": 2}


def test_recover_trailing_comma():
    content = '{"name": "foo", "arguments": {"x": 1,}}'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_recover_reasoning_content_only():
    reasoning = '<tool_call>{"name": "foo", "arguments": {"x": 1}}</tool_call>'
    calls = recover_tool_calls(None, reasoning)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_recover_function_shape():
    content = '{"function": {"name": "foo", "arguments": {"x": 1}}}'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_recover_tool_args_shape():
    content = '{"tool": "foo", "args": {"x": 1}}'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_malformed_json_returns_empty():
    content = '{"name": "foo", "arguments": {"x": 1}'
    calls = recover_tool_calls(content, None)
    assert calls == []


def test_prose_only_returns_empty():
    content = "Here is the plan. I will do this and that."
    calls = recover_tool_calls(content, None)
    assert calls == []


def test_mixed_content_with_hermes():
    content = 'I am thinking... <tool_call>{"name": "foo", "arguments": {"x": 1}}</tool_call> done.'
    calls = recover_tool_calls(content, None)
    assert len(calls) == 1
    assert calls[0].tool_name == "foo"
    assert calls[0].arguments == {"x": 1}


def test_both_content_and_reasoning():
    reasoning = '<tool_call>{"name": "foo", "arguments": {"x": 1}}</tool_call>'
    content = '{"name": "bar", "arguments": {"y": 2}}'
    calls = recover_tool_calls(content, reasoning)
    # Should probably extract both or deduplicate? The prompt says
    # "Scan BOTH content and reasoning_content."
    # We will just extract all.
    assert len(calls) >= 1
    tool_names = [c.tool_name for c in calls]
    assert "foo" in tool_names or "bar" in tool_names
