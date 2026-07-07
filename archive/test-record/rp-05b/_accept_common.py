"""Shared scaffolding for the RP-05b rung-B §6 live-acceptance drills.

These drills exercise the REAL stack — real OpenRouter FREE model (the freeze
investigation forbids the local llama-server), real subprocess / SDK MCP servers,
real container isolation, real egress proxy — driving the composed Build loop end
to end. Each drill imports from here so the model/key/event-trace plumbing is
written once and identically to how the live agent-server (`:8000`) wires itself.

Run a drill from the repo root, e.g.:
    uv run python test-record/rp-05b/drill2_stdio_process.py

The OpenRouter key is sourced exactly as the live server's launch does — via
`harness/marathon/_or_key.py` into `PMX_OPENROUTER_API_KEY` — so the runtime's
`_router_now` env-overlay authenticates the FREE `or-gpt-oss-120b-free` driver
without the key ever touching disk in plaintext.
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Run every drill logically from the repo root: the stdio MCP fakes are spawned
# as `python -c "from packages.tools.tests.mcp_fakes import …"`, which only
# resolves when the subprocess inherits cwd == repo root (its `-c` puts cwd on
# sys.path). Also makes disco-config.json / harness paths resolve.
os.chdir(REPO_ROOT)


def source_openrouter_key() -> None:
    """Set PMX_OPENROUTER_API_KEY the way the live server's launch line does."""
    if os.environ.get("PMX_OPENROUTER_API_KEY"):
        return
    key = subprocess.check_output(
        [sys.executable, "harness/marathon/_or_key.py"], cwd=REPO_ROOT
    ).decode().strip()
    if not key.startswith("sk-or-"):
        raise SystemExit("could not source a FREE OpenRouter key from _or_key.py")
    os.environ["PMX_OPENROUTER_API_KEY"] = key


# Source the key at import so every drill is wired before it builds a runtime.
source_openrouter_key()


from disco.core import (  # noqa: E402  (key must be set before import chain)
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
)


def user_event(content: str) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER, message=LLMMessage(role="user", content=content)
    )


def get(d, k):
    return d.get(k) if isinstance(d, dict) else None


def print_new(events: list, shown: int) -> int:
    """Stream new events to stdout in the proof-of-life trace style."""
    for e in events[shown:]:
        if isinstance(e, MessageEvent):
            print(f"  · {e.source.value}: {e.message.content.strip()[:160]}")
        elif isinstance(e, ActionEvent):
            tc = e.tool_call
            name = tc.tool_name if tc else "(finish)"
            args = tc.arguments if tc else {}
            print(f"  → ACTION {name} {str(args)[:140]}")
            if e.thought:
                print(f"      thought: {e.thought.strip()[:120]}")
        elif isinstance(e, ObservationEvent):
            r = e.tool_result
            ex = get(r.structured, "exit_code")
            print(f"      ✓ obs (exit {ex}): {r.content.strip()[:160]!r}")
        elif isinstance(e, AgentErrorEvent):
            print(f"      ✗ agent-error: {e.error[:160]}")
        elif isinstance(e, ErrorEvent):
            print(f"  [FATAL] {str(getattr(e, 'error', e))[:300]}")
        elif isinstance(e, StatusEvent):
            print(f"  [status] {e.status.value}{(' · ' + e.detail) if e.detail else ''}")
    return len(events)


TERMINAL = (
    ConversationStatus.FINISHED,
    ConversationStatus.ERROR,
    ConversationStatus.IDLE,
    ConversationStatus.STUCK,
)


async def drive(
    runtime,
    store,
    cid: str,
    *,
    max_rounds: int = 24,
    step_timeout: float = 240.0,
    on_confirm=None,
):
    """Drive a Build conversation to a terminal state, auto-approving the plan
    gate and (by default) auto-approving every risky-action confirm gate — the
    human decision points, logged as they fire. `on_confirm(events, state)` may
    override the gate handling (return "confirm" | "reject"); default confirms.

    Returns (final_state, events, counters) where counters tracks plan_gates,
    confirm_gates, actions.
    """
    import asyncio

    shown = 0
    counters = {"plan_gates": 0, "confirm_gates": 0, "actions": 0}
    for _round in range(max_rounds):
        runtime.kick(cid)
        task = runtime._tasks.get(cid)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=step_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                print("  [TIMEOUT waiting for a step]")
                break
        events = await store.get_events(cid)
        shown = print_new(events, shown)
        counters["actions"] = sum(
            1 for e in events if isinstance(e, ActionEvent) and e.tool_call
        )
        state = await store.get_state(cid)
        if state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL:
            counters["plan_gates"] += 1
            print("  [PLAN GATE] approving plan")
            await runtime.approve_plan(cid)
            continue
        if state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION:
            counters["confirm_gates"] += 1
            decision = "confirm"
            if on_confirm is not None:
                decision = on_confirm(events, state) or "confirm"
            print(f"  [CONFIRM GATE] {decision}")
            if decision == "reject":
                await runtime.reject(cid)
            else:
                await runtime.confirm(cid)
            continue
        if state.execution_status in TERMINAL:
            break

    final = await store.get_state(cid)
    return final, await store.get_events(cid), counters
