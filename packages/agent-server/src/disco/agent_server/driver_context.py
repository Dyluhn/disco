"""Per-run driver model context-window resolution — compatibility facade.

The generic implementation lives in ``disco.core.driver_context`` (target-
neutral Core). This module re-exports the public symbols so existing import
paths under ``disco.agent_server.driver_context`` remain stable.
"""

from __future__ import annotations

from disco.core.driver_context import (
    DriverContextResolutionError,
    DriverContextResolver,
    ResolvedDriverContext,
)

__all__ = [
    "DriverContextResolutionError",
    "DriverContextResolver",
    "ResolvedDriverContext",
]
