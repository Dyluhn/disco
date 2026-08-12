"""Run-scoped source admission for concurrent Deep Research legs."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Passage


@dataclass
class SourceBudget:
    """Admit at most ``limit`` unique web passages across one execution.

    All gather tasks run on the same asyncio loop. ``admit`` contains no await,
    so checking and consuming capacity is one atomic event-loop operation.
    Passages already admitted by another leg remain usable without being charged
    twice; user uploads are never passed here and therefore do not consume the
    web-retrieval allowance.
    """

    limit: int
    _seen_ids: set[str] = field(default_factory=set)

    @property
    def used(self) -> int:
        return len(self._seen_ids)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def admit(
        self, passages: list[Passage], *, charge_limit: int | None = None
    ) -> tuple[list[Passage], int]:
        """Return usable passages while charging at most ``charge_limit`` new ids.

        A passage already admitted by a sibling leg is still usable by this leg
        and costs neither the shared nor local allowance.  New passages beyond
        the caller's local allowance are skipped rather than marked seen, so a
        later leg can still admit them if it has capacity.
        """
        admitted: list[Passage] = []
        charged = 0
        for passage in passages:
            if passage.id in self._seen_ids:
                admitted.append(passage)
                continue
            if charge_limit is not None and charged >= charge_limit:
                continue
            if self.remaining <= 0:
                continue
            self._seen_ids.add(passage.id)
            admitted.append(passage)
            charged += 1
        return admitted, charged
