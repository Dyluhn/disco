"""REL-1d host verifier canary bookkeeping."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from disco.agent_server.runtime import ConversationRuntime, host_verify_canary_enabled
from disco.core import SqliteEventStore, VerifierVerdictEvent
from disco.core.context.ledger import ArtifactRecord
from disco.core.context.store import ArtifactMemoryStore
from disco.core.contract import ContractKind, Phase
from disco.tools import ProcessSandboxService


class MemFS:
    """In-memory WorkspaceFS: dict-backed; read of a missing path raises."""

    conversation_id = "conv_verify_canary"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _runtime() -> ConversationRuntime:
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        router=MagicMock(),
        sandbox_service=ProcessSandboxService(),
    )


def _inject_executor(rt: ConversationRuntime, cid: str, fs: MemFS) -> None:
    fake_executor = MagicMock()
    fake_executor.sandbox = fs
    rt._run_resources.set_executor(cid, fake_executor)


def _verdict_event(path: str = "index.html", verdict: str = "pass") -> VerifierVerdictEvent:
    return VerifierVerdictEvent(
        artifact_path=path,
        artifact_kind="app",
        verified=verdict == "pass",
        verdict=verdict,
    )


def test_host_verify_canary_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCO_HOST_VERIFY_CANARY", raising=False)
    monkeypatch.delenv("PMX_HOST_VERIFY_CANARY", raising=False)
    assert host_verify_canary_enabled() is False

    monkeypatch.setenv("DISCO_HOST_VERIFY_CANARY", "on")
    assert host_verify_canary_enabled() is True

    monkeypatch.setenv("DISCO_HOST_VERIFY_CANARY", "0")
    assert host_verify_canary_enabled() is False


@pytest.mark.asyncio
async def test_host_verify_canary_flag_off_installs_no_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook installs when canary OR authoritative is enabled. Authoritative
    became the code default with the REL-1e flip, so the no-hook posture now
    requires BOTH explicitly off — and the default env installs the hook."""
    monkeypatch.delenv("DISCO_HOST_VERIFY_CANARY", raising=False)
    monkeypatch.delenv("PMX_HOST_VERIFY_CANARY", raising=False)
    monkeypatch.delenv("DISCO_HOST_VERIFY_AUTHORITATIVE", raising=False)
    monkeypatch.delenv("PMX_HOST_VERIFY_AUTHORITATIVE", raising=False)
    rt = _runtime()
    cid = "conv_canary_off"
    rt.settings._set_surface(cid, "build")
    fs = MemFS()
    _inject_executor(rt, cid, fs)

    # REL-1e default: authoritative ON → verdict bookkeeping hook installed.
    assert (
        rt.contract._host_verify_canary_hook_for(cid, rt.contract.note_build_verify_result)
        is not None
    )

    monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", "off")
    assert (
        rt.contract._host_verify_canary_hook_for(cid, rt.contract.note_build_verify_result)
        is None
    )
    assert cid not in rt.contract._build_trackers
    assert await ArtifactMemoryStore(fs).read_artifacts() == ()


@pytest.mark.asyncio
async def test_host_verify_canary_on_default_build_updates_phase_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_HOST_VERIFY_CANARY", "1")
    rt = _runtime()
    cid = "conv_canary_default_build"
    rt.settings._set_surface(cid, "build")
    assert rt.contract._finalizer_alias_for(cid) is None

    fs = MemFS()
    _inject_executor(rt, cid, fs)
    await ArtifactMemoryStore(fs).upsert_artifact(
        ArtifactRecord(path="index.html", kind="app", shown=True, export={"zip": "t"})
    )

    calls: list[tuple[str, bool]] = []
    original_note = rt.contract.note_build_verify_result

    def spy_note(conversation_id: str, *, passed: bool) -> None:
        calls.append((conversation_id, passed))
        original_note(conversation_id, passed=passed)

    rt.contract.note_build_verify_result = spy_note  # type: ignore[method-assign]
    hook = rt.contract._host_verify_canary_hook_for(cid, rt.contract.note_build_verify_result)
    assert hook is not None

    await hook(_verdict_event())

    assert calls == [(cid, True)]
    contract, tracker = rt.contract._build_trackers[cid]
    assert contract.kind is ContractKind.CUSTOM
    assert tracker.current() is Phase.EXPORT

    records = await ArtifactMemoryStore(fs).read_artifacts()
    assert len(records) == 1
    record = records[0]
    assert record.path == "index.html"
    assert record.kind == "app"
    assert record.shown is True
    assert record.export == {"zip": "t"}
    assert record.verified is True
    assert record.verify_verdict == "pass"
