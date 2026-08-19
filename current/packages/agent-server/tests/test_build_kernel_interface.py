"""BuildKernel zero-behavior-change seam.
The public facade and explicit control owner both route through pinned Disco kernels.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.build_kernel import (
    BuildKernel,
    BuildKernelKind,
    DiscoKernel,
    resolve_kernel_kind,
    select_kernel,
)
from disco.agent_server.conversation_control_service import ConversationControlService
from disco.agent_server.routes._common import _context_message, _user_message
from disco.agent_server.run_lifecycle_service import RunLifecycleService
from disco.agent_server.run_registry import (
    CancellationRegistry,
    KernelPinRegistry,
    KernelPinStore,
    RunRegistry,
)
from disco.agent_server.run_supervision_ports import DiscoKernelSelector
from disco.agent_server.runtime import ConversationRuntime
from disco.core import Event, EventSource, SqliteEventStore, WorkspaceMutationEvent
from disco.core.verification import (
    VerificationClaimKind,
    VerificationRequestedClaim,
    VerificationRequirementsDirective,
)

CID = "conv-build-kernel-test"


# ---- helpers -----------------------------------------------------------------


@pytest.fixture
def store() -> SqliteEventStore:
    s = SqliteEventStore(":memory:")
    s.create_conversation(CID, owner_id="local")
    return s


def _kernel_harness(store: SqliteEventStore) -> types.SimpleNamespace:
    """Construct the production kernel and control owner with explicit collaborators."""

    harness = types.SimpleNamespace()
    workspace_lock = asyncio.Lock()
    workspace = MagicMock()
    workspace.lock = lambda _conversation_id: workspace_lock

    @asynccontextmanager
    async def process_fence(_conversation_id: str) -> AsyncIterator[None]:
        yield

    async def append_run_ingress(
        conversation_id: str,
        events: list[Event],
        source: str,
    ) -> list[Event]:
        return await store.append_many(
            conversation_id,
            [*events, WorkspaceMutationEvent(operation=f"agent.run-intent.{source}")],
        )

    async def append_transition_batch_locked(
        conversation_id: str,
        events: list[Event],
        **_kwargs: Any,
    ) -> list[Event]:
        return await store.append_many(conversation_id, events)

    workspace.interprocess_mutation_fence = process_fence
    workspace.append_run_ingress_locked = append_run_ingress
    lifecycle = MagicMock()
    lifecycle.append_transition_batch_locked = append_transition_batch_locked

    controls = MagicMock()
    controls.confirm = AsyncMock()
    controls.reject = AsyncMock()
    controls.approve_plan = AsyncMock()
    controls.request_plan = AsyncMock()
    controls.pause = AsyncMock()
    controls.cancel = AsyncMock()
    controls.kill = AsyncMock()

    resume = MagicMock()
    resume.resume_conversation = AsyncMock()

    loop = MagicMock()
    loop.pick_alternative = AsyncMock()
    loops = MagicMock()
    loops.loop_for = MagicMock(return_value=loop)
    controller = MagicMock()
    controller.kick = MagicMock()
    runs = RunRegistry()
    pin_store = KernelPinStore()
    kernel = DiscoKernel._from_owners(
        store,
        workspace,
        lifecycle,
        controller,
        controls,
        loops,
        runs,
        pin_store,
        resume,
    )
    pins = KernelPinRegistry(DiscoKernelSelector(kernel), store=pin_store)
    contract = MagicMock()
    contract._fold_contract_from_history = AsyncMock()
    contract.has_declared_contract.return_value = False
    control = ConversationControlService(
        contract,
        pins,
        controls,
        resume,
    )
    run_lifecycle = RunLifecycleService(
        contract,
        pins,
        runs,
        CancellationRegistry(),
        controller,
        controls,
    )
    harness.kernel = kernel
    harness.control = control
    harness.run_lifecycle = run_lifecycle
    harness.controls = controls
    harness.controller = controller
    harness.contract = contract
    harness.lifecycle = lifecycle
    harness.loop = loop
    harness.loops = loops
    harness.pins = pins
    harness.resume = resume
    harness.runs = runs
    harness.workspace = workspace
    harness.workspace_lock = workspace_lock
    return harness


# ---- selector / policy (pure) ------------------------------------------------


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (None, "disco"),
        ("disco", "disco"),
        ("pi_experimental", "disco"),
        ("pi", "disco"),
        ("garbage", "disco"),
    ],
)
def test_resolve_kernel_kind(selected: str | None, expected: BuildKernelKind) -> None:
    assert resolve_kernel_kind(selected) == expected


def test_select_kernel_picks_disco_for_every_value(store: SqliteEventStore) -> None:
    disco = _kernel_harness(store).kernel
    assert select_kernel(None, disco=disco, selected="disco") is disco
    assert select_kernel(None, disco=disco, selected="pi_experimental") is disco
    assert select_kernel(None, disco=disco, selected="pi") is disco
    assert select_kernel(None, disco=disco, selected="garbage") is disco


# ---- protocol conformance ----------------------------------------------------


def test_disco_kernel_satisfies_protocol(store: SqliteEventStore) -> None:
    """`BuildKernel` is runtime_checkable; Disco occupies the seam."""
    assert isinstance(_kernel_harness(store).kernel, BuildKernel)
    assert DiscoKernel.name == "disco"


# ---- DiscoKernel pass-through (zero behavior change) -------------------------


async def test_disco_kernel_control_ops_delegate_unchanged(store: SqliteEventStore) -> None:
    rt = _kernel_harness(store)
    k = rt.kernel

    await k.confirm(CID)
    rt.controls.confirm.assert_awaited_once_with(CID)

    await k.reject(CID, "nope")
    rt.controls.reject.assert_awaited_once_with(CID, "nope")

    await k.approve_plan(CID)
    rt.controls.approve_plan.assert_awaited_once_with(CID)

    await k.request_plan(CID, "do X")
    rt.controls.request_plan.assert_awaited_once_with(CID, "do X")

    # Disco models plan rejection as a re-plan request carrying the revision.
    await k.reject_plan(CID, "redo")
    rt.controls.request_plan.assert_awaited_with(CID, "redo")

    await k.pause(CID)
    rt.controls.pause.assert_awaited_once_with(CID)
    await k.cancel(CID)
    rt.controls.cancel.assert_awaited_once_with(CID)
    await k.kill(CID)
    # finding #4: the kernel-seam kill threads the captured run-generation (None here —
    # no run task bumped it) to the control op so it terminalizes ONLY its own run.
    rt.controls.kill.assert_awaited_once_with(CID, None)

    rt.controller.kick.reset_mock()
    await rt.run_lifecycle.resume(CID)
    rt.controller.kick.assert_called_once_with(CID)
    rt.resume.resume_conversation.assert_not_awaited()

    await k.pick_alternative(CID, "opt-2")
    rt.loops.loop_for.assert_called_with(CID)
    rt.loop.pick_alternative.assert_awaited_once_with("opt-2")


def test_disco_kernel_start_kicks(store: SqliteEventStore) -> None:
    rt = _kernel_harness(store)
    rt.kernel.start(CID)
    rt.controller.kick.assert_called_once_with(CID)


async def test_disco_kernel_send_user_turn_event_sequence(store: SqliteEventStore) -> None:
    """send_user_turn appends EXACTLY the events today's send path appends (a hidden
    context message then the user message) and kicks — same source/content/meta."""
    rt = _kernel_harness(store)
    await rt.kernel.send_user_turn(CID, "hello there", context="big ctx", steer=True)

    events = await store.get_events(CID)
    msgs = [e for e in events if hasattr(e, "message")]
    # The seeded conversation has no prior messages; expect exactly the two appended.
    assert len(msgs) == 2
    ctx, user = msgs
    expected_ctx = _context_message("big ctx")
    expected_user = _user_message("hello there", steer=True)
    assert ctx.source == EventSource.ENVIRONMENT == expected_ctx.source
    assert ctx.message.content == "big ctx"
    assert user.source == EventSource.USER == expected_user.source
    assert user.message.content == "hello there"
    assert user.meta == expected_user.meta == {"steer": True}
    assert any(
        isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.run-intent.user-turn"
        for event in events
    )
    rt.controller.kick.assert_called_once_with(CID, claimed_user_seq=user.seq)


async def test_disco_kernel_send_user_turn_no_context(store: SqliteEventStore) -> None:
    rt = _kernel_harness(store)
    await rt.kernel.send_user_turn(CID, "just text")
    msgs = [e for e in await store.get_events(CID) if hasattr(e, "message")]
    assert len(msgs) == 1
    assert msgs[0].source == EventSource.USER
    assert msgs[0].meta == {}
    assert any(
        isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.run-intent.user-turn"
        for event in await store.get_events(CID)
    )
    rt.controller.kick.assert_called_once_with(CID, claimed_user_seq=msgs[0].seq)


async def test_verification_requirement_replacement_is_causal_and_atomic(
    store: SqliteEventStore,
) -> None:
    rt = _kernel_harness(store)
    first_directive = VerificationRequirementsDirective(
        claims=(
            VerificationRequestedClaim(
                claim_id="web.visual:quality",
                kind=VerificationClaimKind.VISUAL_SEMANTIC,
                expected="no visible layout defects",
            ),
        ),
    )
    first = await rt.kernel.send_user_turn(
        CID,
        "Require a visual quality review.",
        verification_requirements=first_directive,
    )

    with pytest.raises(ValueError, match="must supersede the current snapshot"):
        await rt.kernel.send_user_turn(
            CID,
            "stale clear",
            verification_requirements=VerificationRequirementsDirective(
                claims=(),
                supersedes_event_id="evt-foreign",
            ),
        )

    after_rejection = await store.get_events(CID)
    assert not any(
        getattr(event, "message", None) is not None and event.message.content == "stale clear"
        for event in after_rejection
    )
    replacement = await rt.kernel.send_user_turn(
        CID,
        "Replace the scenario proof snapshot.",
        verification_requirements=VerificationRequirementsDirective(
            claims=(),
            supersedes_event_id=first.id,
        ),
    )

    assert replacement.verification_requirements is not None
    assert replacement.verification_requirements.supersedes_event_id == first.id
    assert rt.controller.kick.call_count == 2


async def test_disco_kernel_turn_waits_for_host_workspace_fence(store: SqliteEventStore) -> None:
    rt = _kernel_harness(store)
    await rt.workspace_lock.acquire()
    sending = asyncio.create_task(rt.kernel.send_user_turn(CID, "change the header", steer=True))
    await asyncio.sleep(0)

    assert await store.get_events(CID) == []
    assert not sending.done()
    rt.controller.kick.assert_not_called()

    rt.workspace_lock.release()
    stored = await sending
    events = await store.get_events(CID)
    assert stored in events
    assert any(getattr(event, "detail", None) == "revision_steer_pending" for event in events)
    rt.controller.kick.assert_called_once_with(CID, claimed_user_seq=stored.seq)
    rt.workspace.claim_registered_run_locked.assert_called_once_with(CID)


# ---- conversation control routes THROUGH the pinned kernel -----------------


def test_kernel_pin_registry_resolves_disco(store: SqliteEventStore) -> None:
    rt = _kernel_harness(store)
    assert rt.pins.ensure(CID) is rt.kernel


async def test_runtime_kernel_for_resolves_disco_by_default(
    store: SqliteEventStore,
) -> None:
    runtime = ConversationRuntime(store)
    runtime.start(CID)
    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel


@pytest.mark.parametrize("legacy", ["pi_experimental", "pi", "garbage"])
def test_legacy_selection_values_pin_disco(legacy: str, store: SqliteEventStore) -> None:
    rt = _kernel_harness(store)
    selected = select_kernel(None, disco=rt.kernel, selected=legacy)
    assert selected is rt.kernel


@pytest.mark.parametrize("legacy", ["pi_experimental", "pi", "garbage"])
async def test_runtime_kernel_for_resolves_legacy_values_to_disco(
    legacy: str,
    store: SqliteEventStore,
) -> None:
    runtime = ConversationRuntime(store)
    selected = select_kernel(runtime, disco=runtime._disco_kernel, selected=legacy)
    assert selected is runtime._disco_kernel


async def test_conversation_control_ops_route_through_disco_kernel(
    store: SqliteEventStore,
) -> None:
    """The control owner pins Disco and preserves every gate argument."""
    rt = _kernel_harness(store)

    await rt.control.confirm(CID)
    rt.controls.confirm.assert_awaited_once_with(CID)

    await rt.control.reject(CID, "denied")
    rt.controls.reject.assert_awaited_once_with(CID, "denied")

    await rt.control.approve_plan(CID)
    rt.controls.approve_plan.assert_awaited_once_with(CID)

    await rt.control.request_plan(CID, "replan please")
    rt.controls.request_plan.assert_awaited_once_with(CID, "replan please")
    assert rt.pins.current(CID) is rt.kernel


async def test_runtime_control_ops_route_through_disco_kernel(
    store: SqliteEventStore,
) -> None:
    """The public compatibility facade preserves control routing and arguments."""
    runtime = ConversationRuntime(store)
    runtime._control.confirm = AsyncMock()
    runtime._control.reject = AsyncMock()
    runtime._control.approve_plan = AsyncMock()
    runtime._control.request_plan = AsyncMock()

    await runtime.confirm(CID)
    runtime._control.confirm.assert_awaited_once_with(CID)
    await runtime.reject(CID, "denied")
    runtime._control.reject.assert_awaited_once_with(CID, "denied")
    await runtime.approve_plan(CID)
    runtime._control.approve_plan.assert_awaited_once_with(CID)
    await runtime.request_plan(CID, "replan please")
    runtime._control.request_plan.assert_awaited_once_with(CID, "replan please")
    assert runtime._kernel_pins.current(CID) is runtime._disco_kernel
