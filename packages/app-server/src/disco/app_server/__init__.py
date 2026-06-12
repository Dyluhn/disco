"""disco.app_server — the user-facing orchestrator (BoD §5.2).

Owns the settings surface (the deterministic model-assignment matrix + the
skills/MCP scaffolds) and the library surface (owner-scoped conversation list +
delete) over the shared core EventStore. It proxies the per-conversation runtime
+ live WebSocket to the agent-server; it does not run agent loops itself.

Run it: `python -m disco.app_server` (uvicorn on :8800 by default).
"""

from __future__ import annotations

from .app import create_app
from .config_state import ConfigState

__all__ = ["ConfigState", "create_app"]
