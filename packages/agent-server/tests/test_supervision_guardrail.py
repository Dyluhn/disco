"""W10 — static guardrail locking in W2 (loop-task supervision).

A future refactor of ``kick()`` that drops the done-callback would silently re-open the
silent-hang: an exception escaping ``loop.run()`` would again leave the conversation at
RUNNING forever. This asserts the supervision wiring stays in place. (W2's behavioural tests
prove it WORKS; this proves it can't be quietly deleted.)
"""

from __future__ import annotations

import inspect

import pytest
from disco.agent_server.runtime import ConversationRuntime

pytestmark = pytest.mark.boundary_contract


def test_kick_wires_the_supervision_callback():
    src = inspect.getsource(ConversationRuntime.kick)
    assert "add_done_callback" in src, "kick() must register a done-callback on the loop task"
    assert "_on_run_task_done" in src, "kick() must route task completion through the supervisor"


def test_supervisor_terminalizes_to_error():
    """The supervisor method must emit a terminal ERROR (not just log) on an uncaught crash."""
    src = inspect.getsource(ConversationRuntime._terminalize_crashed)
    assert "ConversationStatus.ERROR" in src, "a crashed run must terminalize to ERROR"


def test_clean_return_is_reconciled():
    """W11: a CLEAN run-task return (no exception) must be routed through finalization —
    not silently ignored — so a return at RUNNING can't sit there forever."""
    src = inspect.getsource(ConversationRuntime._on_run_task_done)
    assert "_finalize_clean_return" in src, (
        "_on_run_task_done must reconcile a clean (exception-free) return, not just exceptions"
    )


def test_finalizer_terminalizes_stall_to_stuck():
    """W11: the clean-return finalizer must mark a non-terminal (RUNNING) stall STUCK."""
    src = inspect.getsource(ConversationRuntime._finalize_clean_return)
    assert "ConversationStatus.STUCK" in src, "a wedged RUNNING run must terminalize to STUCK"
