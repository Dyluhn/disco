"""Bounded replay-protection ledger for browser protocol request nonces.

Split out of ``BrowserState``: nonce/replay defense is a distinct protocol
concern from Playwright process/lane lifecycle, and it has zero external
caller — only ``BrowserHandler._protocol`` used ``BrowserState.accept_nonce``,
and only internally — so it carries no compatibility burden.
"""

from __future__ import annotations

from collections import deque


class NonceLedger:
    """FIFO-bounded set of recently-seen request nonces."""

    def __init__(self, limit: int) -> None:
        self._order: deque[str] = deque()
        self._seen: set[str] = set()
        self._limit = limit

    def accept(self, nonce: str) -> bool:
        if nonce in self._seen:
            return False
        self._seen.add(nonce)
        self._order.append(nonce)
        while len(self._order) > self._limit:
            expired = self._order.popleft()
            self._seen.discard(expired)
        return True

    def clear(self) -> None:
        self._order.clear()
        self._seen.clear()
