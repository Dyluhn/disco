"""Run the agent-server with uvicorn: `python -m disco.agent_server`.

Builds the shared event store + the live ConversationRuntime (real Qwen-backed
loop) and serves the wire/REST surface. Env:
  PMX_HOST (default 127.0.0.1), PMX_PORT (default 8000),
  PMX_DB   (default ./disco.db — shared with the app-server).
  PMX_SANDBOX (unset → config-driven; the persisted Settings default is "local") —
    an OPTIONAL startup override of the Build surface's sandbox backend:
    "process" (dev, runs on the HOST, NO isolation), "local" (a real container via the
    rootless Podman/Docker socket — runc/crun), "gvisor"/"podman" (remote-host backends).
  PMX_LOCAL_SOCKET (default unix:///run/user/1000/podman/podman.sock),
  PMX_LOCAL_RUNTIME (default runc), PMX_SANDBOX_IMAGE (default pmx-sandbox:base).
"""

from __future__ import annotations

import os

from disco.core.env import disco_env

import uvicorn
from disco.core.store.sqlite import SqliteEventStore

from .app import create_app
from .runtime import ConversationRuntime


def _sandbox_service():
    """An OPTIONAL startup OVERRIDE of the Build sandbox backend. Unset → the runtime
    reads the backend from the persisted Settings (the Sandbox section) per request.
    Set PMX_SANDBOX=process|local|gvisor|podman to force one (with the env connection)."""
    backend = disco_env("SANDBOX")
    if not backend:
        return None  # config-driven (the Settings selector)
    from disco.core.llm import SandboxSettings

    from .runtime import build_sandbox_service

    return build_sandbox_service(
        SandboxSettings(
            backend=backend.lower(),
            runtime=disco_env("LOCAL_RUNTIME", "runc"),
            docker_socket=os.environ.get(
                "PMX_LOCAL_SOCKET", "unix:///run/user/1000/podman/podman.sock"
            ),
            image=disco_env("SANDBOX_IMAGE", "pmx-sandbox:base"),
        )
    )


def main() -> None:
    # Surface app-logger output (providers, the loop) alongside uvicorn's access log —
    # uvicorn configures only its own loggers, so without this the disco.*
    # INFO traces (e.g. "ddgs search …", "local extract …") are silently dropped.
    import logging

    level = disco_env("LOG_LEVEL", "INFO").upper()
    if disco_env("LOG_JSON") == "1":
        # Structured JSON logs (core.obs span records become one JSON object/line —
        # greppable + trace-assertable). Plain text otherwise.
        from disco.core.obs import install_json_logging

        install_json_logging(level)
    else:
        logging.basicConfig(
            level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    store = SqliteEventStore(disco_env("DB", "disco.db"))
    runtime = ConversationRuntime(store, sandbox_service=_sandbox_service())
    app = create_app(store, runtime=runtime)
    uvicorn.run(
        app,
        host=disco_env("HOST", "127.0.0.1"),
        port=int(disco_env("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
