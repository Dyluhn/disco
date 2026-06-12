"""Allowlisting egress proxy — the per-connection enforcer behind the sandbox's
egress allowlist (tool-sandbox-contract §7 rule 3; Cluster 3).

THE PROBLEM IT SOLVES. A container backend can only express network as a binary:
`network_mode="none"` (sealed) or `"bridge"` (the whole internet). So a box with
a NON-EMPTY `egress_allow` list was getting FULL network and the allowlist was
never consulted — a false security guarantee. This proxy makes the allowlist
real: the sandbox is placed on an INTERNAL (no-NAT) network whose ONLY route out
is this proxy, and the proxy refuses any host the allowlist doesn't name. Because
the sandbox has no other route, even a proxy-unaware client cannot escape — it
simply has nowhere to send packets except here.

DESIGN. Self-contained and stdlib-only (no disco imports) ON PURPOSE: the
same file is `put_archive`'d into a stock `python:slim` sidecar and run with
`python egress_proxy.py --port 8888 --allow .pypi.org,api.github.com`, AND
imported directly by the unit tests (which spin it up on localhost and point a
real client at it — no Docker needed). Matching semantics mirror
`SandboxSpec.egress_allowed` exactly (a shared-semantics test pins this).

SCOPE. Supports the two shapes a package manager / HTTP client actually emits:
HTTPS via `CONNECT host:port` tunneling, and plain HTTP via absolute-form request
lines (`GET http://host/path`). Both consult the same allow predicate; a denied
host gets a clean `403` and the connection closes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable, Iterable

__all__ = ["host_allowed", "make_predicate", "AllowlistProxy", "main"]


# ---- the allow predicate (semantics mirror SandboxSpec.egress_allowed) -------


def host_allowed(host: str, entries: Iterable[str]) -> bool:
    """Deny-by-default host match. An entry is either an exact host
    (`api.example.com`) or a leading-dot suffix (`.example.com`) matching the
    apex and any subdomain. Empty allowlist → everything denied."""
    h = host.lower().strip()
    # strip a port if one slipped in (CONNECT targets are host:port)
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    for entry in entries:
        e = entry.lower().strip()
        if not e:
            continue
        if e.startswith("."):
            if h == e[1:] or h.endswith(e):
                return True
        elif h == e:
            return True
    return False


def make_predicate(entries: Iterable[str]) -> Callable[[str], bool]:
    """Freeze an allowlist into a `(host) -> bool` predicate."""
    frozen = tuple(entries)
    return lambda host: host_allowed(host, frozen)


# ---- the proxy --------------------------------------------------------------

_PIPE_CHUNK = 64 * 1024
_DENY_BODY = b"egress denied by sandbox allowlist"
_FORBIDDEN = (
    b"HTTP/1.1 403 Forbidden\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: " + str(len(_DENY_BODY)).encode() + b"\r\n"
    b"Connection: close\r\n\r\n" + _DENY_BODY
)
_BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"
_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"


class AllowlistProxy:
    """An asyncio forward proxy that allows a connection only if `allow(host)`.

    `denied` counts refused connections and `allowed` counts permitted ones — the
    live-verify + unit tests assert on these to prove enforcement actually fired."""

    def __init__(self, allow: Callable[[str], bool], *, host: str = "0.0.0.0", port: int = 8888):
        self._allow = allow
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self.allowed = 0
        self.denied = 0

    @property
    def port(self) -> int:
        """The bound port (useful when constructed with port=0 in tests)."""
        if self._server is None:
            return self._port
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.split()
            if len(parts) < 2:
                await self._reply(writer, _BAD_REQUEST)
                return
            method, target = parts[0].upper(), parts[1]
            if method == b"CONNECT":
                await self._do_connect(target.decode("latin1"), reader, writer)
            else:
                await self._do_http(method, target.decode("latin1"), reader, writer)
        except Exception:  # noqa: BLE001 — never let one bad client crash the proxy
            with _suppress():
                writer.close()

    async def _do_connect(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host, _, port_s = target.partition(":")
        port = int(port_s) if port_s.isdigit() else 443
        # drain the rest of the CONNECT headers (up to the blank line)
        await _drain_headers(reader)
        if not self._allow(host):
            self.denied += 1
            await self._reply(writer, _FORBIDDEN)
            return
        try:
            up_r, up_w = await asyncio.open_connection(host, port)
        except Exception:  # noqa: BLE001 — upstream unreachable
            await self._reply(writer, _BAD_REQUEST)
            return
        self.allowed += 1
        writer.write(_ESTABLISHED)
        await writer.drain()
        await _pipe(reader, writer, up_r, up_w)

    async def _do_http(
        self,
        method: bytes,
        target: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # Absolute-form request line: GET http://host[:port]/path HTTP/1.1
        host, port, path = _split_absolute_uri(target)
        if host is None:
            await self._reply(writer, _BAD_REQUEST)
            return
        headers = await _read_headers(reader)
        if not self._allow(host):
            self.denied += 1
            await self._reply(writer, _FORBIDDEN)
            return
        try:
            up_r, up_w = await asyncio.open_connection(host, port)
        except Exception:  # noqa: BLE001
            await self._reply(writer, _BAD_REQUEST)
            return
        self.allowed += 1
        # Re-emit in origin form (path, not absolute URI) to the upstream.
        up_w.write(method + b" " + path.encode("latin1") + b" HTTP/1.1\r\n")
        up_w.write(headers)
        await up_w.drain()
        await _pipe(reader, writer, up_r, up_w)

    async def _reply(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        with _suppress():
            writer.write(payload)
            await writer.drain()
            writer.close()


# ---- small stream helpers ---------------------------------------------------


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True  # swallow teardown errors


async def _drain_headers(reader: asyncio.StreamReader) -> None:
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            return


async def _read_headers(reader: asyncio.StreamReader) -> bytes:
    buf = bytearray()
    while True:
        line = await reader.readline()
        buf += line
        if line in (b"\r\n", b"\n", b""):
            return bytes(buf)


def _split_absolute_uri(uri: str) -> tuple[str | None, int, str]:
    if "://" not in uri:
        return None, 0, ""
    _, _, rest = uri.partition("://")
    authority, slash, path = rest.partition("/")
    host, _, port_s = authority.partition(":")
    port = int(port_s) if port_s.isdigit() else 80
    return (host or None), port, ("/" + path if slash else "/")


async def _pipe(
    c_r: asyncio.StreamReader,
    c_w: asyncio.StreamWriter,
    u_r: asyncio.StreamReader,
    u_w: asyncio.StreamWriter,
) -> None:
    async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(_PIPE_CHUNK)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except Exception:  # noqa: BLE001 — half-close on any stream error
            pass
        finally:
            with _suppress():
                dst.close()

    await asyncio.gather(copy(c_r, u_w), copy(u_r, c_w))


# ---- sidecar entrypoint -----------------------------------------------------


async def _run(port: int, entries: list[str]) -> None:
    proxy = AllowlistProxy(make_predicate(entries), port=port)
    await proxy.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Allowlisting egress proxy (sandbox sidecar).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("EGRESS_PORT", "8888")))
    parser.add_argument(
        "--allow",
        default=os.environ.get("EGRESS_ALLOW", ""),
        help="Comma-separated allowlist (exact host or .suffix). Empty = deny all.",
    )
    args = parser.parse_args(argv)
    entries = [e for e in (x.strip() for x in args.allow.split(",")) if e]
    asyncio.run(_run(args.port, entries))


if __name__ == "__main__":
    main()
