from disco.core.events import ActionEvent, ObservationEvent, ToolCall, ToolResult
from disco.core.view import View


def test_tail_variation_deterministic():
    """B3: Variation is deterministic per seq."""
    action = ActionEvent(
        thought="I should read the file.",
        tool_call=ToolCall(tool_name="file_read", arguments={"path": "README.md"}),
        seq=1
    )
    obs = ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="file_read",
            success=True,
            content="File content"
        ),
        action_id=action.id,
        seq=2
    )
    
    view1 = View.of([action, obs])
    # seq 1 % 3 = 1 -> "Reasoning: ..."
    # seq 2 % 3 = 2 -> "Output: ..."
    assert view1.messages[0].content == "Reasoning: I should read the file."
    assert view1.messages[1].content == "Output: File content"
    
    # Same seqs should yield same surface forms
    view2 = View.of([action, obs])
    assert view2.messages[0].content == view1.messages[0].content
    assert view2.messages[1].content == view1.messages[1].content

def test_tail_variation_rotation():
    """B3: Variation rotates through variants."""
    actions = [
        ActionEvent(thought="T", tool_call=ToolCall(tool_name="N", arguments={}), seq=i)
        for i in range(3)
    ]
    view = View.of(actions)
    assert view.messages[0].content == "T"             # 0 % 3 = 0
    assert view.messages[1].content == "Reasoning: T"  # 1 % 3 = 1
    assert view.messages[2].content == "Thought: T"    # 2 % 3 = 2
