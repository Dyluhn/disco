from __future__ import annotations

from pathlib import Path

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.reference_packs import (
    CreateReferencePackArgs,
    CreateReferencePackTool,
    ReferenceInspectArgs,
    ReferenceInspectTool,
)


class Reader:
    def is_file(self, path: str) -> bool:
        return path == "brief.md"

    def is_symlink(self, path: str) -> bool:
        return False

    async def read_file(self, path: str) -> bytes:
        return b"brief"


class Store:
    async def acreate(self, owner_id: str, name: str, description: str, files: list, *, reader):
        del owner_id, description, files, reader

        class Version:
            current = type("Current", (), {"files": (object(),)})()

        return type(
            "Pack",
            (),
            {"id": "pack-1", "name": name, "current_version_id": "v1", "current": Version.current},
        )()


@pytest.mark.asyncio
async def test_create_reference_pack_uses_constructor_host_seams() -> None:
    tool = CreateReferencePackTool(store=Store(), reader_factory=lambda _ctx: Reader())
    ctx = ToolContext(
        sandbox=None,
        workspace_path=str(Path("/workspace")),
        timeout_s=10,
        capabilities=frozenset(),
        owner_id="owner",
        conversation_id="conv_1",
    )
    outcome = await tool.run(
        CreateReferencePackArgs(name="Brief", files=[{"path": "brief.md"}]), ctx
    )
    assert outcome.success is True
    assert outcome.structured == {
        "pack_id": "pack-1",
        "name": "Brief",
        "file_count": 1,
        "current_version_id": "v1",
        "use_in_build": True,
    }


@pytest.mark.asyncio
async def test_create_reference_pack_fails_closed_without_injected_seams() -> None:
    tool = CreateReferencePackTool()
    ctx = ToolContext(
        sandbox=None,
        workspace_path="/workspace",
        timeout_s=10,
        capabilities=frozenset(),
        owner_id="owner",
        conversation_id="conv_1",
    )
    outcome = await tool.run(
        CreateReferencePackArgs(name="Brief", files=[{"path": "brief.md"}]), ctx
    )
    assert outcome.success is False
    assert outcome.error == "reference_pack_host_seam_unavailable"


@pytest.mark.asyncio
async def test_reference_inspect_tool_uses_host_pinned_callback() -> None:
    calls: list[dict[str, str]] = []

    async def inspect_file(**kwargs: str) -> dict[str, str]:
        calls.append(kwargs)
        return {
            "status": "text",
            "pack_id": kwargs["pack_id"],
            "file_name": kwargs["file_name"],
            "content": "trusted pinned text",
            "media_type": "text/plain",
        }

    tool = ReferenceInspectTool(inspector=inspect_file)
    ctx = ToolContext(
        sandbox=None,
        workspace_path="/workspace",
        timeout_s=10,
        capabilities=frozenset(),
        owner_id="owner",
        conversation_id="conv_1",
    )
    outcome = await tool.run(
        ReferenceInspectArgs(pack_id="pack-1", file_name="notes.txt", question="summarize"),
        ctx,
    )
    assert outcome.success is True
    assert outcome.content == "trusted pinned text"
    assert calls == [
        {
            "owner_id": "owner",
            "conversation_id": "conv_1",
            "pack_id": "pack-1",
            "file_name": "notes.txt",
            "question": "summarize",
        }
    ]


@pytest.mark.asyncio
async def test_reference_inspect_tool_fails_closed_without_host_callback() -> None:
    tool = ReferenceInspectTool()
    ctx = ToolContext(
        sandbox=None,
        workspace_path="/workspace",
        timeout_s=10,
        capabilities=frozenset(),
        owner_id="owner",
        conversation_id="conv_1",
    )
    outcome = await tool.run(
        ReferenceInspectArgs(pack_id="pack-1", file_name="notes.txt"),
        ctx,
    )
    assert outcome.success is False
    assert outcome.error == "reference_inspect_host_seam_unavailable"


def test_plain_registry_does_not_advertise_host_reference_capabilities() -> None:
    names = build_default_registry().names()
    assert "create_reference_pack" not in names
    assert "reference_inspect" not in names
