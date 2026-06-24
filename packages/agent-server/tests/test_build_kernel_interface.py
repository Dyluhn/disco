"""BuildKernel seam — Disco Pi Build Kernel Campaign, PR A1/A2.

Proves the seam introduced in A1/A2 is a ZERO-behavior-change wrapper:

  * the current Build runs through `DiscoKernel`, which is a thin pass-through to
    the SAME collaborators the agent-server already drives (the event store +
    `ControlOps` + `kick` + `ResumeService`), with IDENTICAL events/args;
  * the kernel selector resolves `disco` → `DiscoKernel` and `pi_experimental`
    → the `PiKernel` stub ONLY when the experimental flag is on (else it
    downgrades to `disco`, never routing a real build through the stub);
  * `ConversationRuntime`'s public plan/action-gate ops (confirm / reject /
    approve_plan / request_plan) route THROUGH the active kernel and, with the
    default `disco` kernel, land on `ControlOps` exactly as before.

The experimental flag is the env var `DISCO_PI_KERNEL_EXPERIMENTAL` (read via
`disco_env`, suffix `PI_KERNEL_EXPERIMENTAL`).
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server.build_kernel import (
    BuildKernel,
    BuildKernelKind,
    BuildKernelPolicy,
    DiscoKernel,
    PiKernel,
    experimental_kernels_enabled,
    resolve_kernel_kind,
    select_kernel,
)
from disco.agent_server.routes._common import _context_message, _user_message
from disco.agent_server.runtime import ConversationRuntime
from disco.core import EventSource, SqliteEventStore

CID = "conv-build-kernel-test"
EXP_ENV = "DISCO_PI_KERNEL_EXPERIMENTAL"


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
    fake._pi_kernel = PiKernel(fake)

    # Bind the REAL runtime methods so we test the shipped routing, not a copy.
    fake._kernel_for = types.MethodType(ConversationRuntime._kernel_for, fake)
    return fake


# ---- selector / policy (pure) ------------------------------------------------


@pytest.mark.parametrize(
    ("selected", "flag", "expected"),
    [
        (None, False, "disco"),
        ("disco", False, "disco"),
        ("disco", True, "disco"),
        ("pi_experimental", False, "disco"),  # gated off → downgrade
        ("pi_experimental", True, "pi_experimental"),  # gated on → honored
        ("garbage", False, "disco"),  # unknown/stale → disco
        ("garbage", True, "disco"),
    ],
)
def test_resolve_kernel_kind(
    monkeypatch: pytest.MonkeyPatch, selected: str | None, flag: bool, expected: BuildKernelKind
) -> None:
    if flag:
        monkeypatch.setenv(EXP_ENV, "1")
    else:
        monkeypatch.delenv(EXP_ENV, raising=False)
    assert resolve_kernel_kind(selected) == expected


def test_experimental_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    assert experimental_kernels_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "True", "yes", "on"])
def test_experimental_flag_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv(EXP_ENV, val)
    assert experimental_kernels_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "off", "no"])
def test_experimental_flag_falsy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv(EXP_ENV, val)
    assert experimental_kernels_enabled() is False


def test_policy_effective_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    assert BuildKernelPolicy.resolve("pi_experimental").effective_kind == "disco"
    monkeypatch.setenv(EXP_ENV, "1")
    p = BuildKernelPolicy.resolve("pi_experimental")
    assert p.selected == "pi_experimental"
    assert p.experimental_enabled is True
    assert p.effective_kind == "pi_experimental"
    # An unknown persisted value normalizes to disco regardless of the flag.
    assert BuildKernelPolicy.resolve("nope").effective_kind == "disco"


def test_select_kernel_picks_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    disco = DiscoKernel(MagicMock())
    pi = PiKernel(MagicMock())
    monkeypatch.delenv(EXP_ENV, raising=False)
    assert select_kernel(None, disco=disco, pi=pi, selected="disco") is disco
    assert select_kernel(None, disco=disco, pi=pi, selected="pi_experimental") is disco
    monkeypatch.setenv(EXP_ENV, "1")
    assert select_kernel(None, disco=disco, pi=pi, selected="pi_experimental") is pi
    assert select_kernel(None, disco=disco, pi=pi, selected="disco") is disco


# ---- protocol conformance ----------------------------------------------------


def test_both_kernels_satisfy_protocol() -> None:
    """`BuildKernel` is runtime_checkable; both implementations conform so either
    can occupy the seam."""
    assert isinstance(DiscoKernel(MagicMock()), BuildKernel)
    assert isinstance(PiKernel(MagicMock()), BuildKernel)
    assert DiscoKernel.name == "disco"
    assert PiKernel.name == "pi_experimental"


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
    rt._control.kill.assert_awaited_once_with(CID)

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
    rt.kick.assert_called_once_with(CID)


async def test_disco_kernel_send_user_turn_no_context(store: SqliteEventStore) -> None:
    rt = _fake_runtime(store)
    await rt._disco_kernel.send_user_turn(CID, "just text")
    msgs = [e for e in await store.get_events(CID) if hasattr(e, "message")]
    assert len(msgs) == 1
    assert msgs[0].source == EventSource.USER
    assert msgs[0].meta == {}
    rt.kick.assert_called_once_with(CID)


# ---- runtime public ops route THROUGH the kernel ----------------------------


async def test_runtime_kernel_for_resolves_disco_by_default(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    monkeypatch.delenv(EXP_ENV, raising=False)
    rt = _fake_runtime(store, build_kernel="disco")
    assert rt._kernel_for(CID) is rt._disco_kernel


async def test_runtime_kernel_for_resolves_pi_only_when_flagged(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    rt = _fake_runtime(store, build_kernel="pi_experimental")
    monkeypatch.delenv(EXP_ENV, raising=False)
    assert rt._kernel_for(CID) is rt._disco_kernel  # gated off → disco
    monkeypatch.setenv(EXP_ENV, "1")
    assert rt._kernel_for(CID) is rt._pi_kernel  # gated on → stub


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


async def test_runtime_control_ops_route_to_pi_stub_when_flagged(
    monkeypatch: pytest.MonkeyPatch, store: SqliteEventStore
) -> None:
    """When pi_experimental is selected AND flagged on, the public op routes to the
    PiKernel stub, which raises NotImplementedError (never the live build)."""
    monkeypatch.setenv(EXP_ENV, "1")
    rt = _fake_runtime(store, build_kernel="pi_experimental")
    with pytest.raises(NotImplementedError):
        await ConversationRuntime.confirm(rt, CID)
    # The real loop's collaborator was never touched.
    rt._control.confirm.assert_not_awaited()


# ---- PiKernel stub -----------------------------------------------------------


async def test_pi_kernel_all_ops_not_implemented(store: SqliteEventStore) -> None:
    k = PiKernel(_fake_runtime(store))
    with pytest.raises(NotImplementedError):
        k.start(CID)
    for coro in (
        k.send_user_turn(CID, "x"),
        k.approve_plan(CID),
        k.reject_plan(CID),
        k.request_plan(CID),
        k.confirm(CID),
        k.reject(CID),
        k.pick_alternative(CID, "o"),
        k.pause(CID),
        k.cancel(CID),
        k.resume(CID),
        k.kill(CID),
        k.subscribe(CID),
        k.get_state(CID),
    ):
        with pytest.raises(NotImplementedError):
            await coro
