"""Narrow sandbox-to-host relay for the host-service capability.

This is deliberately not a general reverse proxy.  The sandbox can choose only a
strict host-service name and JSON body; the operator chooses the sole upstream.
Authentication remains the agent-server endpoint's responsibility.  The relay
never logs request headers or bodies.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import socket
import socketserver
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Final, cast

HOST_SERVICE_RELAY_PORT: Final = 3211
MAX_HEADER_BYTES: Final = 16 * 1024
MAX_HEADER_COUNT: Final = 64
MAX_BODY_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 256 * 1024
MAX_SERVICE_BYTES: Final = 128
_SERVICE_PATH_PREFIX: Final = b"/_disco/svc/"
MAX_SERVICE_PATH_BYTES: Final = len(_SERVICE_PATH_PREFIX) + MAX_SERVICE_BYTES
MAX_CONCURRENT_REQUESTS: Final = 16

_SERVICE_PATH = re.compile(rb"/_disco/svc/[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BEARER = re.compile(rb"Bearer a2v0\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}")
_FORBIDDEN_HEADERS = frozenset(
    {
        b"connection",
        b"content-encoding",
        b"expect",
        b"forwarded",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-port",
        b"x-forwarded-proto",
        b"x-real-ip",
    }
)


@dataclass(frozen=True)
class RelayUpstream:
    scheme: str
    host: str
    port: int

    @property
    def authority(self) -> str:
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


class RelayConfigurationError(RuntimeError):
    """The operator-pinned relay origin is invalid or unavailable at provisioning."""


def parse_upstream(value: str) -> RelayUpstream:
    """Validate the one operator-pinned upstream; plaintext is loopback-only."""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RelayConfigurationError("invalid host-service relay upstream") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RelayConfigurationError(
            "host-service relay upstream must be an http(s) origin without credentials/path"
        )
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname
    if parsed.scheme == "http" and not _is_loopback(host):
        raise RelayConfigurationError(
            "host-service relay refuses plaintext to a non-loopback upstream; configure HTTPS"
        )
    return RelayUpstream(parsed.scheme, host, port)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _RelayRequestError(Exception):
    def __init__(self, status: int, message: bytes) -> None:
        super().__init__(status)
        self.status = status
        self.message = message


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _RelayRequestError(400, b'{"error":"duplicate JSON key"}')
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise _RelayRequestError(400, b'{"error":"invalid JSON body"}')


class _RelayHandler(socketserver.BaseRequestHandler):
    @property
    def relay_server(self) -> CapabilityRelayServer:
        return cast(CapabilityRelayServer, self.server)

    def handle(self) -> None:
        deadline = time.monotonic() + self.relay_server.total_timeout_s
        self.request.settimeout(self.relay_server.total_timeout_s)
        try:
            path, headers, body = self._read_request(deadline)
            status, response = self.relay_server.forward(path, headers, body, deadline=deadline)
            self._respond(status, response)
        except _RelayRequestError as exc:
            self._respond(exc.status, exc.message)
        except TimeoutError:
            self._respond(504, b'{"error":"host service timed out"}')
        except (OSError, http.client.HTTPException):
            self._respond(502, b'{"error":"host service unavailable"}')

    def _read_request(self, deadline: float) -> tuple[str, dict[bytes, bytes], bytes]:
        head, buffered = self._read_head_block(deadline)
        lines = self._validate_head_framing(head)
        method, target, version = self._parse_request_line(lines[0])
        self._validate_request_line(method, target, version)
        headers = self._parse_headers(lines[1:])
        self._validate_headers(headers)
        length = self._parse_content_length(headers)
        body = self._read_body(buffered, length, deadline)
        self._reject_pipelining()
        self._parse_json_body(bytes(body))
        return target.decode("ascii"), headers, bytes(body)

    def _read_head_block(self, deadline: float) -> tuple[bytes, bytes]:
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            self._set_request_deadline(deadline)
            chunk = self.request.recv(min(4096, MAX_HEADER_BYTES + 1 - len(raw)))
            if not chunk:
                raise _RelayRequestError(400, b'{"error":"incomplete request"}')
            raw.extend(chunk)
            if len(raw) > MAX_HEADER_BYTES:
                raise _RelayRequestError(431, b'{"error":"headers too large"}')
        head, buffered = bytes(raw).split(b"\r\n\r\n", 1)
        return head, buffered

    @staticmethod
    def _validate_head_framing(head: bytes) -> list[bytes]:
        if b"\n" in head.replace(b"\r\n", b""):
            raise _RelayRequestError(400, b'{"error":"malformed headers"}')
        lines = head.split(b"\r\n")
        if len(lines) - 1 > MAX_HEADER_COUNT:
            raise _RelayRequestError(431, b'{"error":"too many headers"}')
        return lines

    @staticmethod
    def _parse_request_line(line0: bytes) -> tuple[bytes, bytes, bytes]:
        try:
            method, target, version = line0.split(b" ")
        except ValueError as exc:
            raise _RelayRequestError(400, b'{"error":"malformed request line"}') from exc
        return method, target, version

    @staticmethod
    def _validate_request_line(method: bytes, target: bytes, version: bytes) -> None:
        if method != b"POST":
            raise _RelayRequestError(405, b'{"error":"POST required"}')
        if (
            version != b"HTTP/1.1"
            or len(target) > MAX_SERVICE_PATH_BYTES
            or not _SERVICE_PATH.fullmatch(target)
        ):
            raise _RelayRequestError(404, b'{"error":"route not found"}')

    def _validate_headers(self, headers: dict[bytes, bytes]) -> None:
        expected_authority = self.relay_server.allowed_authority.encode("ascii")
        if headers.get(b"host") != expected_authority:
            raise _RelayRequestError(400, b'{"error":"invalid host"}')
        if any(name in headers for name in _FORBIDDEN_HEADERS):
            raise _RelayRequestError(400, b'{"error":"unsupported request framing"}')
        if headers.get(b"content-type") != b"application/json":
            raise _RelayRequestError(415, b'{"error":"application/json required"}')
        authorization = headers.get(b"authorization", b"")
        if not _BEARER.fullmatch(authorization):
            raise _RelayRequestError(401, b'{"error":"bearer required"}')

    @staticmethod
    def _parse_content_length(headers: dict[bytes, bytes]) -> int:
        length_raw = headers.get(b"content-length")
        if length_raw is None or not length_raw.isdigit() or length_raw.startswith(b"0"):
            if length_raw != b"0":
                raise _RelayRequestError(400, b'{"error":"invalid content length"}')
        length = int(length_raw or b"-1")
        if length < 0 or length > MAX_BODY_BYTES:
            raise _RelayRequestError(413, b'{"error":"body too large"}')
        return length

    def _read_body(self, buffered: bytes, length: int, deadline: float) -> bytearray:
        body = bytearray(buffered)
        if len(body) > length:
            raise _RelayRequestError(400, b'{"error":"conflicting request length"}')
        while len(body) < length:
            self._set_request_deadline(deadline)
            chunk = self.request.recv(min(8192, length - len(body)))
            if not chunk:
                raise _RelayRequestError(400, b'{"error":"incomplete body"}')
            body.extend(chunk)
        return body

    def _reject_pipelining(self) -> None:
        # No keep-alive/pipelining: detect already-buffered bytes beyond the declared body.
        self.request.settimeout(0.01)
        try:
            if self.request.recv(1):
                raise _RelayRequestError(400, b'{"error":"conflicting request length"}')
        except TimeoutError:
            pass
        finally:
            self.request.settimeout(self.relay_server.total_timeout_s)

    @staticmethod
    def _parse_json_body(body: bytes) -> None:
        try:
            parsed = json.loads(
                bytes(body).decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RelayRequestError(400, b'{"error":"invalid JSON body"}') from exc
        if not isinstance(parsed, dict):
            raise _RelayRequestError(400, b'{"error":"JSON object required"}')

    def _set_request_deadline(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _RelayRequestError(504, b'{"error":"request timed out"}')
        self.request.settimeout(remaining)

    @staticmethod
    def _parse_headers(lines: list[bytes]) -> dict[bytes, bytes]:
        result: dict[bytes, bytes] = {}
        for line in lines:
            if not line or line[:1] in b" \t" or b":" not in line:
                raise _RelayRequestError(400, b'{"error":"malformed headers"}')
            name, value = line.split(b":", 1)
            lower = name.lower()
            value = value.strip(b" \t")
            if (
                not _HEADER_NAME.fullmatch(name)
                or lower in result
                or any(byte < 32 and byte != 9 for byte in value)
                or 127 in value
            ):
                raise _RelayRequestError(400, b'{"error":"malformed headers"}')
            result[lower] = value
        return result

    def _respond(self, status: int, body: bytes) -> None:
        reason = {
            200: b"OK",
            400: b"Bad Request",
            401: b"Unauthorized",
            404: b"Not Found",
            405: b"Method Not Allowed",
            413: b"Content Too Large",
            415: b"Unsupported Media Type",
            431: b"Request Header Fields Too Large",
            502: b"Bad Gateway",
            504: b"Gateway Timeout",
        }.get(status, b"Response")
        response = (
            b"HTTP/1.1 "
            + str(status).encode("ascii")
            + b" "
            + reason
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
            + body
        )
        try:
            self.request.sendall(response)
        except OSError:
            pass


class CapabilityRelayServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Lifecycle-owned, bounded relay server.  No request logging is implemented."""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        upstream: str,
        *,
        allowed_authority: str | None = None,
        connect_timeout_s: float = 2.0,
        read_timeout_s: float = 5.0,
        total_timeout_s: float = 7.0,
    ) -> None:
        self.upstream = parse_upstream(upstream)
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.total_timeout_s = total_timeout_s
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._upstream_addresses = _resolve_upstream(
            self.upstream, min(connect_timeout_s, total_timeout_s)
        )
        super().__init__((listen_host, listen_port), _RelayHandler, bind_and_activate=True)
        actual_host, actual_port = self.server_address[:2]
        self.allowed_authority = allowed_authority or f"{actual_host}:{actual_port}"

    def handle_error(self, request: object, client_address: object) -> None:
        # BaseServer otherwise prints handler tracebacks, which could include headers.
        return

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 28\r\nCache-Control: no-store\r\n"
                    b'Connection: close\r\n\r\n{"error":"relay overloaded"}'
                )
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def forward(
        self,
        path: str,
        headers: dict[bytes, bytes],
        body: bytes,
        *,
        deadline: float | None = None,
    ) -> tuple[int, bytes]:
        started = time.monotonic()
        deadline = deadline or started + self.total_timeout_s
        conn = self._open_upstream_connection(deadline)
        try:
            self._send_request(conn, path, headers, body, deadline, started)
            response, lengths = self._get_validated_response(conn)
            data = self._read_response_body(conn, response, deadline)
            self._validate_response_length(data, lengths)
            self._validate_response_json(data)
            if time.monotonic() - started > self.total_timeout_s:
                raise _RelayRequestError(504, b'{"error":"host service timed out"}')
            status = response.status if 200 <= response.status <= 599 else 502
            return status, data
        finally:
            conn.close()

    def _open_upstream_connection(self, deadline: float) -> http.client.HTTPConnection:
        sock = _connect_pinned(
            self._upstream_addresses,
            min(self.connect_timeout_s, max(0.01, deadline - time.monotonic())),
        )
        if self.upstream.scheme == "https":
            try:
                sock.settimeout(min(self.connect_timeout_s, max(0.01, deadline - time.monotonic())))
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self.upstream.host
                )
            except Exception:
                sock.close()
                raise
        conn = http.client.HTTPConnection(
            self.upstream.host, self.upstream.port, timeout=self.connect_timeout_s
        )
        conn.sock = sock
        return conn

    def _send_request(
        self,
        conn: http.client.HTTPConnection,
        path: str,
        headers: dict[bytes, bytes],
        body: bytes,
        deadline: float,
        started: float,
    ) -> None:
        outbound = {
            "Host": self.upstream.authority,
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Authorization": headers[b"authorization"].decode("ascii"),
        }
        conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        for name, value in outbound.items():
            conn.putheader(name, value)
        if conn.sock is not None:
            conn.sock.settimeout(max(0.01, deadline - time.monotonic()))
        conn.endheaders(body)
        if time.monotonic() - started > self.total_timeout_s:
            raise _RelayRequestError(504, b'{"error":"host service timed out"}')
        if conn.sock is not None:
            conn.sock.settimeout(min(self.read_timeout_s, max(0.01, deadline - time.monotonic())))

    @staticmethod
    def _get_validated_response(
        conn: http.client.HTTPConnection,
    ) -> tuple[http.client.HTTPResponse, list[str]]:
        response = conn.getresponse()
        if 300 <= response.status < 400:
            raise _RelayRequestError(502, b'{"error":"upstream redirect refused"}')
        media_type = (response.getheader("Content-Type") or "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json" or response.getheader(
            "Content-Encoding"
        ):
            raise _RelayRequestError(502, b'{"error":"invalid upstream response"}')
        lengths = response.headers.get_all("Content-Length", failobj=[]) or []
        if (
            response.getheader("Transfer-Encoding")
            or len(lengths) != 1
            or not lengths[0].isdigit()
        ):
            raise _RelayRequestError(502, b'{"error":"invalid upstream response"}')
        if int(lengths[0]) > MAX_RESPONSE_BYTES:
            raise _RelayRequestError(502, b'{"error":"upstream response too large"}')
        return response, lengths

    def _read_response_body(
        self,
        conn: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        deadline: float,
    ) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while received <= MAX_RESPONSE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _RelayRequestError(504, b'{"error":"host service timed out"}')
            if conn.sock is not None:
                conn.sock.settimeout(min(self.read_timeout_s, remaining))
            chunk = response.read(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_RESPONSE_BYTES:
            raise _RelayRequestError(502, b'{"error":"upstream response too large"}')
        return data

    @staticmethod
    def _validate_response_length(data: bytes, lengths: list[str]) -> None:
        if len(data) != int(lengths[0]):
            raise _RelayRequestError(502, b'{"error":"incomplete upstream response"}')

    @staticmethod
    def _validate_response_json(data: bytes) -> None:
        try:
            json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RelayRequestError(502, b'{"error":"invalid upstream response"}') from exc


def _resolve_upstream(
    upstream: RelayUpstream, timeout_s: float
) -> tuple[tuple[int, int, int, tuple[object, ...]], ...]:
    """Resolve once at provisioning with a hard wait bound; pin the resulting IPs."""
    results: Queue[object] = Queue(maxsize=1)

    def _worker() -> None:
        try:
            infos = socket.getaddrinfo(upstream.host, upstream.port, type=socket.SOCK_STREAM)
            results.put(infos)
        except OSError as exc:
            results.put(exc)

    threading.Thread(target=_worker, name="disco-svc-relay-dns", daemon=True).start()
    try:
        value = results.get(timeout=timeout_s)
    except Empty as exc:
        raise RelayConfigurationError(
            "host-service relay upstream DNS timed out during provisioning"
        ) from exc
    if isinstance(value, OSError):
        raise RelayConfigurationError(
            "host-service relay upstream could not be resolved"
        ) from value
    addresses: list[tuple[int, int, int, tuple[object, ...]]] = []
    if not isinstance(value, list):
        raise RelayConfigurationError("host-service relay upstream resolution failed")
    for family, socktype, proto, _canonname, sockaddr in value:
        if socktype == socket.SOCK_STREAM and isinstance(sockaddr, tuple):
            if upstream.scheme == "http":
                try:
                    resolved_ip = ipaddress.ip_address(cast(str, sockaddr[0]))
                except ValueError as exc:
                    raise RelayConfigurationError(
                        "plaintext host-service relay upstream did not resolve to an IP"
                    ) from exc
                if not resolved_ip.is_loopback:
                    raise RelayConfigurationError(
                        "plaintext host-service relay upstream resolved outside loopback"
                    )
            addresses.append((family, socktype, proto, sockaddr))
    if not addresses:
        raise RelayConfigurationError("host-service relay upstream has no TCP address")
    return tuple(addresses)


def _connect_pinned(
    addresses: tuple[tuple[int, int, int, tuple[object, ...]], ...], timeout_s: float
) -> socket.socket:
    deadline = time.monotonic() + timeout_s
    last_error: OSError | None = None
    for family, socktype, proto, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
            if time.monotonic() >= deadline:
                break
    raise OSError("host-service upstream connect failed") from last_error


def serve_in_thread(server: CapabilityRelayServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="disco-svc-relay", daemon=True)
    thread.start()
    return thread


def relay_run_argv(upstream: str, authority: str, port: int = HOST_SERVICE_RELAY_PORT) -> list[str]:
    parse_upstream(upstream)
    return [
        "python3",
        "/capability_relay.py",
        "--listen",
        "0.0.0.0",
        "--port",
        str(port),
        "--upstream",
        upstream,
        "--authority",
        authority,
    ]


def relay_readiness_argv(host: str, port: int = HOST_SERVICE_RELAY_PORT) -> list[str]:
    script = (
        "import socket,sys,time\n"
        "deadline=time.monotonic()+5\n"
        "while True:\n"
        " try:\n"
        "  s=socket.create_connection((sys.argv[1],int(sys.argv[2])),.25); s.close(); break\n"
        " except OSError:\n"
        "  if time.monotonic()>=deadline: raise\n"
        "  time.sleep(.1)"
    )
    return ["python3", "-c", script, host, str(port)]


def _main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--listen", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--authority", required=True)
    args = parser.parse_args()
    with CapabilityRelayServer(
        args.listen, args.port, args.upstream, allowed_authority=args.authority
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    _main()
