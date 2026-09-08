"""The loop's own re-issue queue for queries the infrastructure never tested.

A query that came back ``provider_degraded``, ``extraction_failure``, or blew up
in transport was never put to the world. Somebody has to run it again. For a
long time that somebody was the MODEL — the degraded-feedback text told it so in
as many words — and the cost is measurable: it spent its own turns and tokens
re-issuing queries into an outage, and the harness read the result as thrash.

Retrying infrastructure is system mechanics. The queue here is the system doing
its own job: untested queries are enqueued keyed to the engines that refused
them, and the loop drains the queue itself at the top of each turn once those
engines are out of the process-local cooldown registry. The results come back to
the model as ordinary observations. The model is never asked, never told, and —
via ``QueuedQueryRef`` and the freshness wall — never allowed to take the work
back.

Two hard, work-denominated bounds keep the queue from becoming its own runaway,
in the spirit of "budgets are hard and visible":

* :data:`MAX_REISSUES` — how many times the host will re-run ONE query before it
  gives up and lets the angle stand as unreached. An outage that outlasts this
  is an outage the run cannot research around, and the exhaustion error says so.
* :data:`MAX_DRAIN_PER_TURN` — how many queued queries one drain may issue. It
  is one research turn's own query width, so a recovering pool is never met with
  a burst that re-bans it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from ._search_outcomes import (
    QueuedQueryRef,
    cooldown_until_text,
    normalize_query,
    query_tokens,
)

#: How many times the SYSTEM re-issues one untested query before giving up.
#: Two: the first re-issue covers a flap, the second covers a cooldown window
#: that expired early. A third would be waiting on an outage, not retrying one.
MAX_REISSUES = 2

#: How many queued queries one drain issues — one research turn's own width.
MAX_DRAIN_PER_TURN = 3


@dataclass(frozen=True)
class QueuedQuery:
    """One untested query the host owns re-running, and what refused it."""

    query: str
    normalized: str
    tokens: frozenset[str]
    turn: int
    reason: str
    #: The engines named in the degradation that produced it, lowercased to the
    #: same identity the cooldown registry keys on. Empty for an extraction
    #: outage or a transport failure: those name no engine, so nothing gates
    #: them beyond the next drain.
    engines: tuple[str, ...] = ()
    #: How many times the host has issued it, counting the model's original.
    attempts: int = 1

    def blocked_by(self, cooling: Mapping[str, float]) -> tuple[str, ...]:
        """The engines this query is waiting on that are still cut off."""
        return tuple(engine for engine in self.engines if engine in cooling)


@dataclass
class ReissueQueue:
    """Untested queries the loop will run again by itself, in arrival order."""

    _items: list[QueuedQuery] = field(default_factory=list)
    #: Queries whose re-issue budget is spent. Kept so the loop can say the
    #: angle was dropped rather than silently forgetting it.
    _abandoned: list[QueuedQuery] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def queries(self) -> tuple[str, ...]:
        return tuple(item.query for item in self._items)

    @property
    def abandoned(self) -> tuple[str, ...]:
        return tuple(item.query for item in self._abandoned)

    def holds(self, query: str) -> bool:
        """Whether this exact query (by host identity) is already queued."""
        return any(item.normalized == normalize_query(query) for item in self._items)

    def enqueue(self, query: str, *, turn: int, reason: str, engines: Iterable[str] = ()) -> bool:
        """Take over re-running one untested query. False when already held."""
        normalized = normalize_query(query)
        if not normalized or any(item.normalized == normalized for item in self._items):
            return False
        self._items.append(
            QueuedQuery(
                query=query,
                normalized=normalized,
                tokens=query_tokens(query),
                turn=turn,
                reason=reason,
                engines=tuple(dict.fromkeys(engine for engine in engines if engine)),
            )
        )
        return True

    def take_ready(self, cooling: Mapping[str, float]) -> list[QueuedQuery]:
        """Remove and return the queued queries whose engines are live again.

        A query that named no engine (extraction outage, transport failure) is
        ready on the next drain: nothing in the cooldown registry describes what
        broke it, so the honest probe is to run it and see.
        """
        ready = [item for item in self._items if not item.blocked_by(cooling)][:MAX_DRAIN_PER_TURN]
        taken = {id(item) for item in ready}
        self._items = [item for item in self._items if id(item) not in taken]
        return ready

    def requeue(self, item: QueuedQuery, *, reason: str, engines: Iterable[str] = ()) -> bool:
        """Put a still-untested re-issue back, or abandon it at its budget."""
        if item.attempts + 1 > MAX_REISSUES:
            self._abandoned.append(item)
            return False
        self._items.append(
            replace(
                item,
                attempts=item.attempts + 1,
                reason=reason,
                engines=tuple(dict.fromkeys(engine for engine in engines if engine))
                or item.engines,
            )
        )
        return True

    def refs(self, cooling: Mapping[str, float]) -> tuple[QueuedQueryRef, ...]:
        """The freshness wall's view of every query the HOST owns.

        Abandoned queries stay in this view forever. Dropping them would make
        the re-issue budget meaningless: the model would re-issue the query, the
        host would take it over again with a fresh budget, and the pair would
        retry an outage between them without limit. Once the budget is spent the
        angle is unreached, the refusal says exactly that, and the only move left
        is a real pivot.
        """
        return tuple(
            QueuedQueryRef(
                query=item.query,
                normalized=item.normalized,
                tokens=item.tokens,
                detail=_waiting_detail(item, cooling, spent=spent),
                spent=spent,
            )
            for spent, item in (
                *((False, item) for item in self._items),
                *((True, item) for item in self._abandoned),
            )
        )


def _waiting_detail(item: QueuedQuery, cooling: Mapping[str, float], *, spent: bool) -> str:
    """Where this query stands with the host, in the words the model reads."""
    if spent:
        return (
            f"was run {item.attempts} times by the host and never reached the world "
            f"({item.reason}); the host's retry budget for it is spent, so that "
            "angle stands unreached rather than disproved"
        )
    blocked = item.blocked_by(cooling)
    if blocked:
        return "is queued to run again behind " + cooldown_until_text(
            {engine: cooling[engine] for engine in blocked}
        )
    return f"is queued to run again on the next turn (it ended in {item.reason})"


def enqueue_untested(
    queue: ReissueQueue,
    untested: Sequence[tuple[str, str, tuple[str, ...]]],
    *,
    turn: int,
) -> list[str]:
    """Enqueue one turn's untested ``(query, reason, engines)`` rows.

    Returns the queries the queue actually took over, so the feedback the model
    reads describes the host's real state rather than the host's intention.
    """
    return [
        query
        for query, reason, engines in untested
        if queue.enqueue(query, turn=turn, reason=reason, engines=engines)
    ]


__all__ = [
    "MAX_DRAIN_PER_TURN",
    "MAX_REISSUES",
    "QueuedQuery",
    "ReissueQueue",
    "enqueue_untested",
]
