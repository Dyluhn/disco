"""Bounded, single-dispatch HTTP over an already-owned sandbox exec channel."""

from __future__ import annotations

import base64
import json
import shlex
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from disco.core.loop.preview_target import is_managed_host_preview_port

from .._container import NOVNC_PORT, USER_PORTS

if TYPE_CHECKING:
    from ..base import SandboxInstance
    from ..session import SandboxSession

HttpResponse = tuple[int, list[tuple[str, str]], bytes]
_CLIENT_SOURCE = Path(__file__).with_name("preview_http_client.py").read_text()
_CHUNK_SIZE = 48000


def _encode_request(
    port: int, method: str, target: str, headers: list[tuple[str, str]], body: bytes
) -> str:
    payload = {
        "port": port,
        "method": method,
        "target": target,
        "headers": headers,
        "body": base64.b64encode(body).decode("ascii"),
    }
    return base64.b64encode(json.dumps(payload).encode()).decode("ascii")


def _decode_response(output: str) -> HttpResponse:
    status, headers, encoded = json.loads(output)
    if type(status) is not int or not 200 <= status <= 599:
        raise ValueError("invalid HTTP status")
    if not isinstance(headers, list) or len(headers) > 100:
        raise ValueError("invalid HTTP headers")
    pairs: list[tuple[str, str]] = []
    for name, value in headers:
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("invalid HTTP header")
        if any(c in name + value for c in "\r\n\0"):
            raise ValueError("invalid HTTP header")
        pairs.append((name, value))
    body = base64.b64decode(encoded, validate=True)
    if len(body) > 25 * 1024 * 1024:
        raise ValueError("response body too large")
    return status, pairs, body


async def _stage_request(instance: SandboxInstance, frame: str, directory: str) -> None:
    created = await instance.exec_shell(f"mkdir -m 700 {shlex.quote(directory)}", timeout_s=5)
    if created.exit_code != 0:
        raise ValueError("cannot stage HTTP request")
    for offset in range(0, len(frame), _CHUNK_SIZE):
        command = (
            f"printf %s {shlex.quote(frame[offset : offset + _CHUNK_SIZE])} >> {directory}/request"
        )
        result = await instance.exec_shell(command, timeout_s=5)
        if result.exit_code != 0:
            raise ValueError("cannot stage HTTP request")


async def request_inside(
    session: SandboxSession,
    port: int,
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> HttpResponse | None:
    """Dispatch once on a pinned instance; never recreate, retry, or follow redirects.

    Large frames use a private, request-specific scratch directory outside the
    workspace. Preparation cannot contact the application. The final exec is the
    sole HTTP dispatch; any failure has an unknown outcome and returns unavailable.
    """
    if method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}:
        return None
    if not target.startswith("/") or target.startswith("//") or "#" in target:
        return None
    instance = session._instance
    generation = session.generation
    allowed = port in USER_PORTS or (
        session.shares_host_network and is_managed_host_preview_port(port)
    )
    if instance is None or not allowed or port == NOVNC_PORT or len(body) > 1024 * 1024:
        return None
    if sum(len(k) + len(v) for k, v in headers) > 65536:
        return None
    frame = _encode_request(port, method, target, headers, body)
    directory = f"/tmp/disco-preview-http-{uuid.uuid4().hex}"
    staged = len(frame) > _CHUNK_SIZE
    try:
        if staged:
            await _stage_request(instance, frame, directory)
        if session._instance is not instance or session.generation != generation:
            return None
        argument = f"@{directory}/request" if staged else frame
        result = await instance.exec_shell(
            f"python3 -I -c {shlex.quote(_CLIENT_SOURCE)} {shlex.quote(argument)}", timeout_s=20
        )
        if (
            result.exit_code != 0
            or result.timed_out
            or session._instance is not instance
            or session.generation != generation
        ):
            return None
        return _decode_response(result.stdout)
    except Exception:  # noqa: BLE001 — unknown outcomes must never trigger replay
        return None
    finally:
        if staged:
            try:
                await instance.exec_shell(f"rm -rf -- {directory}", timeout_s=5)
            except Exception:  # noqa: BLE001 — dead instances cannot retain a usable request
                pass
