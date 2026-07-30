"""Conversation-scoped settings and live queues for Deep Research."""

from __future__ import annotations

from typing import Literal, TypeVar, cast

from disco.retrieval.deep_research import DepthTier
from disco.retrieval.models import Passage

_T = TypeVar("_T")


class DeepResearchState:
    """Own transient Deep Research selections, uploads, and live input queues."""

    def __init__(self) -> None:
        self._depth: dict[str, str] = {}
        self._iterative: dict[str, bool] = {}
        self._recency: dict[str, Literal["month", "week"]] = {}
        self._upload_passages: dict[str, list[Passage]] = {}
        self._steers: dict[str, list[str]] = {}
        self._injected_sources: dict[str, list[Passage]] = {}

    def forget(self, conversation_id: str) -> None:
        self._depth.pop(conversation_id, None)
        self._iterative.pop(conversation_id, None)
        self._recency.pop(conversation_id, None)
        self._upload_passages.pop(conversation_id, None)
        self._steers.pop(conversation_id, None)
        self._injected_sources.pop(conversation_id, None)

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

    def iterative_for(self, conversation_id: str) -> bool:
        return self._iterative.get(conversation_id, False)

    def set_iterative(self, conversation_id: str, enabled: bool) -> None:
        self._iterative[conversation_id] = bool(enabled)

    def set_recency(self, conversation_id: str, window: str | None) -> None:
        if window in {"month", "week"}:
            self._recency[conversation_id] = cast(Literal["month", "week"], window)
        elif window is None:
            self._recency.pop(conversation_id, None)

    def recency_for(self, conversation_id: str) -> Literal["month", "week"] | None:
        return self._recency.get(conversation_id)

    def begin_live_run(self, conversation_id: str) -> None:
        self._steers[conversation_id] = []
        self._injected_sources[conversation_id] = []

    def enqueue_steer(self, conversation_id: str, text: str) -> bool:
        queue = self._steers.get(conversation_id)
        if queue is None:
            return False
        queue.append(text)
        return True

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
