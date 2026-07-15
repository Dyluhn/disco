"""[FIX6 — live-proven on real gVisor/runsc] Inbound TCP forwarder for sandbox
preview and internal control transport.

THE PROBLEM. A filtered-egress sandbox sits on an INTERNAL no-NAT docker network
(containment = no route out except the allowlisting proxy sidecar). gVisor (runsc)
FREEZES its netstack at boot — there is no NIC hot-plug — so the sandbox can NEVER
publish its preview port on the host without putting it on a NAT bridge, and a NAT
bridge IS raw egress = broken containment. So the sandbox publishes NOTHING and its
preview is unreachable from the host.

THE FIX (validated end-to-end on real runsc for filtered preview). Keep the sandbox
INTERNAL-only. The hardened dual-homed SIDECAR publishes curated ports on host
loopback and runs THIS forwarder:
it binds ``0.0.0.0:PORT`` on the sidecar and pipes every connection across the
internal net to ``<sandbox_internal_ip>:PORT``. The host reaches the preview via the
sidecar's published port; the sandbox keeps ZERO direct egress. Sealed mode selects
only INTERNAL_PORTS and installs no egress proxy; filtered/public modes select the
full curated set. The forward is a transparent byte pipe, so websockets / Vite HMR
pass through unchanged.

stdlib-only (the sandbox base image's ``python3`` runs it with no install, honoring
the never-pull rule). Delivered onto the sidecar via ``put_archive`` (NOT piped on a
detached ``docker exec`` stdin — a live gotcha: detached exec drops stdin), then
launched detached.

CLI: ``python3 inbound_forward.py <dest_host> <port> [<port> ...]`` — for each port,
bind ``0.0.0.0:<port>`` and forward to ``<dest_host>:<port>`` (one listener thread
per port, two pipe threads per connection).
"""

from __future__ import annotations

import socket
import sys
import threading

# Per-recv chunk. 64 KiB is the usual sweet spot for a transparent TCP relay.
_CHUNK = 65536
_BACKLOG = 128


def parse_args(argv: list[str]) -> tuple[str, list[int]]:
    """Parse ``<dest_host> <port> [<port> ...]`` → (dest_host, [ports]).

    Raises ``ValueError`` on too few args or a non-integer port — the caller
    (``main``) maps that to a non-zero exit. Kept separate from ``main`` so the
    parse is unit-testable without binding any socket."""
    if len(argv) < 2:
        raise ValueError("usage: inbound_forward.py <dest_host> <port> [<port> ...]")
    dest_host = argv[0]
    ports = [int(p) for p in argv[1:]]  # ValueError propagates for a bad port
    return dest_host, ports


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes src→dst until EOF, then half-close dst's write side so the peer
    observes the close (needed for clean HTTP/keep-alive + websocket teardown)."""
    try:
        while True:
            data = src.recv(_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, dest_host: str, dest_port: int) -> None:
    """Bridge one accepted connection to ``dest_host:dest_port`` with two pipe
    threads (each direction), closing both sockets when both directions finish."""
    try:
        upstream = socket.create_connection((dest_host, dest_port))
    except OSError:
        client.close()
        return
    t_up = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
    t_down = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
    t_up.start()
    t_down.start()
    t_up.join()
    t_down.join()
    for sock in (client, upstream):
        try:
            sock.close()
        except OSError:
            pass


def _make_listener(port: int) -> socket.socket:
    """A bound, listening ``0.0.0.0:port`` socket. Pass ``0`` to get an ephemeral
    port (read it back via ``getsockname()`` — used by the round-trip unit test)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen(_BACKLOG)
    return listener


def _serve_forever(listener: socket.socket, dest_host: str, dest_port: int) -> None:
    """Accept loop: spawn a handler thread per connection. Returns when the
    listener is closed (``accept`` raises ``OSError`` on a closed socket)."""
    while True:
        try:
            client, _addr = listener.accept()
        except OSError:
            break
        threading.Thread(target=_handle, args=(client, dest_host, dest_port), daemon=True).start()


def _serve(dest_host: str, listen_port: int, dest_port: int | None = None) -> None:
    """Bind ``0.0.0.0:listen_port`` and forward to ``dest_host:dest_port`` (default
    same port). Blocks forever serving the accept loop."""
    target_port = listen_port if dest_port is None else dest_port
    _serve_forever(_make_listener(listen_port), dest_host, target_port)


def main(argv: list[str]) -> int:
    try:
        dest_host, ports = parse_args(argv)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    threads: list[threading.Thread] = []
    for port in ports:
        thread = threading.Thread(target=_serve, args=(dest_host, port), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()  # listeners never return → block forever, forwarding
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
