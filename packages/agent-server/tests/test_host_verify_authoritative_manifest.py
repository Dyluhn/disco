"""REL-1e authoritative host verifier manifest stamping."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore, VerifierVerdictEvent
from disco.core.context.ledger import ArtifactRecord
from disco.core.context.store import ArtifactMemoryStore
from disco.tools import ProcessSandboxService


class MemFS:
    conversation_id = "conv_authoritative_manifest"

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


@pytest.mark.asyncio
async def test_authoritative_flag_stamps_unverifiable_manifest_without_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCO_HOST_VERIFY_CANARY", raising=False)
    monkeypatch.delenv("PMX_HOST_VERIFY_CANARY", raising=False)
    monkeypatch.setenv("DISCO_HOST_VERIFY_AUTHORITATIVE", "1")
    rt = _runtime()
    cid = "conv_authoritative_unverifiable"
    rt.set_surface(cid, "build")
    fs = MemFS()
    _inject_executor(rt, cid, fs)
    await ArtifactMemoryStore(fs).upsert_artifact(
        ArtifactRecord(path="report.txt", kind="files", shown=True)
    )

    hook = rt._contract._host_verify_canary_hook_for(cid, rt._contract.note_build_verify_result)
    assert hook is not None

    await hook(
        VerifierVerdictEvent(
            artifact_path="report.txt",
            artifact_kind="files",
            verified=False,
            verdict="unverifiable",
        )
    )

    records = await ArtifactMemoryStore(fs).read_artifacts()
    assert len(records) == 1
    assert records[0].path == "report.txt"
    assert records[0].verified is False
    assert records[0].verify_verdict == "unverifiable"
