"""Stable preview-capture ownership shared by preview routes and lifecycle."""

from __future__ import annotations

import asyncio
import time

from disco.core.auth import intent_ttl_s


class PreviewCaptureOwnership:
    """Own per-conversation preview locks and bounded handoff generations."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._deadlines: dict[str, float] = {}
        self._owners: dict[str, dict[int, float]] = {}
        self._next_generation = 0

    def lock_for(self, conversation_id: str) -> asyncio.Lock:
        """Return the stable lock that serializes preview capture and teardown."""
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    def clear_session_state(self, conversation_id: str) -> None:
        """Clear handoff ownership while preserving lock identity across rehydrate."""
        self._deadlines.pop(conversation_id, None)
        self._owners.pop(conversation_id, None)

    def clear_conversation(self, conversation_id: str) -> None:
        """Drop all ownership state after a conversation is forgotten."""
        self.clear_session_state(conversation_id)
        self._locks.pop(conversation_id, None)

    def begin(self, conversation_id: str) -> int:
        """Retain ownership across metadata → capability → redemption."""
        self._next_generation += 1
        generation = self._next_generation
        deadline = time.monotonic() + intent_ttl_s()
        self._owners.setdefault(conversation_id, {})[generation] = deadline
        self._deadlines[conversation_id] = max(
            deadline,
            self._deadlines.get(conversation_id, 0.0),
        )
        return generation

    def complete(self, conversation_id: str, generation: int | None = None) -> None:
        """Release one generation, never guessing at a tokenized owner."""
        owners = self._owners.get(conversation_id)
        if owners:
            if generation is None:
                return
            owners.pop(generation, None)
            if owners:
                self._deadlines[conversation_id] = max(owners.values())
            else:
                self._owners.pop(conversation_id, None)
                self._deadlines.pop(conversation_id, None)
            return
        self._deadlines.pop(conversation_id, None)

    def active(self, conversation_id: str) -> bool:
        return self.remaining(conversation_id) > 0

    def remaining(self, conversation_id: str) -> float:
        now = time.monotonic()
        owners = self._owners.get(conversation_id)
        if owners:
            for generation, deadline in list(owners.items()):
                if deadline <= now:
                    owners.pop(generation, None)
            if owners:
                deadline = max(owners.values())
                self._deadlines[conversation_id] = deadline
                return max(deadline - now, 0.0)
            self._owners.pop(conversation_id, None)

        deadline = self._deadlines.get(conversation_id)
        if deadline is None:
            return 0.0
        remaining = deadline - now
        if remaining <= 0:
            self._deadlines.pop(conversation_id, None)
            return 0.0
        return remaining
