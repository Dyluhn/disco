"""Run the agent-server with uvicorn: `python -m disco.agent_server`.

Builds the shared event store + the live ConversationRuntime (real Qwen-backed
loop) and serves the wire/REST surface. Env (DISCO_* preferred; legacy PMX_* honored):
  DISCO_HOST (default 127.0.0.1), DISCO_PORT (default 8000),
  DISCO_DB   (default ./disco.db — shared with the app-server).
  DISCO_SANDBOX (unset → config-driven; the persisted Settings default is "local") —
    an OPTIONAL startup override of the Build surface's sandbox backend:
    "process" (dev, runs on the HOST, NO isolation), "local" (a real container via the
    rootless Podman/Docker socket — runc/crun), "gvisor"/"podman" (remote-host backends).
  DISCO_LOCAL_SOCKET (default unix:///run/user/1000/podman/podman.sock),
  DISCO_LOCAL_RUNTIME (default runc), DISCO_SANDBOX_IMAGE (default disco-sandbox:base).
"""

from __future__ import annotations

import contextlib
import socket

import uvicorn
from disco.core.auth import local_preview_gateway_host, local_preview_gateway_ports
from disco.core.env import disco_env
from disco.core.llm.secrets import ensure_process_secret_key
from disco.core.store.sqlite import SqliteEventStore

from .app import create_app
from .runtime import ConversationRuntime


def _sandbox_service():
    """An OPTIONAL startup OVERRIDE of the Build sandbox backend. Unset → the runtime
    reads the backend from the persisted Settings (the Sandbox section) per request.
    Set DISCO_SANDBOX=process|local|gvisor|podman to force one (with the env connection)."""
    backend_raw = disco_env("SANDBOX")
    if not backend_raw:
        return None  # config-driven (the Settings selector)
    # W3 C-1: validate the dev override against the fail-CLOSED allowlist — a typo'd
    # DISCO_SANDBOX must error loudly, never silently fall through to host execution.
    from typing import Literal, cast

    backend = backend_raw.strip().lower()
    if backend not in ("gvisor", "local", "podman", "process"):
        raise ValueError(
            f"DISCO_SANDBOX={backend_raw!r} is not a valid sandbox backend "
            "(expected one of: gvisor, local, podman, process)"
        )
    backend = cast(Literal["gvisor", "local", "podman", "process"], backend)
    from disco.core.llm import SandboxSettings

    from .runtime import build_sandbox_service

    # disco_env returns str|None by type, but the defaults below are non-None
    # string literals so the runtime values are always `str` here.
    local_runtime = disco_env("LOCAL_RUNTIME", "runc")
    assert local_runtime is not None
    local_socket = disco_env("LOCAL_SOCKET", "unix:///run/user/1000/podman/podman.sock")
    assert local_socket is not None
    sandbox_image = disco_env("SANDBOX_IMAGE", "disco-sandbox:base")
    assert sandbox_image is not None
    return build_sandbox_service(
        SandboxSettings(
            backend=backend,
            runtime=local_runtime,
            docker_socket=local_socket,
            image=sandbox_image,
        )
    )


def main() -> None:
    # Surface app-logger output (providers, the loop) alongside uvicorn's access log —
    # uvicorn configures only its own loggers, so without this the disco.*
    # INFO traces (e.g. "exa search …", "local extract …") are silently dropped.
    import logging

    log_level = disco_env("LOG_LEVEL", "INFO")
    assert log_level is not None  # default above is non-None
    level = log_level.upper()
    if disco_env("LOG_JSON") == "1":
        # Structured JSON logs (core.obs span records become one JSON object/line —
        # greppable + trace-assertable). Plain text otherwise.
        from disco.core.obs import install_json_logging

        install_json_logging(level)
    else:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ensure_process_secret_key()
    db_path = disco_env("DB", "disco.db")
    assert db_path is not None  # default above is non-None
    store = SqliteEventStore(db_path)
    runtime = ConversationRuntime(store, sandbox_service=_sandbox_service())
    app = create_app(store, runtime=runtime)
    host = disco_env("HOST", "127.0.0.1")
    assert host is not None  # default above is non-None
    port = disco_env("PORT", "8000")
    assert port is not None  # default above is non-None
    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        workers=1,
        # Uvicorn's legacy websockets adapter imports APIs deprecated by
        # websockets 14+. The sans-I/O adapter is the maintained path and
        # preserves the ASGI WebSocket contract used by the agent surface.
        ws="websockets-sansio",
        # Do NOT let a forwarding header rewrite the ASGI client address.
        # uvicorn's default (proxy_headers=True, forwarded_allow_ips="127.0.0.1")
        # rewrites scope["client"] from X-Forwarded-For without checking it is an
        # IP, so on a loopback-bound host-process run any local caller could
        # present an arbitrary client host. The loopback checks in auth.py are
        # about the real TCP peer; nothing here needs the forwarded value (the
        # https signal is read from the header directly).
        proxy_headers=False,
    )
    sockets = [config.bind_socket()]
    gateway_bind = (disco_env("LOCAL_PREVIEW_BIND", "") or "").strip()
    try:
        for gateway_port in local_preview_gateway_ports():
            gateway = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            gateway.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Host-process deployments bind the exact loopback identity for
            # cookie isolation. Containers bind all internal interfaces; their
            # published host mappings remain exact 127/8 literals.
            bind_host = gateway_bind or local_preview_gateway_host(gateway_port)
            gateway.bind((bind_host, gateway_port))
            gateway.listen(socket.SOMAXCONN)
            gateway.set_inheritable(True)
            sockets.append(gateway)
        uvicorn.Server(config).run(sockets=sockets)
    finally:
        for bound in sockets:
            with contextlib.suppress(OSError):
                bound.close()


if __name__ == "__main__":
    main()
