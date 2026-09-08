"""Conversation-scoped selections and live input queues for Deep Research."""

from __future__ import annotations

from typing import Literal, TypeVar, cast

from disco.retrieval.deep_research import DepthTier
from disco.retrieval.models import Passage

_T = TypeVar("_T")


class DeepResearchState:
    """Own transient Deep Research selections and uploaded passages."""

    def __init__(self) -> None:
        self._depth: dict[str, str] = {}
        self._recency: dict[str, Literal["month", "week"]] = {}
        self._upload_passages: dict[str, list[Passage]] = {}

    def forget(self, conversation_id: str) -> None:
        self._depth.pop(conversation_id, None)
        self._recency.pop(conversation_id, None)
        self._upload_passages.pop(conversation_id, None)

    def add_upload_passages(
        self,
        conversation_id: str,
        passages: list[Passage],
    ) -> None:
        self._upload_passages.setdefault(conversation_id, []).extend(passages)

    def upload_passages(self, conversation_id: str) -> list[Passage]:
        return list(self._upload_passages.get(conversation_id, ()))

    def depth_for(self, conversation_id: str) -> DepthTier:
        try:
            return DepthTier(self._depth.get(conversation_id, DepthTier.STANDARD_DEEP.value))
        except ValueError:
            return DepthTier.STANDARD_DEEP

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        if tier and tier in {candidate.value for candidate in DepthTier}:
            self._depth[conversation_id] = tier

    def set_recency(self, conversation_id: str, window: str | None) -> None:
        if window in {"month", "week"}:
            self._recency[conversation_id] = cast(Literal["month", "week"], window)
        elif window is None:
            self._recency.pop(conversation_id, None)

    def recency_for(self, conversation_id: str) -> Literal["month", "week"] | None:
        return self._recency.get(conversation_id)


class DeepResearchLiveState:
    """Own inputs queued for an active Deep Research run."""

    def __init__(self) -> None:
        self._steers: dict[str, list[str]] = {}
        self._injected_sources: dict[str, list[Passage]] = {}
        self._writing: set[str] = set()

    def forget(self, conversation_id: str) -> None:
        self._steers.pop(conversation_id, None)
        self._injected_sources.pop(conversation_id, None)
        self._writing.discard(conversation_id)

    def begin(self, conversation_id: str) -> None:
        self._writing.discard(conversation_id)
        self._steers[conversation_id] = []
        self._injected_sources[conversation_id] = []

    def is_live(self, conversation_id: str) -> bool:
        """True while the ENGINE owns this conversation's run.

        `begin` installs the queues as the run starts and the run's own
        `finally` removes them, so key presence is the live-run fact —
        the same one `enqueue_steer` already trusts to decide whether mid-run
        input becomes a steer or a new user turn. Stop reads it to decide
        whether the terminal status belongs to the engine or to an AgentLoop.
        """
        return conversation_id in self._steers

    def enqueue_steer(self, conversation_id: str, text: str) -> bool:
        queue = self._steers.get(conversation_id)
        if queue is None or conversation_id in self._writing:
            return False
        queue.append(text)
        return True

    def close_research_inputs(self, conversation_id: str) -> None:
        if self.is_live(conversation_id):
            self._writing.add(conversation_id)

    def observe_phase(self, conversation_id: str, kind: str, phase: object) -> None:
        if kind == "phase" and phase == "writing":
            self.close_research_inputs(conversation_id)

    def inject_source(self, conversation_id: str, passage: Passage) -> bool:
        queue = self._injected_sources.get(conversation_id)
        if queue is None:
            return False
        queue.append(passage)
        return True

    def pop_steers(self, conversation_id: str) -> list[str]:
        return self._drain(self._steers.get(conversation_id))

    def pop_injected_sources(self, conversation_id: str) -> list[Passage]:
        return self._drain(self._injected_sources.get(conversation_id))

    @staticmethod
    def _drain(queue: list[_T] | None) -> list[_T]:
        if not queue:
            return []
        drained, queue[:] = queue[:], []
        return drained
