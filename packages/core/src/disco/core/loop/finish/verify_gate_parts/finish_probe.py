"""The verify-on-finish shell-probe gate (`finish_verify_passed`).

Owns: the early AppKit/workflow bypasses, running the agent-attached verify
command through the same hard-deny/confirm gate as any action, and telling a
malformed verify CARRIER (command-not-found / SyntaxError) apart from a real
task failure.
"""

from __future__ import annotations

from typing import Any, cast

from ... import signals
from ..common import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ToolCall,
)
from .probe_events import latest_event_for_action


def _appkit_finish_verify_bypass(gate: Any) -> bool:
    # Strict AppKit has no raw shell surface. Its authoritative finish
    # verification is the structured `verify_appkit_app` gate that runs
    # later in the shared finish path, so the model-authored shell probe is
    # a redundant impossible check here.
    return getattr(getattr(gate._loop, "executor", None), "appkit_phase", None) is not None


async def _workflow_finish_verify_shell_guard(gate: Any) -> bool:
    """True (and reminded) when this sealed workflow does not grant `shell`.

    Defense in depth for stale clients and model hallucinations. The
    workflow finish schema omits `verify` in this shape, but arguments
    are still untrusted: never let the virtual finish tool smuggle a
    raw shell action outside the approved workflow scope.
    """

    workflow_run = getattr(gate._loop, "_workflow_run", None)
    workflow_tools = tuple(getattr(getattr(workflow_run, "definition", None), "tools", ()))
    if workflow_run is None or "shell" in workflow_tools:
        return False
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    "The supplied finish verification was ignored because this "
                    "sealed workflow does not grant the shell tool. This workflow "
                    "grants: "
                    f"{', '.join(workflow_tools) if workflow_tools else 'no tools'}. "
                    "Host-owned workflow output checks remain authoritative.\n"
                    "</system-reminder>"
                ),
            ),
            meta={"workflow_finish_verify_ignored": True},
        )
    )
    return True


def _verify_command_malformed(passed: bool, obs: Event | None) -> bool:
    """The verify CARRIER is broken (not the deliverable).

    Malformed = command-not-found (127) or an interpreter SyntaxError. A
    non-zero exit from an unrunnable check is NOT evidence the task failed —
    the carrier was bad. Uses the real exit code, NOT a regex over output:
    "exit 1, 127 tests failed" is a REAL failure, not a malformed carrier.
    Only applies when the shell actually RAN the command and reported it (an
    ObservationEvent) — an AgentErrorEvent means the executor raised BEFORE
    any observation, which is an environmental failure, not a malformed verify.
    """

    if passed or not isinstance(obs, ObservationEvent):
        return False
    st = obs.tool_result.structured or {}
    ec = st.get("exit_code")
    exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
    low = f"{obs.tool_result.content or ''} {obs.tool_result.error or ''}".lower()
    return (
        exit_code == 127
        or "syntaxerror" in low
        or (exit_code is None and "command not found" in low)
        or (exit_code is None and ": not found" in low)
    )


async def _refuse_verify_command(
    gate: Any, action: ActionEvent, call: ToolCall, error: str
) -> None:
    await gate._loop._emit(action)
    await gate._loop._emit(
        AgentErrorEvent(
            error=error,
            action_id=action.id,
            tool_call_id=call.call_id,
        )
    )


async def finish_verify_passed(gate: Any, command: str) -> tuple[bool, bool]:
    """Run the agent's stated acceptance check before allowing `finish`
    (verify-on-finish post-condition gate). The agent attaches a shell
    command to finish whose exit 0 means the deliverable is good; we run it,
    VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
    the real problem instead of declaring a broken build complete.

    The verify command is NOT privileged: it passes the same hard-deny gate
    AND the same confirmation policy as any action. A command that would
    normally require confirmation is refused here (we don't silently run a
    gated command as a 'verification') — the agent is told to run it as an
    ordinary, gated action first. Ordinary test/build/lint checks assess as
    MEDIUM and run unimpeded. Returns (passed, malformed): `passed` is True
    iff the check ran and passed; `malformed` is True iff the verify command
    itself is broken (command-not-found / SyntaxError) rather than the task.
    """

    if _appkit_finish_verify_bypass(gate):
        return True, False

    if await _workflow_finish_verify_shell_guard(gate):
        return True, False

    call = ToolCall(tool_name="shell", arguments={"command": command})
    # meta marker: this shell action is the GATE'S probe, not the agent's
    # work. Phase-B re-run #6 (2026-06-10): an unmarked probe counted as a
    # real action in _actions_since_last_resume, so a refused first-move
    # finish UNLOCKED the withheld meta tools and the model remember-spammed
    # straight into the valve. The probe must never flip fresh-session.
    action = ActionEvent(
        thought=f"Verifying completion: {command}",
        tool_call=call,
        meta={"verify_probe": True},
    )

    deny = signals.hard_deny_reason(action)
    if deny is not None:
        await _refuse_verify_command(
            gate,
            action,
            call,
            (
                "<system-reminder>\n"
                f"The verify command attached to finish is hard-denied ({deny}); it "
                "will not run. Provide a safe verify command, or finish without one.\n"
                "</system-reminder>"
            ),
        )
        return False, False

    risk = gate._loop.analyzer.assess(action)
    if gate._loop.policy.should_confirm(risk):
        await _refuse_verify_command(
            gate,
            action,
            call,
            (
                "<system-reminder>\n"
                f"The verify command attached to finish (`{command}`) assessed "
                f"{getattr(risk, 'level', risk)} and needs confirmation to run, so "
                "it won't be executed silently as a verification. Next move: run "
                "that exact command as a normal action first (it will go through "
                "the confirm gate), then finish.\n"
                "</system-reminder>"
            ),
        )
        return False, False

    action = cast(ActionEvent, await gate._loop._emit(action))
    await gate._loop._execute_and_observe(action)
    # Find the observation correlated to THIS verify action (robust against a
    # trailing sandbox-restart notice that _execute_and_observe may append).
    obs = await latest_event_for_action(gate._loop, action.id)
    passed = isinstance(obs, ObservationEvent) and obs.tool_result.success
    malformed = _verify_command_malformed(passed, obs)
    return passed, malformed
