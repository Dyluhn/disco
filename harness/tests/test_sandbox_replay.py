"""Record→replay round-trip for the stateful sandbox seam (no live backend).

Proves the property that makes BUILD-surface replay deterministic: a recorded
sequence of stateful sandbox ops replays byte-identically — including repeated
identical commands that returned DIFFERENT outputs (the nonce ordering) — and a
call that wasn't recorded surfaces as a CassetteMiss (the regression signal).
"""

from __future__ import annotations

import pytest
from perpleximanus.tools.sandbox.base import ExecResult, SandboxSpec

from harness.cassette import Cassette, CassetteMiss
from harness.sandbox import RecordingSandboxService, ReplaySandboxService


class _FakeInstance:
    """A minimal stateful sandbox: list_dir grows each call; reads see writes."""

    def __init__(self) -> None:
        self.id = "fake-1"
        self.owner_id = "local"
        self.conversation_id = "conv_test"
        self.spec = SandboxSpec()
        self._ls = 0
        self._files: dict[str, bytes] = {}

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        return ExecResult(exit_code=0, stdout=f"ran: {cmd}", stderr="")

    async def read_file(self, path: str) -> bytes:
        return self._files.get(path, b"")

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data

    async def list_dir(self, path: str) -> list[str]:
        self._ls += 1
        return [f"f{i}.py" for i in range(self._ls)]  # grows: stateful

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return "http://preview"

    async def destroy(self) -> None:
        return None


class _FakeService:
    name = "fake"

    def __init__(self) -> None:
        self._inst = _FakeInstance()

    async def create(self, spec, *, owner_id, conversation_id):
        return self._inst

    async def get(self, instance_id):
        return self._inst


async def _drive(inst) -> list:
    """A fixed sequence of stateful ops; returns the observed outputs."""
    out: list = []
    await inst.write_file("a.py", b"print(1)\n")
    out.append((await inst.exec_shell("python a.py", timeout_s=5)).stdout)  # [0]
    out.append(await inst.list_dir("."))  # [1] ls #1
    out.append(await inst.read_file("a.py"))  # [2] bytes
    out.append(await inst.list_dir("."))  # [3] ls #2 — different output (stateful)
    out.append((await inst.exec_shell("rm a.py", timeout_s=5)).stdout)  # [4]
    return out


async def test_record_replay_roundtrip_is_identical():
    cas = Cassette()
    rec = RecordingSandboxService(_FakeService(), cas)
    inst = await rec.create(SandboxSpec(), owner_id="local", conversation_id="conv_test")
    recorded = await _drive(inst)

    # Replay the SAME op sequence off the cassette — no fake backend involved.
    rep = ReplaySandboxService(cas)
    rinst = await rep.create(SandboxSpec(), owner_id="local", conversation_id="conv_test")
    replayed = await _drive(rinst)

    assert replayed == recorded
    # The two identical `ls .` calls (indices 1 and 3) returned DIFFERENT outputs,
    # and both replayed in order — the nonce keying, the whole point for a stateful
    # backend.
    assert recorded[1] != recorded[3]
    assert recorded[1] == ["f0.py"] and recorded[3] == ["f0.py", "f1.py"]
    assert recorded[2] == b"print(1)\n"  # bytes survive the base64 round-trip


async def test_divergent_call_is_a_cassette_miss():
    cas = Cassette()
    rec = RecordingSandboxService(_FakeService(), cas)
    inst = await rec.create(SandboxSpec(), owner_id="local", conversation_id="c")
    await inst.exec_shell("echo hi", timeout_s=5)

    rep = ReplaySandboxService(cas)
    rinst = await rep.create(SandboxSpec(), owner_id="local", conversation_id="c")
    with pytest.raises(CassetteMiss):
        await rinst.exec_shell("echo DIFFERENT", timeout_s=5)


async def test_replay_instance_satisfies_the_protocol_shape():
    cas = Cassette()
    rep = ReplaySandboxService(cas)
    inst = await rep.create(SandboxSpec(), owner_id="local", conversation_id="c")
    # write is a no-op, preview is absent under replay — honest, not a false URL.
    assert await inst.write_file("x", b"y") is None
    assert inst.expose_port(8000) is None
    assert inst.display_url() is None
    assert await rep.get(inst.id) is inst
