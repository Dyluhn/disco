"""Record/replay for the sandbox seam — the STATEFUL backend.

Unlike search/extraction (stateless: same query → same result, so idempotent
first-wins keying is fine — see providers.py), the sandbox is stateful: the same
`ls` returns different output before vs after a write. So each call is keyed by
`(method, args, occurrence-nonce)` — the Nth identical call replays the Nth
recorded result, IN ORDER. As long as the replay engine is deterministic (LLM +
reads served from the same cassette), the per-key call order matches and the
nonces align; a misalignment surfaces as a `CassetteMiss`, which is exactly the
regression signal the harness exists to produce.

Only data-returning calls are recorded (`exec_shell` / `read_file` / `list_dir`)
— those are what the agent acts on. `write_file`/`destroy` are no-ops on replay
(state is reconstructed entirely through the replayed reads), and
`expose_port`/`display_url` return None (no live preview under replay).

Both pairs conform to the `SandboxService` / `SandboxInstance` protocols
(`tools/sandbox/base.py`) by duck-typing, so they drop into the existing
`ConversationRuntime(sandbox_service=...)` override seam.
"""

from __future__ import annotations

import base64
from collections import defaultdict

from perpleximanus.tools.sandbox.base import ExecResult

from .cassette import Cassette


class _Nonce:
    """Per-instance occurrence counter so repeated identical calls key distinctly."""

    def __init__(self) -> None:
        self._n: dict[tuple[str, str], int] = defaultdict(int)

    def key(self, method: str, arg: str) -> dict:
        n = self._n[(method, arg)]
        self._n[(method, arg)] += 1
        return {"m": method, "a": arg, "n": n}


# ---- recording --------------------------------------------------------------


class RecordingSandboxInstance:
    """Wraps a REAL instance; records each data-returning call verbatim."""

    def __init__(self, inner, cassette: Cassette):
        self._inner = inner
        self._cas = cassette
        self._nonce = _Nonce()
        self.id = inner.id
        self.owner_id = inner.owner_id
        self.conversation_id = inner.conversation_id
        self.spec = inner.spec

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        res = await self._inner.exec_shell(cmd, timeout_s=timeout_s)
        self._cas.record("sandbox.exec", self._nonce.key("exec", cmd), res.model_dump())
        return res

    async def read_file(self, path: str) -> bytes:
        data = await self._inner.read_file(path)
        self._cas.record(
            "sandbox.read", self._nonce.key("read", path), base64.b64encode(data).decode()
        )
        return data

    async def list_dir(self, path: str) -> list[str]:
        names = await self._inner.list_dir(path)
        self._cas.record("sandbox.list", self._nonce.key("list", path), names)
        return names

    async def write_file(self, path: str, data: bytes) -> None:
        await self._inner.write_file(path, data)

    def display_url(self) -> str | None:
        return self._inner.display_url()

    def expose_port(self, port: int) -> str | None:
        return self._inner.expose_port(port)

    async def destroy(self) -> None:
        await self._inner.destroy()


class RecordingSandboxService:
    name = "recording-sandbox"

    def __init__(self, inner, cassette: Cassette):
        self._inner = inner
        self._cas = cassette
        self._instances: dict[str, RecordingSandboxInstance] = {}

    async def create(self, spec, *, owner_id: str, conversation_id: str):
        inst = await self._inner.create(spec, owner_id=owner_id, conversation_id=conversation_id)
        wrapped = RecordingSandboxInstance(inst, self._cas)
        self._instances[wrapped.id] = wrapped
        return wrapped

    async def get(self, instance_id: str):
        return self._instances.get(instance_id)


# ---- replay -----------------------------------------------------------------


class ReplaySandboxInstance:
    """Serves recorded sandbox results — no real backend, fully deterministic."""

    def __init__(self, cassette: Cassette, *, id, owner_id, conversation_id, spec):
        self._cas = cassette
        self._nonce = _Nonce()
        self.id = id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        row = self._cas.lookup("sandbox.exec", self._nonce.key("exec", cmd))
        return ExecResult.model_validate(row)

    async def read_file(self, path: str) -> bytes:
        row = self._cas.lookup("sandbox.read", self._nonce.key("read", path))
        return base64.b64decode(row)

    async def list_dir(self, path: str) -> list[str]:
        return self._cas.lookup("sandbox.list", self._nonce.key("list", path))

    async def write_file(self, path: str, data: bytes) -> None:
        return None  # state is reconstructed through replayed reads

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return None  # no live preview under replay

    async def destroy(self) -> None:
        return None


class ReplaySandboxService:
    name = "replay-sandbox"

    def __init__(self, cassette: Cassette):
        self._cas = cassette
        self._instances: dict[str, ReplaySandboxInstance] = {}

    async def create(self, spec, *, owner_id: str, conversation_id: str):
        inst = ReplaySandboxInstance(
            self._cas,
            id=f"replay-{conversation_id}",
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
        )
        self._instances[inst.id] = inst
        return inst

    async def get(self, instance_id: str):
        return self._instances.get(instance_id)
