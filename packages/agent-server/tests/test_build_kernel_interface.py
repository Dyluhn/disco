"""BuildKernel seam.

Proves the seam is a ZERO-behavior-change wrapper:

  * the current Build runs through `DiscoKernel`, which is a thin pass-through to
    the SAME collaborators the agent-server already drives (the event store +
    `ControlOps` + `kick` + `ResumeService`), with IDENTICAL events/args;
  * the kernel selector resolves legacy/unknown persisted values to `DiscoKernel`;
  * `ConversationRuntime`'s public plan/action-gate ops (confirm / reject /
    approve_plan / request_plan) route THROUGH the active kernel and, with the
    default `disco` kernel, land on `ControlOps` exactly as before.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.build_kernel import (
    BuildKernel,
    BuildKernelKind,
    DiscoKernel,
    resolve_kernel_kind,
    select_kernel,
)
from disco.agent_server.routes._common import _context_message, _user_message
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


def _fake_runtime(store: SqliteEventStore, *, build_kernel: str = "disco") -> types.SimpleNamespace:
    """A SimpleNamespace standing in for `ConversationRuntime`, carrying ONLY the
    collaborators the kernel seam touches. The real `_kernel_for` + the real public
    control ops are bound onto it so the FULL routing chain (public op → _kernel_for
    → DiscoKernel → collaborator) is exercised against the production code."""
    fake = types.SimpleNamespace()
    fake._store = store
    fake.kick = MagicMock()
    fake._fold_contract_from_history = AsyncMock()
    fake._pinned_kernels = {}
    fake._run_generation = {}
    workspace_lock = asyncio.Lock()
    fake.workspace_lock = lambda _conversation_id: workspace_lock
    fake._test_workspace_lock = workspace_lock
    fake._workspace = MagicMock()

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

    fake._workspace.interprocess_mutation_fence = process_fence
    fake._workspace.append_run_ingress_locked = append_run_ingress

    control = MagicMock()
    control.confirm = AsyncMock()
    control.reject = AsyncMock()
    control.approve_plan = AsyncMock()
    control.request_plan = AsyncMock()
    control.pause = AsyncMock()
    control.cancel = AsyncMock()
    control.kill = AsyncMock()
    fake._control = control

    resume = MagicMock()
    resume.resume_conversation = AsyncMock()
    fake._resume = resume

    loop = MagicMock()
    loop.pick_alternative = AsyncMock()
    fake._loop_for = MagicMock(return_value=loop)

    cfg = MagicMock()
    cfg.build_kernel = build_kernel
    config_store = MagicMock()
    config_store.load.return_value = cfg
    fake._config_store = config_store

    fake._disco_kernel = DiscoKernel(fake)
    # Bind the REAL runtime methods so we test the shipped routing, not a copy.
    for name in (
        "_kernel_for",
        "_ensure_kernel_pinned",
        "_clear_pinned_kernel",
        "_unpin_if_current_generation",
        "_run_continuing_control",
    ):
        setattr(fake, name, types.MethodType(getattr(ConversationRuntime, name), fake))
    return fake


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


def test_select_kernel_picks_disco_for_every_value() -> None:
    disco = DiscoKernel(MagicMock())
    assert select_kernel(None, disco=disco, selected="disco") is disco
    assert select_kernel(None, disco=disco, selected="pi_experimental") is disco
    assert select_kernel(None, disco=disco, selected="pi") is disco
    assert select_kernel(None, disco=disco, selected="garbage") is disco


# ---- protocol conformance ----------------------------------------------------


def test_disco_kernel_satisfies_protocol() -> None:
    """`BuildKernel` is runtime_checkable; Disco occupies the seam."""
    assert isinstance(DiscoKernel(MagicMock()), BuildKernel)
    assert DiscoKernel.name == "disco"


# ---- DiscoKernel pass-through (zero behavior change) -------------------------


async def test_disco_kernel_control_ops_delegate_unchanged(store: SqliteEventStore) -> None:
    rt = _fake_runtime(store)
    k = rt._disco_kernel

    await k.confirm(CID)
    rt._control.confirm.assert_awaited_once_with(CID)

    await k.reject(CID, "nope")
    rt._control.reject.assert_awaited_once_with(CID, "nope")

    await k.approve_plan(CID)
    rt._control.approve_plan.assert_awaited_once_with(CID)

    await k.request_plan(CID, "do X")
    rt._control.request_plan.assert_awaited_once_with(CID, "do X")

    # Disco models plan rejection as a re-plan request carrying the revision.
    await k.reject_plan(CID, "redo")
    rt._control.request_plan.assert_awaited_with(CID, "redo")

    await k.pause(CID)
    rt._control.pause.assert_awaited_once_with(CID)
    await k.cancel(CID)
    rt._control.cancel.assert_awaited_once_with(CID)
    await k.kill(CID)
    # finding #4: the kernel-seam kill threads the captured run-generation (None here —
    # no run task bumped it) to the control op so it terminalizes ONLY its own run.
    rt._control.kill.assert_awaited_once_with(CID, None)

    await k.resume(CID)
    rt._resume.resume_conversation.assert_awaited_once_with(CID)

    await k.pick_alternative(CID, "opt-2")
    rt._loop_for.assert_called_with(CID)
    rt._loop_for.return_value.pick_alternative.assert_awaited_once_with("opt-2")


def test_disco_kernel_start_kicks(store: SqliteEventStore) -> None:
    rt = _fake_runtime(store)
    rt._disco_kernel.start(CID)
    rt.kick.assert_called_once_with(CID)


async def test_disco_kernel_send_user_turn_event_sequence(store: SqliteEventStore) -> None:
    """send_user_turn appends EXACTLY the events today's send path appends (a hidden
    context message then the user message) and kicks — same source/content/meta."""
    rt = _fake_runtime(store)
    await rt._disco_kernel.send_user_turn(CID, "hello there", context="big ctx", steer=True)

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
    rt.kick.assert_called_once_with(CID, claimed_user_seq=user.seq)


async def test_disco_kernel_send_user_turn_no_context(store: SqliteEventStore) -> None:
    rt = _fake_runtime(store)
    await rt._disco_kernel.send_user_turn(CID, "just text")
    msgs = [e for e in await store.get_events(CID) if hasattr(e, "message")]
    assert len(msgs) == 1
    assert msgs[0].source == EventSource.USER
    assert msgs[0].meta == {}
    assert any(
        isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.run-intent.user-turn"
        for event in await store.get_events(CID)
    )
    rt.kick.assert_called_once_with(CID, claimed_user_seq=msgs[0].seq)


async def test_verification_requirement_replacement_is_causal_and_atomic(
    store: SqliteEventStore,
) -> None:
    rt = _fake_runtime(store)
    first_directive = VerificationRequirementsDirective(
        claims=(
            VerificationRequestedClaim(
                claim_id="web.visual:quality",
                kind=VerificationClaimKind.VISUAL_SEMANTIC,
                expected="no visible layout defects",
            ),
        ),
    )
    first = await rt._disco_kernel.send_user_turn(
        CID,
        "Require a visual quality review.",
        verification_requirements=first_directive,
    )

    with pytest.raises(ValueError, match="must supersede the current snapshot"):
        await rt._disco_kernel.send_user_turn(
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
    replacement = await rt._disco_kernel.send_user_turn(
        CID,
        "Replace the scenario proof snapshot.",
        verification_requirements=VerificationRequirementsDirective(
            claims=(),
            supersedes_event_id=first.id,
        ),
    )

    assert replacement.verification_requirements is not None
    assert replacement.verification_requirements.supersedes_event_id == first.id
    assert rt.kick.call_count == 2


async def test_disco_kernel_turn_waits_for_host_workspace_fence(store: SqliteEventStore) -> None:
    rt = _fake_runtime(store)
    await rt._test_workspace_lock.acquire()
    sending = asyncio.create_task(
        rt._disco_kernel.send_user_turn(CID, "change the header", steer=True)
    )
    await asyncio.sleep(0)

    assert await store.get_events(CID) == []
    assert not sending.done()
    rt.kick.assert_not_called()

    rt._test_workspace_lock.release()
    stored = await sending
    events = await store.get_events(CID)
    assert stored in events
    assert any(getattr(event, "detail", None) == "revision_steer_pending" for event in events)
    rt.kick.assert_called_once_with(CID, claimed_user_seq=stored.seq)
    rt._workspace.claim_registered_run_locked.assert_called_once_with(CID)


# ---- runtime public ops route THROUGH the kernel ----------------------------


async def test_runtime_kernel_for_resolves_disco_by_default(store: SqliteEventStore) -> None:
    rt = _fake_runtime(store, build_kernel="disco")
    assert rt._kernel_for(CID) is rt._disco_kernel


@pytest.mark.parametrize("legacy", ["pi_experimental", "pi", "garbage"])
async def test_runtime_kernel_for_resolves_legacy_values_to_disco(
    legacy: str, store: SqliteEventStore
) -> None:
    rt = _fake_runtime(store, build_kernel=legacy)
    assert rt._kernel_for(CID) is rt._disco_kernel


async def test_runtime_control_ops_route_through_disco_kernel(store: SqliteEventStore) -> None:
    """With the default disco kernel, the public gate ops land on ControlOps with
    IDENTICAL args — observationally identical to before the seam."""
    rt = _fake_runtime(store, build_kernel="disco")

    await ConversationRuntime.confirm(rt, CID)
    rt._control.confirm.assert_awaited_once_with(CID)

    await ConversationRuntime.reject(rt, CID, "denied")
    rt._control.reject.assert_awaited_once_with(CID, "denied")

    await ConversationRuntime.approve_plan(rt, CID)
    rt._control.approve_plan.assert_awaited_once_with(CID)

    await ConversationRuntime.request_plan(rt, CID, "replan please")
    rt._control.request_plan.assert_awaited_once_with(CID, "replan please")
