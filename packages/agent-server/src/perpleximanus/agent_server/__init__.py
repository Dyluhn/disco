"""perpleximanus.agent_server — per-conversation runtime (BoD §5).

Phase 0: the §7 wire layer (WebSocket event/state streaming + reconnect/replay +
the pending-message path) and the §7.5 REST surface, both thin adapters over the
core `EventStore`. No agent loop, no model — those arrive in Phase 1.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
