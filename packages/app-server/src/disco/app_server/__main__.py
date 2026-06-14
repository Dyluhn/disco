"""Run the app-server with uvicorn: `python -m disco.app_server`.

Env:
  PMX_HOST (default 127.0.0.1), PMX_PORT (default 8800),
  PMX_DB   (default ./disco.db — the shared SQLite event store).
"""

from __future__ import annotations

import os

from disco.core.env import disco_env

import uvicorn
from disco.core.store.sqlite import SqliteEventStore

from .app import create_app


def main() -> None:
    store = SqliteEventStore(disco_env("DB", "disco.db"))
    app = create_app(store)
    uvicorn.run(
        app,
        host=disco_env("HOST", "127.0.0.1"),
        port=int(disco_env("PORT", "8800")),
    )


if __name__ == "__main__":
    main()
