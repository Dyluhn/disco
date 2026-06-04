"""perpleximanus.agent_server — per-conversation runtime (BoD §5).

The §7 wire layer (WebSocket event/state streaming + reconnect/replay) and the
§7.5 REST surface over the core `EventStore`, PLUS the `ConversationRuntime` that
runs the agent loop with real inference (Stage 2): a user message kicks the loop,
which calls the model and streams its events over the WebSocket.

Run it: `python -m perpleximanus.agent_server` (uvicorn on :8000 by default).
"""

from __future__ import annotations

from .app import create_app
from .runtime import ConversationRuntime

__all__ = ["ConversationRuntime", "create_app"]
