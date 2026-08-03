"""W8 scripted-agent integration harness.

Wires the REAL agent loop + REAL event store + REAL sandbox (ProcessSandbox)
with a scripted model (``ScriptedProvider``) that replays a caller-supplied
sequence of tool calls.  The scripted model emits through the same
``DefaultLLMRouter`` → ``RouterAgent.step()`` boundary the production model
uses — NOT by calling internal functions directly.

Public API::

    result = await run_scripted(steps, timeout_s=30.0)
    # result["events"]       — list[Event] from the event store
    # result["final_status"] — ConversationStatus (FINISHED / ERROR / …)
    # result["workspace_path"] — str | None (host path to the sandbox workspace)

Surface and lifecycle
---------------------
Conversations are created on the **build** surface with ``artifact_mode=True``
(``OperatingMode.INTERACTIVE``, no plan gate).  This lets the simplest scripted
sequence be ``file_write → finish`` without a preceding ``submit_plan`` /
plan-approval round-trip.  Tests that need the full plan lifecycle can set
``artifact_mode=False`` (default build lifecycle) and include ``submit_plan`` +
``set_autonomous=True`` in their step sequence.

Watchdog
--------
``asyncio.wait_for`` wraps the loop task with a hard *timeout_s* ceiling.  A
hang (e.g. the loop stalls waiting for a model response that never arrives)
raises ``asyncio.TimeoutError`` and fails the test rather than blocking
forever.
"""

from __future__ import annotations

import asyncio
import uuid

from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.events import EventSource, LLMMessage, MessageEvent
from disco.core.llm import (
    DefaultLLMRouter,
    ModelEntry,
    RouterConfig,
)
from disco.tools import ProcessSandboxService

from .scripted_model import ScriptedProvider, Step

# A minimal RouterConfig that routes every role through our scripted provider.
_SCRIPTED_MODEL_ENTRY = ModelEntry(
    model_id="scripted-m",
    provider="scripted",
    context_window=8192,
)
_SCRIPTED_ROUTER_CONFIG = RouterConfig(
    models={"scripted-m": _SCRIPTED_MODEL_ENTRY},
    default_model="scripted-m",
)


def _build_runtime(
    store: SqliteEventStore,
    steps: list[Step],
) -> ConversationRuntime:
    """Build a ConversationRuntime wired with a scripted model and a real
    ProcessSandbox.  No network calls; no real LLM.  The runtime uses the
    injected router so the provider selection never reads from disk config."""
    provider = ScriptedProvider(steps)
    router = DefaultLLMRouter(
        _SCRIPTED_ROUTER_CONFIG,
        {"scripted": provider},
    )
    return ConversationRuntime(
        store,
        router=router,
        sandbox_service=ProcessSandboxService(),
    )


async def run_scripted(
    steps: list[Step],
    *,
    artifact_mode: bool = True,
    autonomous: bool = False,
    timeout_s: float = 30.0,
) -> dict:
    """Run the REAL agent loop with a scripted model and return evidence.

    Parameters
    ----------
    steps:
        Ordered list of ``(text, [ProposedToolCall, ...])`` tuples to replay.
        The last step is repeated if the loop calls the model more times than
        the script provides.
    artifact_mode:
        When ``True`` (default), the conversation uses ``OperatingMode.INTERACTIVE``
        (no plan gate): the model can call tools immediately, then call ``finish``
        to end the run.  Set to ``False`` for the full plan-first lifecycle (the
        scripted steps must include a ``submit_plan`` call and, if
        ``autonomous=False``, the test must call ``runtime.approve_plan(cid)``
        after the first kick).
    autonomous:
        When ``True``, the loop auto-approves the submitted plan (relevant only
        when ``artifact_mode=False``).
    timeout_s:
        Hard ceiling on how long to wait for the loop to reach a terminal
        status.  Raises ``asyncio.TimeoutError`` on breach so a hung test
        fails immediately rather than blocking the suite.

    Returns
    -------
    dict with keys:
        - ``events``: ``list[Event]`` — the full event log from the store
        - ``final_status``: ``ConversationStatus`` — the terminal status
        - ``workspace_path``: ``str | None`` — host path to the sandbox workspace
          (available when at least one sandbox tool was called; ``None`` if
          the run used no sandbox tools or the backend does not expose a host path)
        - ``runtime``: ``ConversationRuntime`` — for test-level inspection
        - ``cid``: ``str`` — the conversation id
    """
    cid = f"scripted-{uuid.uuid4().hex[:8]}"
    store = SqliteEventStore(":memory:")

    runtime = _build_runtime(store, steps)

    # Register the conversation.
    store.create_conversation(cid, owner_id="local", surface="build")
    runtime.set_surface(cid, "build")

    if artifact_mode:
        runtime.set_artifact_mode(cid, True)
    if autonomous:
        runtime.set_autonomous(cid, True)

    # Append the triggering user message that kicks the loop.
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="run the scripted test task"),
        ),
    )

    # Kick the loop; wait for it to reach a terminal status.
    runtime.run_controller.kick(cid)
    task = runtime._run_registry.task(cid)
    if task is None:
        raise RuntimeError(
            "runtime.run_controller.kick() did not schedule a task — check surface wiring"
        )

    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
    except TimeoutError:
        task.cancel()
        raise

    # Collect evidence.
    events = await store.get_events(cid)
    state = await store.get_state(cid)

    # Workspace path: executor holds the sandbox session; the session exposes
    # the host-side workspace root once at least one sandbox tool has run.
    executor = runtime._run_resources.executor(cid)
    sandbox = getattr(executor, "sandbox", None) if executor is not None else None
    workspace_path: str | None = getattr(sandbox, "workspace_path", None)

    return {
        "events": events,
        "final_status": state.execution_status,
        "workspace_path": workspace_path,
        "runtime": runtime,
        "cid": cid,
    }
