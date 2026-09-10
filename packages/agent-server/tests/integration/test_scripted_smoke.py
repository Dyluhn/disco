"""W8 smoke test — scripted-agent integration harness (one end-to-end scenario).

Proves the harness works end-to-end:
  1. The scripted model emits a ``file_write`` tool call through the REAL
     ``DefaultLLMRouter`` → ``RouterAgent.step()`` → ``DefaultToolExecutor``
     → ``ProcessSandbox`` boundary.
  2. Then emits a ``finish`` call (intercepted by the loop, not the executor).
  3. The run reaches ``FINISHED`` within the timeout.
  4. The written file exists on the host filesystem at the reported
     ``workspace_path``.
  5. The event log contains at least one ``ActionEvent`` (for the file_write)
     and at least one ``ObservationEvent`` (the sandbox result), proving the
     tool call actually executed through the real sandbox rather than being
     short-circuited.

This scenario does NOT test failure paths (missing-file sandbox errors,
crash-supervisor, etc.) — those are W8 follow-on scenarios.  The sole
purpose here is verifying that the harness wiring is correct before
depending on it in other tests.
"""

from __future__ import annotations

import os

import pytest
from disco.core.events import ActionEvent, ConversationStatus, ObservationEvent
from disco.core.llm import ProposedToolCall

from .harness import run_scripted


@pytest.mark.asyncio
@pytest.mark.integration
async def test_file_write_then_finish_reaches_finished() -> None:
    """Smoke: scripted file_write → finish lands in FINISHED with real sandbox."""
    output_path = "smoke_output.txt"
    output_content = "hello from the scripted harness"

    steps = [
        # Step 1: write a file through the real sandbox.
        (
            "writing the output file",
            [
                ProposedToolCall(
                    tool_name="file_write",
                    arguments={"path": output_path, "content": output_content},
                )
            ],
        ),
        # Step 2: finish the run (intercepted by the loop as the affirmative
        # terminal move; never reaches the executor).
        (
            "task complete",
            [
                ProposedToolCall(
                    tool_name="finish",
                    arguments={"summary": "wrote output file"},
                )
            ],
        ),
    ]

    result = await run_scripted(steps, timeout_s=30.0)

    # ── 1. Terminal status ────────────────────────────────────────────────────
    assert result["final_status"] == ConversationStatus.FINISHED, (
        f"Expected FINISHED, got {result['final_status']}. "
        f"Events: {[type(e).__name__ for e in result['events']]}"
    )

    # ── 2. Event log contains the real tool execution ─────────────────────────
    events = result["events"]
    action_events = [e for e in events if isinstance(e, ActionEvent)]
    obs_events = [e for e in events if isinstance(e, ObservationEvent)]

    assert action_events, "No ActionEvent in the event log — tool call was not recorded"
    assert obs_events, (
        "No ObservationEvent in the event log — "
        "tool call may not have executed through the real sandbox"
    )

    # The file_write action should be first.
    first_action = action_events[0]
    assert first_action.tool_call is not None
    assert first_action.tool_call.tool_name == "file_write", (
        f"Expected first action to be file_write, got {first_action.tool_call.tool_name!r}"
    )

    # ── 3. File exists in the sandbox workspace ───────────────────────────────
    workspace = result["workspace_path"]
    assert workspace is not None, (
        "workspace_path is None — the sandbox did not create a host-side workspace. "
        "Check that ProcessSandboxService is wired correctly."
    )

    host_path = os.path.join(workspace, output_path)
    assert os.path.exists(host_path), (
        f"Written file not found at {host_path!r}. workspace_path={workspace!r}"
    )

    with open(host_path) as f:
        actual_content = f.read()
    assert actual_content == output_content, (
        f"File content mismatch: expected {output_content!r}, got {actual_content!r}"
    )
