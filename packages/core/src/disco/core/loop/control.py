"""Loop-control disposition — the signal an extracted ``run()`` gate/handler
returns to the skeleton.

Part of the ``AgentLoop.run()`` decomposition (god-function breakup): the
~1,800-line ``while True`` body is being split into named gate/handler methods.
Each one used to ``continue`` the loop, ``return await self.get_state()``, or
fall through to the next arm inline. Now they return a ``Disp`` and the thin
skeleton acts on it, so control flow stays in one readable place.

Gates that re-poll the event log return ``(Disp, list[Event])`` (the possibly
refreshed events); gates that don't, return a bare ``Disp``.
"""

from __future__ import annotations

from enum import Enum, auto


class Disp(Enum):
    """What the skeleton should do after an extracted gate/handler runs."""

    CONTINUE = auto()
    """Skeleton does ``continue`` — restart the loop iteration."""

    HALT = auto()
    """Skeleton does ``return await self.get_state()`` — end the run."""

    FALLTHROUGH = auto()
    """The arm did not consume the step; proceed to the next arm."""
