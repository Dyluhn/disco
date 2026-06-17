import hashlib
import re

from event_fakes import action, agent_error, observation, user_msg, with_seqs
from disco.core import DatasourceEvent, KnowledgeEvent, View


def test_small_old_observation_is_full():
    # _MASK_MIN_CHARS = 600
    obs = observation(content="small")
    # Make it old by adding many observations after it
    # _MASK_KEEP_RECENT = 8
    events = with_seqs([user_msg(), obs] + [observation(content="recent") for _ in range(10)])
    view = View.of(events)
    # obs is at index 1 (seq 2)
    # rp-12 B3 tail variation rotates observation surface wrappers by seq —
    # the masking contract is "content present, unmasked", not an exact format.
    assert "small" in view.messages[1].content
    assert "[older observation" not in view.messages[1].content

def test_large_recent_observation_is_full_but_snipped():
    # _MASK_KEEP_RECENT = 8
    # ObservationEvent.to_llm_message() calls snip_content (8000 chars)
    large_content = "A" * 10000
    obs = observation(content=large_content)
    events = with_seqs([user_msg(), obs])
    view = View.of(events)
    content = view.messages[1].content
    assert len(content) < 10000
    assert "[snipped" in content 
    assert "[masked" not in content

def test_large_old_observation_is_masked():
    large_content = "A" * 1000
    obs = observation(content=large_content, tool="test_tool")
    # seq will be 2
    events = with_seqs([user_msg(), obs] + [observation(content="recent") for _ in range(10)])
    view = View.of(events)
    
    content = view.messages[1].content
    # [masked output: {tool_name} #{seq} — {n_chars:,} chars, sha256:{hash12}.
    # Re-run the tool (or file_read the same path) to see it again.]
    digest = hashlib.sha256(large_content.encode()).hexdigest()[:12]
    expected_pattern = (
        rf"^\[masked output: test_tool #2 — 1,000 chars, sha256:{digest}\. "
        rf"Re-run the tool \(or file_read the same path\) to see it again\.\]$"
    )
    assert re.match(expected_pattern, content)

def test_agent_error_never_masked():
    # AgentErrorEvent with a 50KB body, old → NEVER masked.
    large_error = "E" * 50000
    err = agent_error(err=large_error)
    events = with_seqs([user_msg(), err] + [observation(content="recent") for _ in range(10)])
    view = View.of(events)
    # AgentErrorEvent is LLMConvertible. It should be at index 1.
    assert view.messages[1].content == f"ERROR: {large_error}"

def test_pinned_events_never_masked():
    large_content = "K" * 1000
    kevt = KnowledgeEvent(snippet=large_content)
    devt = DatasourceEvent(name="d", docs=large_content)
    events = with_seqs(
        [user_msg(), kevt, devt] + [observation(content="recent") for _ in range(10)]
    )
    view = View.of(events)
    assert large_content in view.messages[1].content
    assert large_content in view.messages[2].content
    assert "[masked" not in view.messages[1].content
    assert "[masked" not in view.messages[2].content

def test_masked_stub_preserves_tool_call_id():
    large_content = "A" * 1000
    obs = observation(content=large_content)
    # observation helper sets call_id="c"
    events = with_seqs([user_msg(), obs] + [observation(content="recent") for _ in range(10)])
    view = View.of(events)
    assert view.messages[1].tool_call_id == "c"

def test_action_events_untouched():
    large_thought = "T" * 1000
    act = action(thought=large_thought)
    events = with_seqs([user_msg(), act] + [observation(content="recent") for _ in range(10)])
    view = View.of(events)
    # ActionEvent.to_llm_message() renders the thought.
    assert large_thought in view.messages[1].content
