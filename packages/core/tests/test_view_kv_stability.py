import json
import subprocess
import sys

import pytest
from event_fakes import action, agent_msg, observation, user_msg, with_seqs
from disco.core import PlanEvent, View
from disco.core.events import PlanStep


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
        
        assert changes <= 1, (
            f"Too many changes at k={k}: {changes}. "
            f"Prev len: {len(fp_prev)}, Next len: {len(fp_next)}"
        )

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
    
    # The subprocess needs to be able to import disco.
    # We'll assume the environment is set up such that it's in sys.path.
    code = f"""
import json
import sys
from disco.core import View
from disco.core.events import TypeAdapter, Event
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


# ---- BP-00: latest-image-only view rule --------------------------------------

from disco.core.events import ObservationEvent, ToolResult  # noqa: E402


def _browser_obs(b64: str | None, n: int) -> ObservationEvent:
    """A successful browser observation; carries screenshot_b64 iff b64 is set."""
    structured: dict = {
        "url": "http://127.0.0.1:8000",
        "screenshot_path": f".pmx/screenshots/{n:04d}-navigate.png",
    }
    if b64 is not None:
        structured["screenshot_b64"] = b64
    return ObservationEvent(
        tool_result=ToolResult(
            call_id="c",
            tool_name="browser",
            success=True,
            content=(
                f"URL: http://127.0.0.1:8000\nscreenshot: .pmx/screenshots/{n:04d}-navigate.png"
            ),
            structured=structured,
        ),
        action_id="evt_x",
    )


def _image_messages(view):
    return [m for m in view.messages if m.images]


def test_latest_image_only(monkeypatch):
    """Only the LATEST screenshot-bearing browser observation renders an image;
    older ones render as path text only (images=None)."""
    monkeypatch.setenv("PMX_DRIVER_VISION", "1")
    events = with_seqs([
        user_msg("Start"),
        action(thought="browse 1", tool="browser", args={"action": "navigate"}),
        _browser_obs("AAA", 1),
        action(thought="browse 2", tool="browser", args={"action": "navigate"}),
        _browser_obs("BBB", 2),
    ])
    view = View.of(events)
    imaged = _image_messages(view)
    assert len(imaged) == 1, f"expected exactly one image-bearing message, got {len(imaged)}"
    assert imaged[0].images == ["data:image/png;base64,BBB"]
    # The OLDER observation still renders, text-only, with its screenshot path.
    older = [m for m in view.messages if "0001-navigate.png" in m.content]
    assert older and older[0].images is None


def test_no_images_when_gate_off(monkeypatch):
    """With the vision gate off, no rendered message carries images."""
    monkeypatch.delenv("PMX_DRIVER_VISION", raising=False)
    events = with_seqs([
        user_msg("Start"),
        action(thought="browse", tool="browser", args={"action": "navigate"}),
        _browser_obs("AAA", 1),
    ])
    assert _image_messages(View.of(events)) == []


def test_image_flip_fingerprint_stability(monkeypatch):
    """BP-00 invariant: appending a NEW screenshot observation changes at most ONE
    existing fingerprint element (the old image-bearing message flips image->text).
    Appends without a new screenshot change zero existing elements."""
    monkeypatch.setenv("PMX_DRIVER_VISION", "1")
    events = [user_msg("Start")]
    for i in range(12):
        events.append(action(thought=f"browse {i}", tool="browser", args={"action": "navigate"}))
        # Every other browse produces a screenshot; the rest are plain observations.
        events.append(_browser_obs("B64" + str(i) if i % 2 == 0 else None, i))
    events = with_seqs(events)

    for k in range(3, len(events)):
        fp_prev = View.of(events[:k]).fingerprint()
        fp_next = View.of(events[:k + 1]).fingerprint()
        common = min(len(fp_prev), len(fp_next))
        changes = sum(1 for i in range(common) if fp_prev[i] != fp_next[i])
        assert changes <= 1, (
            f"k={k}: {changes} mid-list fingerprint changes — the latest-image-only "
            "rule must flip at most one message per new screenshot"
        )
