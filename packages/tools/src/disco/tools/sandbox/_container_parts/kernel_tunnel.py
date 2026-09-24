"""Private kernel transport through the sandbox daemon's authenticated exec API.

A mounted daemon socket does not put the agent-server in the daemon host's
network namespace. Published 127.0.0.1 ports therefore cannot be used from a
Compose sibling. Relay only the kernel port through exec, keeping both ends
loopback-bound and retaining the gateway's token authentication.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import subprocess
import threading
from collections.abc import Callable
from typing import Any, Protocol

from ..base import SandboxError

# Fixed endpoint: callers cannot turn this plumbing into a general network proxy.
_KERNEL_PORT = 8899
_CHUNK = 65536
_MAX_CONNECTIONS = 8
_GUEST_RELAY = """
import os, socket, threading
s = socket.create_connection(('127.0.0.1', 8899), timeout=5)
s.settimeout(None)
def upload():
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            s.sendall(data)
    except OSError:
        pass
    finally:
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
threading.Thread(target=upload, daemon=True).start()
try:
    while True:
        data = s.recv(65536)
        if not data:
            break
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]
finally:
    s.close()
"""


class ExecStream(Protocol):
    def read(self) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class PodmanExecStream:
    def __init__(self, endpoint: str, container_name: str) -> None:
        self._process = subprocess.Popen(
            [
                "podman",
                "--url",
                endpoint,
                "exec",
                "-i",
                container_name,
                "python3",
                "-u",
                "-c",
                _GUEST_RELAY,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._close_lock = threading.Lock()

    def read(self) -> bytes:
        assert self._process.stdout is not None
        return self._process.stdout.read1(_CHUNK)

    def write(self, data: bytes) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(data)
        self._process.stdin.flush()

    def close(self) -> None:
        with self._close_lock:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=1)
            for pipe in (self._process.stdin, self._process.stdout):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()


class DockerExecStream:
    def __init__(self, container: Any) -> None:
        result = container.exec_run(
            ["python3", "-u", "-c", _GUEST_RELAY],
            stdin=True,
            stdout=True,
            stderr=False,
            tty=False,
            socket=True,
        )
        self._stream = result.output
        self._socket = getattr(self._stream, "_sock", self._stream)

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            part = self._socket.recv(size - len(chunks))
            if not part:
                if chunks:
                    raise OSError("truncated sandbox exec frame")
                return b""
            chunks.extend(part)
        return bytes(chunks)

    def read(self) -> bytes:
        # Docker's non-TTY hijacked exec stream has an 8-byte multiplex header.
        while header := self._read_exact(8):
            channel, size = struct.unpack(">BxxxI", header)
            if channel != 1 or size > 8 * 1024 * 1024:
                raise OSError("invalid sandbox exec frame")
            if size:
                data = self._read_exact(size)
                if not data:
                    raise OSError("truncated sandbox exec payload")
                return data
        return b""

    def write(self, data: bytes) -> None:
        self._socket.sendall(data)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._stream.close()


class KernelExecTunnel:
    """One lazy local listener with bounded connections; closed with its sandbox."""

    def __init__(self, open_stream: Callable[[], ExecStream]) -> None:
        self._open_stream = open_stream
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._clients: set[socket.socket] = set()
        self._streams: list[ExecStream] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(_MAX_CONNECTIONS)
        self._listener.settimeout(0.2)
        self._address = self._listener.getsockname()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def forward(self, port: int) -> tuple[str, int]:
        if port != _KERNEL_PORT or self._closed.is_set():
            raise SandboxError("private kernel transport is unavailable for this port")
        return self._address

    @staticmethod
    def _close_client(client: socket.socket) -> None:
        with contextlib.suppress(OSError):
            client.shutdown(socket.SHUT_RDWR)
        client.close()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                client, _peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                self._close_client(client)
                continue
            with self._lock:
                if self._closed.is_set():
                    self._close_client(client)
                    self._slots.release()
                    break
                self._clients.add(client)
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client: socket.socket) -> None:
        stream = None
        try:
            stream = self._open_stream()
            with self._lock:
                if self._closed.is_set():
                    return
                self._streams.append(stream)

            def upload() -> None:
                try:
                    while data := client.recv(_CHUNK):
                        stream.write(data)
                except (OSError, ValueError):
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        stream.close()
                    self._close_client(client)

            threading.Thread(target=upload, daemon=True).start()
            while data := stream.read():
                client.sendall(data)
        except Exception:  # noqa: BLE001 — daemon errors are private diagnostics
            # Readiness and gateway clients report bounded transport errors;
            # never expose daemon exception strings or payloads in logs.
            pass
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            self._close_client(client)
            with self._lock:
                self._clients.discard(client)
                if stream in self._streams:
                    self._streams.remove(stream)
            self._slots.release()

    def close(self) -> None:
        self._closed.set()
        self._listener.close()
        with self._lock:
            clients, streams = list(self._clients), list(self._streams)
        for client in clients:
            self._close_client(client)
        for stream in streams:
            with contextlib.suppress(Exception):
                stream.close()
        self._thread.join(timeout=1)
