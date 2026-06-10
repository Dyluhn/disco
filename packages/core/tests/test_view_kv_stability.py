import sys
import subprocess
import json

import pytest
from conftest import action, observation, user_msg, agent_msg, with_seqs
from perpleximanus.core import View, PlanEvent
from perpleximanus.core.events import PlanStep

def test_view_append_stability():
    # Build 60 synthetic events
    events = [user_msg("Start")]
    events.append(PlanEvent(summary="Goal", steps=[PlanStep(title="Step 1")], revision=1))
    for i in range(58):
        if i % 3 == 0:
            events.append(agent_msg(f"Thinking {i}"))
        elif i % 3 == 1:
            events.append(action(thought=f"Action {i}", tool="shell", args={"cmd": "ls"}))
        else:
            # Add some large observations to trigger masking
            # seq will be assigned by with_seqs.
            # Observation #1 is at index 4 (seq 5) if we count:
            # 0: user_msg (seq 1)
            # 1: PlanEvent (seq 2)
            # 2: agent_msg (seq 3)
            # 3: action (seq 4)
            # 4: observation (seq 5)
            content = "Result " + str(i) + ("A" * 1000 if i % 6 == 2 else "")
            events.append(observation(content=content))
    
    events = with_seqs(events)
    
    for k in range(30, 60):
        view_prev = View.of(events[:k])
        view_next = View.of(events[:k+1])
        
        fp_prev = view_prev.fingerprint()
        fp_next = view_next.fingerprint()
        
        # Strip tail recitation from both if present
        if fp_prev and view_prev.messages[-1].content.startswith("<current-objective>"):
            fp_prev = fp_prev[:-1]
        if fp_next and view_next.messages[-1].content.startswith("<current-objective>"):
            fp_next = fp_next[:-1]
            
        # fp_prev should be a prefix of fp_next, EXCEPT for at most 1 changed element (masking flip)
        common_len = min(len(fp_prev), len(fp_next))
        changes = 0
        for i in range(common_len):
            if fp_prev[i] != fp_next[i]:
                changes += 1
        
        assert changes <= 1, f"Too many changes at k={k}: {changes}. Prev len: {len(fp_prev)}, Next len: {len(fp_next)}"

def test_view_subprocess_roundtrip():
    # Test that View.fingerprint is stable across processes
    events = with_seqs([
        user_msg("Start"), 
        PlanEvent(summary="Goal", steps=[PlanStep(title="Step 1")], revision=1),
        observation(content="A"*1000)
    ])
    
    fp_parent = View.of(events).fingerprint()
    
    # Serialize events to JSON using model_dump(mode="json")
    events_json = json.dumps([e.model_dump(mode="json") for e in events])
    
    # The subprocess needs to be able to import perpleximanus.
    # We'll assume the environment is set up such that it's in sys.path.
    code = f"""
import json
import sys
from perpleximanus.core import View
from perpleximanus.core.events import TypeAdapter, Event
try:
    events_data = json.loads({events_json!r})
    adapter = TypeAdapter(list[Event])
    events = adapter.validate_python(events_data)
    print(json.dumps(View.of(events).fingerprint()))
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(f"Subprocess failed: {result.stderr}")
    
    fp_child = json.loads(result.stdout)
    assert fp_parent == fp_child
