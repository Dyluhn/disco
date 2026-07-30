"""Owned in-process state for agent run registration and cleanup."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from disco.core import ConversationState, ConversationStatus
from disco.core.loop import AgentLoop
from disco.tools import DefaultToolExecutor, SandboxSession

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
        latest_user_seq: int,
    ) -> None: ...


class RunRegistry:
    """Atomic task, loop, authority, generation, and ingress ownership."""

    def __init__(self) -> None:
        self._loops: dict[str, AgentLoop] = {}
        self._tasks: dict[str, RunTask] = {}
        self._task_authorities: dict[RunTask, RunAuthority] = {}
        self._generations: dict[str, int] = {}
        self._claimed_user_seqs: dict[str, int] = {}

    @property
    def loops(self) -> dict[str, AgentLoop]:
        return self._loops

    @property
    def tasks(self) -> dict[str, RunTask]:
        return self._tasks

    def active_task(self, conversation_id: str) -> RunTask | None:
        task = self._tasks.get(conversation_id)
        return task if task is not None and not task.done() else None

    def register_task(
        self,
        conversation_id: str,
        task: RunTask,
        *,
        claimed_user_seq: int | None = None,
    ) -> int:
        if self.active_task(conversation_id) is not None:
            raise RuntimeError("conversation already has a live run task")
        if claimed_user_seq is not None:
            self._claimed_user_seqs[conversation_id] = max(
                claimed_user_seq,
                self._claimed_user_seqs.get(conversation_id, -1),
            )
        generation = self._generations.get(conversation_id, 0) + 1
        self._generations[conversation_id] = generation
        self._tasks[conversation_id] = task
        return generation

    def complete_task(
        self,
        conversation_id: str,
        task: RunTask,
    ) -> tuple[RunAuthority, bool]:
        authority = self._task_authorities.pop(task, (None, None))
        owned = self._tasks.get(conversation_id) is task
        if owned:
            self._tasks.pop(conversation_id, None)
        return authority, owned

    def bind_authority(
        self,
        task: RunTask,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> None:
        self._task_authorities[task] = (agent_view_id, run_intent_id)

    def generation(self, conversation_id: str) -> int | None:
        return self._generations.get(conversation_id)

    def generation_is_current(self, conversation_id: str, generation: int | None) -> bool:
        return generation is None or self._generations.get(conversation_id) == generation

    def claimed_user_seq(self, conversation_id: str) -> int | None:
        return self._claimed_user_seqs.get(conversation_id)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._task_authorities.clear()


class RunResourceRegistry:
    """Live loop executors and pre-composition sandbox sessions."""

    def __init__(self) -> None:
        self._executors: dict[str, DefaultToolExecutor] = {}
        self._pending_sessions: dict[str, SandboxSession] = {}

    @property
    def executors(self) -> dict[str, DefaultToolExecutor]:
        return self._executors

    @property
    def pending_sessions(self) -> dict[str, SandboxSession]:
        return self._pending_sessions

    def executor(self, conversation_id: str) -> DefaultToolExecutor | None:
        return self._executors.get(conversation_id)

    def set_executor(self, conversation_id: str, executor: DefaultToolExecutor) -> None:
        self._executors[conversation_id] = executor

    def pop_executor(self, conversation_id: str) -> DefaultToolExecutor | None:
        return self._executors.pop(conversation_id, None)

    def pending_session(self, conversation_id: str) -> SandboxSession | None:
        return self._pending_sessions.get(conversation_id)

    def set_pending_session(self, conversation_id: str, session: SandboxSession) -> None:
        self._pending_sessions[conversation_id] = session

    def pop_pending_session(self, conversation_id: str) -> SandboxSession | None:
        return self._pending_sessions.pop(conversation_id, None)

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


class KernelPinRegistry:
    """Generation-safe Build-kernel selection and pinning."""

    def __init__(self, selector: KernelSelector) -> None:
        self._selector = selector
        self._pins: dict[str, BuildKernel] = {}

    @property
    def pins(self) -> dict[str, BuildKernel]:
        return self._pins

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
