"""Fix 2 (B-E) — SandboxSession.fetch_inside: the in-sandbox liveness proxy.

On sealed/filtered backends the box publishes NO host port, so the only host
channel to a live dev server is this exec-curl proxy. These unit tests drive a
fake SandboxInstance whose exec_shell FRAMES the response exactly as the real
`curl -w '%{http_code}\\t%{content_type}' ... | base64 -w0` pipeline does, so the
parse half (status / content-type / base64 body round-trip) is exercised
byte-for-byte, plus the USER_PORTS containment gate and the not-listening path.
"""

from __future__ import annotations

import base64

import pytest
from disco.tools.sandbox._container import PREVIEW_PORT
from disco.tools.sandbox.base import ExecResult
from disco.tools.sandbox.session import SandboxSession


class _FramingInstance:
    """SandboxInstance whose exec_shell emulates the real curl|base64 pipeline.

    Configured with a (status, body, ctype) "server" or `down=True` (nothing
    listening → curl http_code 000, empty body). Records the last command so a
    test can assert the command shape (regression guard on the pipeline)."""

    id = "fake-fetch-inside"
    owner_id = "local"
    conversation_id = "conv-fi"
    spec = None

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        ctype: str = "text/html",
        down: bool = False,
        missing_curl: bool = False,
    ) -> None:
        self._status = status
        self._body = body
        self._ctype = ctype
        self._down = down
        self._missing_curl = missing_curl
        self.last_cmd: str | None = None

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.last_cmd = cmd
        if "pwd" == cmd:  # auto-preview / detect_serve_dir probes
            return ExecResult(exit_code=0, stdout="/workspace", stderr="")
        if self._missing_curl:
            # curl absent: -w prints nothing, printf adds the newline, base64 of the
            # stale/empty temp file yields nothing → a single empty header line.
            return ExecResult(exit_code=0, stdout="\n", stderr="")
        if self._down:
            return ExecResult(exit_code=0, stdout="000\t\n", stderr="")
        framed = f"{self._status}\t{self._ctype}\n" + base64.b64encode(self._body).decode()
        return ExecResult(exit_code=0, stdout=framed, stderr="")

    async def read_file(self, path: str) -> bytes:
        return b""

    async def write_file(self, path: str, data: bytes) -> None:
        pass

    async def list_dir(self, path: str) -> list[str]:
        return []

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return None

    async def destroy(self) -> None:
        pass


class _Svc:
    name = "fake"

    def __init__(self, inst: _FramingInstance) -> None:
        self._inst = inst

    async def create(self, spec, *, owner_id, conversation_id):
        return self._inst

    async def get(self, instance_id):
        return None


def _session(inst: _FramingInstance) -> SandboxSession:
    return SandboxSession(_Svc(inst), conversation_id="conv-fetchinside")


@pytest.mark.asyncio
async def test_fetch_inside_round_trips_binary_png_byte_identical():
    """A binary PNG must survive the base64 frame byte-for-byte (the binary-safe
    requirement) and the status + content-type must be parsed off the header."""
    # 1x1 PNG (real signature + a couple of non-text/high bytes).
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDAT\x78\x9c\x63\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    inst = _FramingInstance(status=200, body=png, ctype="image/png")
    session = _session(inst)

    got = await session.fetch_inside(PREVIEW_PORT, "logo.png")
    assert got is not None
    status, body, ctype = got
    assert status == 200
    assert ctype == "image/png"
    assert body == png  # byte-identical through base64
    # regression guard: the real exec-curl|base64 pipeline shape is intact.
    assert inst.last_cmd is not None
    assert "curl" in inst.last_cmd
    assert "base64 -w0" in inst.last_cmd
    assert str(PREVIEW_PORT) in inst.last_cmd


@pytest.mark.asyncio
async def test_fetch_inside_non_user_port_returns_none_without_exec():
    """Containment: a non-curated port is refused BEFORE any exec (never a surface)."""
    inst = _FramingInstance(status=200, body=b"x", ctype="text/html")
    session = _session(inst)
    got = await session.fetch_inside(9999, "")
    assert got is None
    # exec_shell was never reached for the (rejected) fetch — only the auto-preview
    # pwd probe may have run, never a curl to 9999.
    assert inst.last_cmd is None or "9999" not in inst.last_cmd


@pytest.mark.asyncio
async def test_fetch_inside_nothing_listening_returns_none():
    """Connection refused inside the box (curl http_code 000) → None (honest 503)."""
    inst = _FramingInstance(down=True)
    session = _session(inst)
    got = await session.fetch_inside(PREVIEW_PORT, "")
    assert got is None


@pytest.mark.asyncio
async def test_fetch_inside_missing_curl_returns_none():
    """No curl in the image (empty header line) → None, not a crash."""
    inst = _FramingInstance(missing_curl=True)
    session = _session(inst)
    got = await session.fetch_inside(PREVIEW_PORT, "")
    assert got is None


@pytest.mark.asyncio
async def test_fetch_inside_empty_body_ok():
    """A 204-style empty body is a valid reachable response (status surfaced)."""
    inst = _FramingInstance(status=204, body=b"", ctype="")
    session = _session(inst)
    got = await session.fetch_inside(PREVIEW_PORT, "")
    assert got is not None
    status, body, ctype = got
    assert status == 204
    assert body == b""
    assert ctype == "application/octet-stream"  # defaulted when curl reports none
