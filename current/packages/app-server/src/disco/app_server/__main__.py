"""Run the app-server with uvicorn: `python -m disco.app_server`.

Env:
  DISCO_HOST (default 127.0.0.1), DISCO_PORT (default 8800),
  DISCO_DB   (default ./disco.db — the shared SQLite event store).
  Legacy PMX_* names are still honored by disco_env().
"""

from __future__ import annotations

from typing import cast

import uvicorn
from disco.core.env import disco_env
from disco.core.llm.secrets import ensure_process_secret_key
from disco.core.store.sqlite import SqliteEventStore

from .app import create_app


def main() -> None:
    # disco_env() is typed str | None, but every call here passes a non-None
    # default so the runtime value is always `str`. `cast` is a typing-only
    # no-op (zero behavior change); fixing this properly would require
    # touching disco.core.env, which lives outside current/packages/app-server.
    ensure_process_secret_key()
    store = SqliteEventStore(cast(str, disco_env("DB", "disco.db")))
    app = create_app(store)
    uvicorn.run(
        app,
        host=cast(str, disco_env("HOST", "127.0.0.1")),
        port=int(cast(str, disco_env("PORT", "8800"))),
        ws="websockets-sansio",
    )


if __name__ == "__main__":
    main()
