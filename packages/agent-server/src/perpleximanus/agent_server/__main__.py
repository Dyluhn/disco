"""Run the agent-server with uvicorn: `python -m perpleximanus.agent_server`.

Builds the shared event store + the live ConversationRuntime (real Qwen-backed
loop) and serves the wire/REST surface. Env:
  PMX_HOST (default 127.0.0.1), PMX_PORT (default 8000),
  PMX_DB   (default ./perpleximanus.db — shared with the app-server).
  PMX_SANDBOX (default "process") — the Build surface's sandbox backend:
    "process" (dev, runs on the HOST), "local" (a real container via the rootless
    Podman/Docker socket — runc/crun), "gvisor"/"podman" (the remote-host backends).
  PMX_LOCAL_SOCKET (default unix:///run/user/1000/podman/podman.sock),
  PMX_LOCAL_RUNTIME (default runc), PMX_SANDBOX_IMAGE (default pmx-sandbox:base).
"""

from __future__ import annotations

import os

import uvicorn
from perpleximanus.core.store.sqlite import SqliteEventStore

from .app import create_app
from .runtime import ConversationRuntime


def _sandbox_service():
    """An OPTIONAL startup OVERRIDE of the Build sandbox backend. Unset → the runtime
    reads the backend from the persisted Settings (the Sandbox section) per request.
    Set PMX_SANDBOX=process|local|gvisor|podman to force one (with the env connection)."""
    backend = os.environ.get("PMX_SANDBOX")
    if not backend:
        return None  # config-driven (the Settings selector)
    from perpleximanus.core.llm import SandboxSettings

    from .runtime import build_sandbox_service

    return build_sandbox_service(
        SandboxSettings(
            backend=backend.lower(),
            runtime=os.environ.get("PMX_LOCAL_RUNTIME", "runc"),
            docker_socket=os.environ.get(
                "PMX_LOCAL_SOCKET", "unix:///run/user/1000/podman/podman.sock"
            ),
            image=os.environ.get("PMX_SANDBOX_IMAGE", "pmx-sandbox:base"),
        )
    )


def main() -> None:
    store = SqliteEventStore(os.environ.get("PMX_DB", "perpleximanus.db"))
    runtime = ConversationRuntime(store, sandbox_service=_sandbox_service())
    app = create_app(store, runtime=runtime)
    uvicorn.run(
        app,
        host=os.environ.get("PMX_HOST", "127.0.0.1"),
        port=int(os.environ.get("PMX_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
