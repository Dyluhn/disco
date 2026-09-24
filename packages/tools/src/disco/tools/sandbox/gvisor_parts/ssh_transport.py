"""Per-client fix for docker-py's shell SSH socket teardown race.

Docker 7.1's SSHSocket.close writes to an SSH child that can already have
exited after an exec response. EPIPE during that cleanup discards successfully
read output. Only close-time broken pipes are ignored; commands are never
retried and send/read failures still propagate. Keep this import lazy so local
and injected Docker clients do not depend on the SSH transport.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import threading
from typing import Any, cast

from docker.transport.sshconn import (
    SSHConnection,
    SSHConnectionPool,
    SSHHTTPAdapter,
    SSHSocket,
)


class _ClosingSSHSocket(SSHSocket):
    def __init__(self, host: str) -> None:
        super().__init__(host)
        self._close_lock = threading.Lock()

    def close(self) -> None:
        with self._close_lock:
            proc = self.proc
            try:
                # Preserve the SDK's normal shutdown, but an exited dial-stdio
                # process has no stdin reader. This is not a failed command.
                with contextlib.suppress(BrokenPipeError):
                    super().close()
            finally:
                self.proc = None
                try:
                    if proc is not None:
                        try:
                            if proc.poll() is None:
                                with contextlib.suppress(ProcessLookupError):
                                    proc.terminate()
                            try:
                                proc.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                with contextlib.suppress(ProcessLookupError):
                                    proc.kill()
                                proc.wait(timeout=1)
                        finally:
                            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                                if pipe is not None:
                                    with contextlib.suppress(BrokenPipeError):
                                        pipe.close()
                finally:
                    socket.socket.close(self)


class _ClosingSSHConnection(SSHConnection):
    def connect(self) -> None:
        if self.ssh_transport:
            return super().connect()
        assert self.ssh_host is not None
        sock = _ClosingSSHSocket(self.ssh_host)
        try:
            sock.settimeout(self.timeout)
            sock.connect()
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class _ClosingSSHConnectionPool(SSHConnectionPool):
    def _new_conn(self) -> Any:
        # The SDK overwrites urllib3's Timeout field with a numeric timeout.
        return _ClosingSSHConnection(self.ssh_transport, cast(Any, self.timeout), self.ssh_host)


class _ClosingSSHHTTPAdapter(SSHHTTPAdapter):
    def get_connection(self, url: str | bytes, proxies: Any = None) -> Any:
        if self.ssh_client:
            return super().get_connection(url, proxies)
        return _ClosingSSHConnectionPool(
            ssh_client=None,
            timeout=self.timeout,
            maxsize=self.max_pool_size,
            host=self.ssh_host,
        )


def configure_ssh_cleanup(client: Any, base_url: str, timeout: float) -> None:
    """Replace only this shell-out client's adapter before any exec operations."""
    api = client.api
    previous = api.get_adapter("http+docker://ssh")
    adapter = _ClosingSSHHTTPAdapter(
        base_url,
        timeout=cast(Any, timeout),  # docker-py accepts fractional socket timeouts.
        max_pool_size=previous.max_pool_size,
        shell_out=True,
    )
    api.mount("http+docker://ssh", adapter)
    api._custom_adapter = adapter  # SDK bookkeeping, matching APIClient.__init__.
    previous.close()
