"""The Disco loop implementation behind the stable ``BuildKernel`` seam."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from disco.core import (
    ConversationState,
    ConversationStatus,
    MessageEvent,
    active_verification_requirements_event,
)
from disco.core.appkit import BuildBrief
from disco.core.store.sqlite import SqliteEventStore
from disco.core.verification import VerificationRequirementsDirective

from ..build_loop_factory import BuildLoopFactory
from ..build_messages import _build_brief_message, _context_message, _user_message
from ..control_ops import ControlOps
from ..lifecycle_command_service import LifecycleCommandService
from ..run_controller import RunController
from ..workspace_service import WorkspaceCoordinator

if TYPE_CHECKING:
    from ..resume_service import ResumeService
    from ..run_registry import KernelPinStore, RunRegistry
    from ..runtime import ConversationRuntime
    from .base import KernelEvent


class DiscoKernel:
    """The Disco ``AgentLoop``-driven Build with explicit named collaborators."""

    name: ClassVar[str] = "disco"

    if TYPE_CHECKING:

        def __init__(self, runtime: ConversationRuntime | Any) -> None: ...

        async def resume(self, conversation_id: str) -> None: ...

        async def subscribe(
            self, conversation_id: str, *, after_seq: int | None = None
        ) -> AsyncIterator[KernelEvent]: ...

        async def get_state(self, conversation_id: str) -> ConversationState: ...

    else:

        def __init__(self, runtime: object) -> None:
            """Legacy constructor: copy named owners without retaining the runtime."""

            self._assign_owners(
                runtime._store,
                runtime.workspace,
                runtime._lifecycle_commands,
                runtime.run_controller,
                runtime._control,
                runtime._loop_factory,
                runtime.run_registry,
                runtime._kernel_pin_store,
                runtime._resume,
            )

    @classmethod
    def _from_owners(
        cls,
        store: SqliteEventStore,
        workspace: WorkspaceCoordinator,
        lifecycle: LifecycleCommandService,
        controller: RunController,
        controls: ControlOps,
        loops: BuildLoopFactory,
        runs: RunRegistry,
        pins: KernelPinStore,
        resume: ResumeService | None = None,
    ) -> Self:
        kernel = cls.__new__(cls)
        kernel._assign_owners(
            store,
            workspace,
            lifecycle,
            controller,
            controls,
            loops,
            runs,
            pins,
            resume,
        )
        return kernel

    def _assign_owners(
        self,
        store: SqliteEventStore,
        workspace: WorkspaceCoordinator,
        lifecycle: LifecycleCommandService,
        controller: RunController,
        controls: ControlOps,
        loops: BuildLoopFactory,
        runs: RunRegistry,
        pins: KernelPinStore,
        resume: ResumeService | None,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._lifecycle = lifecycle
        self._controller = controller
        self._controls = controls
        self._loops = loops
        self._runs = runs
        self._pins = pins
        self._resume = resume

    def _bind_resume(self, resume: ResumeService) -> None:
        if self._resume is not None:
            raise RuntimeError("DiscoKernel resume owner is already bound")
        self._resume = resume

    # -- lifecycle ------------------------------------------------------------
    def start(self, conversation_id: str) -> None:
        self._controller.kick(conversation_id)

    async def send_user_turn(
        self,
        conversation_id: str,
        text: str,
        *,
        context: str | None = None,
        build_brief: BuildBrief | None = None,
        verification_requirements: VerificationRequirementsDirective | None = None,
        steer: bool = False,
    ) -> MessageEvent:
        """Append the (optional hidden context +) user message, then kick — the
        same store appends + `kick` the WS/REST send_message path performs.

        Returns the stored USER message (the REST send/followup routes report its
        id/seq) — byte-identical to the append the routes did inline before the seam."""
        # The user turn, its durable revision marker, and task registration are
        # one linearizable ingress operation relative to host-side mutations.
        # The newly registered task queues on this same non-reentrant fence and
        # cannot execute until this block publishes the complete turn.
        async with self._workspace.lock(conversation_id):
            async with self._workspace.interprocess_mutation_fence(conversation_id):
                pending = []
                if context:
                    pending.append(_context_message(context))
                if build_brief is not None:
                    pending.append(_build_brief_message(build_brief))
                history = await self._store.get_events(conversation_id)
                latest_requirement_event = active_verification_requirements_event(history)
                if verification_requirements is not None:
                    expected_predecessor = (
                        latest_requirement_event.id
                        if latest_requirement_event is not None
                        else None
                    )
                    if verification_requirements.supersedes_event_id != expected_predecessor:
                        raise ValueError(
                            "verification requirement snapshot must supersede the current snapshot"
                        )
                user_event = _user_message(
                    text,
                    steer=steer,
                    verification_requirements=verification_requirements,
                )
                pending.append(user_event)
                # USER and run-intent share one SQLite transaction: cancellation
                # can leave neither or both, never a durable unmarked user turn.
                stored_events = await self._workspace.append_run_ingress_locked(
                    conversation_id,
                    pending,
                    "user-turn",
                )
                stored = next(
                    event
                    for event in stored_events
                    if isinstance(event, MessageEvent) and event.id == user_event.id
                )
                # A live revision steer needs a sequence-stable marker that cannot be
                # masked by later in-flight Action/Observation events.
                if steer:
                    from disco.core.loop import signals as _signals

                    if _signals.is_revision_intent(text) and not (
                        _signals.current_blocked_question_landing(
                            await self._store.get_events(conversation_id)
                        )
                    ):
                        await self._lifecycle.append_transition_batch_locked(
                            conversation_id,
                            [
                                LifecycleCommandService.build_status(
                                    ConversationStatus.RUNNING,
                                    detail="revision_steer_pending",
                                )
                            ],
                            bind_current=True,
                        )
                self._controller.kick(conversation_id, claimed_user_seq=stored.seq)
                self._workspace.claim_registered_run_locked(conversation_id)
        return cast("MessageEvent", stored)

    # -- plan gate ------------------------------------------------------------
    async def approve_plan(self, conversation_id: str) -> None:
        await self._controls.approve_plan(conversation_id)

    async def reject_plan(self, conversation_id: str, reason: str = "") -> None:
        # Disco has no distinct "reject plan" op — a rejected plan is a re-plan
        # request carrying the user's revision (empty reason → plain replan).
        await self._controls.request_plan(conversation_id, reason)

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        await self._controls.request_plan(conversation_id, text)

    # -- action gate ----------------------------------------------------------
    async def confirm(self, conversation_id: str) -> None:
        await self._controls.confirm(conversation_id)

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        await self._controls.reject(conversation_id, reason)

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        # The inner impl (runtime.pick_alternative does exactly this); replicated
        # here so the public runtime method can route through the kernel safely.
        loop = self._loops.loop_for(conversation_id)
        await loop.pick_alternative(option_id)

    # -- stop / resume --------------------------------------------------------
    async def pause(self, conversation_id: str) -> None:
        await self._controls.pause(conversation_id)

    async def cancel(self, conversation_id: str) -> None:
        await self._controls.cancel(conversation_id)

    async def kill(self, conversation_id: str) -> None:
        # Generation-guarded kill (finding #4) — mirror `ConversationRuntime.kill`:
        # capture the run-generation, clear only THIS generation's pin, and thread the
        # generation to the control op so a newer run that starts during teardown is
        # neither terminalized nor torn down.
        generation = self._runs.generation(conversation_id)
        self._pins.clear_if_current(conversation_id, generation, self._runs)
        await self._controls.kill(conversation_id, generation)


async def _compat_resume(self: DiscoKernel, conversation_id: str) -> None:
    if self._resume is None:
        raise RuntimeError("DiscoKernel resume owner is not bound")
    await self._resume.resume_conversation(conversation_id)


async def _compat_subscribe(
    self: DiscoKernel,
    conversation_id: str,
    *,
    after_seq: int | None = None,
) -> AsyncIterator[KernelEvent]:
    return await self._store.subscribe(conversation_id, after_seq=after_seq)


async def _compat_get_state(
    self: DiscoKernel,
    conversation_id: str,
) -> ConversationState:
    return await self._store.get_state(conversation_id)


DiscoKernel.resume = _compat_resume
DiscoKernel.subscribe = _compat_subscribe
DiscoKernel.get_state = _compat_get_state
