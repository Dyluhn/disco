"""Owned in-process state for agent run registration and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol, cast

from disco.core import ConversationState, ConversationStatus
from disco.core.loop import AgentLoop
from disco.tools import DefaultToolExecutor, SandboxSession

if TYPE_CHECKING:
    from .build_kernel import BuildKernel

RunTask = asyncio.Task[ConversationState]
RunAuthority = tuple[str | None, str | None]


class KernelSelector(Protocol):
    def select_kernel(self, conversation_id: str) -> BuildKernel: ...


class RunWorkspacePort(Protocol):
    async def run_after_admission(
        self,
        conversation_id: str,
        loop: AgentLoop,
        *,
        expected_run_intent_id: str | None = None,
    ) -> ConversationState: ...

    def clear_run_claim(self, conversation_id: str) -> None: ...

    async def run_authority_is_current(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool: ...

    async def rekick_stranded_followup(
        self,
        conversation_id: str,
        claimed_user_seq: int,
    ) -> None: ...


class RunRegistry:
    """Atomic task, authority, generation, and ingress ownership."""

    def __init__(self) -> None:
        self._tasks: dict[str, RunTask] = {}
        self._generations: dict[str, int] = {}

    def active_task(self, conversation_id: str) -> RunTask | None:
        task = self._tasks.get(conversation_id)
        return task if task is not None and not task.done() else None

    def task(self, conversation_id: str) -> RunTask | None:
        """Return the registered task even when its callback has not cleaned a done task."""

        return self._tasks.get(conversation_id)

    def active_conversation_ids(self) -> tuple[str, ...]:
        return tuple(
            conversation_id
            for conversation_id, task in self._tasks.items()
            if not task.done()
        )

    def owns_task(self, conversation_id: str, task: RunTask) -> bool:
        return self._tasks.get(conversation_id) is task

    def register_task(
        self,
        conversation_id: str,
        task: RunTask,
    ) -> int:
        if self.active_task(conversation_id) is not None:
            raise RuntimeError("conversation already has a live run task")
        generation = self._generations.get(conversation_id, 0) + 1
        self._generations[conversation_id] = generation
        self._tasks[conversation_id] = task
        return generation

    def complete_task(
        self,
        conversation_id: str,
        task: RunTask,
    ) -> bool:
        owned = self._tasks.get(conversation_id) is task
        if owned:
            self._tasks.pop(conversation_id, None)
        return owned

    def generation(self, conversation_id: str) -> int | None:
        return self._generations.get(conversation_id)

    def generation_is_current(self, conversation_id: str, generation: int | None) -> bool:
        return generation is None or self._generations.get(conversation_id) == generation

    def detach_task_if_owned(self, conversation_id: str, task: RunTask | None) -> bool:
        """Remove exactly one observed task without touching its captured authority."""

        if self._tasks.get(conversation_id) is not task:
            return False
        self._tasks.pop(conversation_id, None)
        return True

    def restore_task_if_generation(
        self,
        conversation_id: str,
        task: RunTask,
        generation: int | None,
    ) -> bool:
        """Restore a detached task only while its generation still owns an empty slot."""

        if not self.generation_is_current(conversation_id, generation):
            return False
        if self._tasks.get(conversation_id) is not None:
            return False
        self._tasks[conversation_id] = task
        return True

    def forget(self, conversation_id: str) -> None:
        self._tasks.pop(conversation_id, None)
        self._generations.pop(conversation_id, None)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


class RunAuthorityLedger:
    """Durable view/intent identities captured by live task identity."""

    def __init__(self) -> None:
        self._authorities: dict[RunTask, RunAuthority] = {}

    def bind(
        self,
        task: RunTask,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> None:
        self._authorities[task] = (agent_view_id, run_intent_id)

    def get(self, task: RunTask) -> RunAuthority:
        return self._authorities.get(task, (None, None))

    def discard(self, task: RunTask) -> RunAuthority:
        return self._authorities.pop(task, (None, None))

    def clear(self) -> None:
        self._authorities.clear()


class RunIngressLedger:
    """Highest user sequence already claimed by a registered run."""

    def __init__(self) -> None:
        self._claimed_user_seqs: dict[str, int] = {}

    def claimed_user_seq(self, conversation_id: str) -> int | None:
        return self._claimed_user_seqs.get(conversation_id)

    def claim_user_seq(self, conversation_id: str, user_seq: int) -> None:
        self._claimed_user_seqs[conversation_id] = max(
            user_seq,
            self._claimed_user_seqs.get(conversation_id, -1),
        )

    def forget(self, conversation_id: str) -> None:
        self._claimed_user_seqs.pop(conversation_id, None)

    def clear(self) -> None:
        self._claimed_user_seqs.clear()


class LoopRegistry:
    """Composed loop ownership without exposing the backing dictionary."""

    def __init__(self) -> None:
        self._loops: dict[str, AgentLoop] = {}

    def loop(self, conversation_id: str) -> AgentLoop | None:
        return self._loops.get(conversation_id)

    def bind(self, conversation_id: str, loop: AgentLoop) -> None:
        self._loops[conversation_id] = loop

    def pop(self, conversation_id: str) -> AgentLoop | None:
        return self._loops.pop(conversation_id, None)

    def restore_if_absent(self, conversation_id: str, loop: AgentLoop) -> None:
        self._loops.setdefault(conversation_id, loop)

    def owns(self, conversation_id: str, loop: AgentLoop) -> bool:
        return self._loops.get(conversation_id) is loop

    def conversation_ids(self) -> tuple[str, ...]:
        return tuple(self._loops)

    def forget(self, conversation_id: str) -> None:
        self._loops.pop(conversation_id, None)


class RunResourceRegistry:
    """Live loop executors and pre-composition sandbox sessions."""

    def __init__(self) -> None:
        self._executors: dict[str, DefaultToolExecutor] = {}
        self._pending_sessions: dict[str, SandboxSession] = {}

    def executor(self, conversation_id: str) -> DefaultToolExecutor | None:
        return self._executors.get(conversation_id)

    def has_executor(self, conversation_id: str) -> bool:
        return conversation_id in self._executors

    def owns_executor(
        self,
        conversation_id: str,
        executor: DefaultToolExecutor | None,
    ) -> bool:
        return self._executors.get(conversation_id) is executor

    def set_executor(self, conversation_id: str, executor: DefaultToolExecutor) -> None:
        self._executors[conversation_id] = executor

    def pop_executor(self, conversation_id: str) -> DefaultToolExecutor | None:
        return self._executors.pop(conversation_id, None)

    def pending_session(self, conversation_id: str) -> SandboxSession | None:
        return self._pending_sessions.get(conversation_id)

    def has_pending_session(self, conversation_id: str) -> bool:
        return conversation_id in self._pending_sessions

    def set_pending_session(self, conversation_id: str, session: SandboxSession) -> None:
        self._pending_sessions[conversation_id] = session

    def pop_pending_session(self, conversation_id: str) -> SandboxSession | None:
        return self._pending_sessions.pop(conversation_id, None)

    def park_executor_session(self, conversation_id: str) -> None:
        executor = self._executors.pop(conversation_id, None)
        if conversation_id in self._pending_sessions or executor is None:
            return
        session = executor.sandbox
        if session is not None:
            self._pending_sessions[conversation_id] = cast(SandboxSession, session)

    def conversation_ids(self, *, executors_only: bool = False) -> tuple[str, ...]:
        if executors_only:
            return tuple(self._executors)
        return tuple(dict.fromkeys((*self._executors, *self._pending_sessions)))

    async def close(self) -> None:
        for executor in tuple(self._executors.values()):
            with contextlib.suppress(Exception):
                await executor.kill()
        for session in tuple(self._pending_sessions.values()):
            with contextlib.suppress(Exception):
                await session.destroy()
        self._executors.clear()
        self._pending_sessions.clear()


class RunRecoveryLedger:
    """Bounded stall and terminal re-kick bookkeeping."""

    def __init__(self) -> None:
        self._nonterminal_rekicks: dict[str, int] = {}
        self._last_progress_seq: dict[str, int] = {}
        self._last_status: dict[str, ConversationStatus] = {}
        self._post_terminal_rekick_seq: dict[str, int] = {}

    def last_status(self, conversation_id: str) -> ConversationStatus | None:
        return self._last_status.get(conversation_id)

    def record_status(self, conversation_id: str, status: ConversationStatus) -> None:
        self._last_status[conversation_id] = status

    def reset_stall(self, conversation_id: str) -> None:
        self._nonterminal_rekicks.pop(conversation_id, None)
        self._last_progress_seq.pop(conversation_id, None)

    def record_progress(self, conversation_id: str, latest_seq: int | None) -> int:
        previous = self._last_progress_seq.get(conversation_id, 0)
        latest_seq = previous if latest_seq is None else latest_seq
        if latest_seq > previous:
            self._nonterminal_rekicks[conversation_id] = 0
        self._last_progress_seq[conversation_id] = latest_seq
        return self._nonterminal_rekicks.get(conversation_id, 0)

    def increment_stall(self, conversation_id: str) -> int:
        attempts = self._nonterminal_rekicks.get(conversation_id, 0) + 1
        self._nonterminal_rekicks[conversation_id] = attempts
        return attempts

    def claim_terminal_rekick(self, conversation_id: str, user_seq: int) -> bool:
        previous = self._post_terminal_rekick_seq.get(conversation_id)
        if previous is not None and user_seq <= previous:
            return False
        self._post_terminal_rekick_seq[conversation_id] = user_seq
        return True

    def forget(self, conversation_id: str) -> None:
        self._nonterminal_rekicks.pop(conversation_id, None)
        self._last_progress_seq.pop(conversation_id, None)
        self._last_status.pop(conversation_id, None)
        self._post_terminal_rekick_seq.pop(conversation_id, None)


class CancellationRegistry:
    """Cooperative stop ownership shared by agent and Deep Research runs."""

    def __init__(self) -> None:
        self._flags: dict[str, asyncio.Event] = {}

    def begin(self, conversation_id: str) -> asyncio.Event:
        flag = asyncio.Event()
        self._flags[conversation_id] = flag
        return flag

    def request(self, conversation_id: str) -> None:
        self._flags.setdefault(conversation_id, asyncio.Event()).set()

    def clear(self, conversation_id: str) -> None:
        self._flags.pop(conversation_id, None)


class KernelPinRegistry:
    """Generation-safe Build-kernel selection and pinning."""

    def __init__(self, selector: KernelSelector) -> None:
        self._selector = selector
        self._pins: dict[str, BuildKernel] = {}

    def current(self, conversation_id: str) -> BuildKernel | None:
        return self._pins.get(conversation_id)

    def ensure(self, conversation_id: str) -> BuildKernel:
        current = self._pins.get(conversation_id)
        if current is not None:
            return current
        selected = self._selector.select_kernel(conversation_id)
        self._pins[conversation_id] = selected
        return selected

    def clear(self, conversation_id: str) -> None:
        self._pins.pop(conversation_id, None)

    def clear_if_current(
        self,
        conversation_id: str,
        generation: int | None,
        registry: RunRegistry,
    ) -> None:
        if registry.generation_is_current(conversation_id, generation):
            self.clear(conversation_id)
