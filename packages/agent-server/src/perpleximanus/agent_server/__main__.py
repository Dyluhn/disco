"""Run the agent-server with uvicorn: `python -m perpleximanus.agent_server`.

Builds the shared event store + the live ConversationRuntime (real Qwen-backed
loop) and serves the wire/REST surface. Env:
  PMX_HOST (default 127.0.0.1), PMX_PORT (default 8000),
  PMX_DB   (default ./perpleximanus.db — shared with the app-server).
"""

from __future__ import annotations

import os

import uvicorn
from perpleximanus.core.store.sqlite import SqliteEventStore

from .app import create_app
from .runtime import ConversationRuntime


def main() -> None:
    store = SqliteEventStore(os.environ.get("PMX_DB", "perpleximanus.db"))
    runtime = ConversationRuntime(store)
    app = create_app(store, runtime=runtime)
    uvicorn.run(
        app,
        host=os.environ.get("PMX_HOST", "127.0.0.1"),
        port=int(os.environ.get("PMX_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
