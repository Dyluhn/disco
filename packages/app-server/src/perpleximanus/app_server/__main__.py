"""Run the app-server with uvicorn: `python -m perpleximanus.app_server`.

Env:
  PMX_HOST (default 127.0.0.1), PMX_PORT (default 8800),
  PMX_DB   (default ./perpleximanus.db — the shared SQLite event store).
"""

from __future__ import annotations

import os

import uvicorn
from perpleximanus.core.store.sqlite import SqliteEventStore

from .app import create_app


def main() -> None:
    store = SqliteEventStore(os.environ.get("PMX_DB", "perpleximanus.db"))
    app = create_app(store)
    uvicorn.run(
        app,
        host=os.environ.get("PMX_HOST", "127.0.0.1"),
        port=int(os.environ.get("PMX_PORT", "8800")),
    )


if __name__ == "__main__":
    main()
