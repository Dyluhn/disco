from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from disco.agent_server.build_loop_factory import BuildExecutorFactory
from disco.agent_server.reference_pack_binding import (
    ReferencePackBindingStore,
    ReferencePackSelection,
    amaterialize_reference_binding,
)
from disco.agent_server.reference_pack_runtime import ReferencePackRuntime
from disco.agent_server.reference_pack_store import ReferencePackStore
from disco.core.events import (
    ActionEvent,
    Event,
    MessageEvent,
    ObservationEvent,
    ReferencePackBindingEvent,
    ToolCall,
    ToolResult,
)
from disco.core.loop.resource_receipts import snapshot_receipt
from disco.tools.anatomy import ToolContext
from disco.tools.builtin import build_default_registry
from disco.tools.builtin.reference_packs import CreateReferencePackArgs


class Reader:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def read_file(self, path: str) -> bytes:
        return self.files[path]

    async def is_file(self, path: str) -> bool:
        return path in self.files

    async def is_symlink(self, path: str) -> bool:
        del path
        return False


class Target:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        return self.files[path]


class GenerationTarget(Target):
    generation = 1
    conversation_id = "conv"


class EventSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def get_events(self, conversation_id: str) -> list[Event]:
        del conversation_id
        return list(self.events)

    async def append(self, conversation_id: str, event: Event) -> Event:
        del conversation_id
        for existing in self.events:
            if existing.id == event.id:
                return existing
        self.events.append(event)
        return event

    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]:
        return [await self.append(conversation_id, event) for event in events]


class Sandbox:
    owner_id = "sandbox-owner-is-not-the-auth-authority"
    conversation_id = "conv"

    async def read_file(self, path: str) -> bytes:
        return {"brief.md": b"brief"}[path]

    async def file_exists(self, path: str) -> bool:
        return path == "brief.md"


async def _pack(tmp_path: Path) -> tuple[ReferencePackStore, ReferencePackBindingStore, str]:
    packs = ReferencePackStore(tmp_path / "packs")
    pack = await packs.acreate(
        "owner", "Brief", "trusted", ["brief.md"], reader=Reader({"brief.md": b"brief"})
    )
    bindings = ReferencePackBindingStore(tmp_path / "bindings")
    bindings.bind(
        "owner",
        "conv",
        [ReferencePackSelection(pack.id, pack.current_version_id, pack.current.content_sha256)],
        packs=packs,
    )
    return packs, bindings, pack.id


@pytest.mark.asyncio
async def test_create_tool_is_bound_to_one_sandbox_identity(tmp_path: Path) -> None:
    packs = ReferencePackStore(tmp_path / "packs")
    runtime = ReferencePackRuntime(store=packs, bindings=ReferencePackBindingStore(tmp_path / "b"))
    tool = runtime.create_agent_tool(Sandbox())
    ctx = ToolContext(
        sandbox=None,
        workspace_path=".",
        timeout_s=10,
        capabilities=frozenset(),
        owner_id="owner",
        conversation_id="conv",
    )
    outcome = await tool.run(
        CreateReferencePackArgs(name="Brief", files=[{"path": "brief.md"}]), ctx
    )
    assert outcome.success
    assert outcome.structured is not None
    assert len(packs.list("owner")) == 1

    foreign = ctx.model_copy(update={"conversation_id": "other"})
    rejected = await tool.run(
        CreateReferencePackArgs(name="Brief", files=[{"path": "brief.md"}]), foreign
    )
    assert not rejected.success
    assert rejected.error == "reference_pack_host_seam_unavailable"


@pytest.mark.asyncio
async def test_materialize_records_one_idempotent_binding_event(tmp_path: Path) -> None:
    _packs, bindings, _pack_id = await _pack(tmp_path)
    sink = EventSink()
    runtime = ReferencePackRuntime(bindings=bindings, event_store=sink)
    first_target, second_target = Target(), Target()
    first = await runtime.materialize_before_model("owner", "conv", first_target)
    second = await runtime.rematerialize("owner", "conv", second_target)
    assert first is not None and second is not None
    assert first.binding_id == second.binding_id
    assert first.paths == second.paths
    assert first.event is not None and second.event is not None
    assert first.event.id == second.event.id
    assert (
        len([event for event in sink.events if isinstance(event, ReferencePackBindingEvent)]) == 1
    )
    prompts = [event for event in sink.events if isinstance(event, MessageEvent)]
    assert len(prompts) == 1
    assert "Before calling `submit_plan`" in prompts[0].message.content
    assert "references/Brief/PACK.md" in prompts[0].message.content
    assert first_target.files == second_target.files
    assert first_target.files["references/Brief/PACK.md"].startswith(b"# Reference Pack")


@pytest.mark.asyncio
async def test_materialize_without_binding_is_a_noop(tmp_path: Path) -> None:
    runtime = ReferencePackRuntime(
        store=ReferencePackStore(tmp_path / "packs"),
        bindings=ReferencePackBindingStore(tmp_path / "bindings"),
        event_store=EventSink(),
    )
    assert await runtime.materialize_before_model("owner", "empty", Target()) is None


@pytest.mark.asyncio
async def test_read_funnel_facts_are_host_derived(tmp_path: Path) -> None:
    _packs, bindings, _pack_id = await _pack(tmp_path)
    runtime = ReferencePackRuntime(bindings=bindings)
    binding = bindings.get("owner", "conv")
    assert binding is not None
    facts = runtime.read_funnel_facts(binding, [])
    assert facts.required_paths == ("references/Brief/PACK.md",)
    assert facts.missing_paths == facts.required_paths
    assert not facts.complete
    complete = runtime.read_funnel_facts(binding, facts.required_paths)
    assert complete.complete


@pytest.mark.asyncio
async def test_pinned_inspection_callback_reads_current_binding_only(tmp_path: Path) -> None:
    _packs, bindings, pack_id = await _pack(tmp_path)
    runtime = ReferencePackRuntime(bindings=bindings)
    callback = runtime.reference_inspect_callback()
    # Materialize into the snapshot first; inspection never reads the mutable
    # library head or a caller-provided workspace path.
    binding = bindings.get("owner", "conv")
    assert binding is not None
    await amaterialize_reference_binding(binding, bindings, Target())
    result = await callback(
        owner_id="owner",
        conversation_id="conv",
        pack_id=pack_id,
        file_name="brief.md",
        question="",
    )
    assert result["status"] == "text"
    assert result["content"] == "brief"


@pytest.mark.asyncio
async def test_executor_seam_registers_only_real_capabilities_and_rematerializes(
    tmp_path: Path,
) -> None:
    packs, bindings, _pack_id = await _pack(tmp_path)
    sink = EventSink()
    runtime = ReferencePackRuntime(store=packs, bindings=bindings, event_store=sink)

    class OwnerStore:
        @staticmethod
        def conversation_owner_id_sync(_conversation_id: str) -> str:
            return "owner"

    factory = object.__new__(BuildExecutorFactory)
    factory._reference_packs = runtime
    factory._event_store = OwnerStore()
    target = GenerationTarget()

    registry = factory._registry_with_reference_tools(
        build_default_registry(),
        "conv",
        target,
        include_create=True,
    )
    assert {"create_reference_pack", "reference_inspect"} <= registry.names()

    prepare = factory._reference_prepare("conv", target)
    assert prepare is not None
    await prepare([])
    assert "references/Brief/PACK.md" in target.files
    first_events = list(sink.events)

    guard = factory._reference_plan_guard("conv")
    assert guard is not None
    assert "PACK.md" in (guard(first_events) or "")
    read = ActionEvent(
        thought="Ground the selected pack",
        tool_call=ToolCall(
            call_id="read-pack",
            tool_name="file_read",
            arguments={"path": "references/Brief/PACK.md"},
        ),
    )
    observed = ObservationEvent(
        action_id=read.id,
        tool_result=ToolResult(
            call_id="read-pack",
            tool_name="file_read",
            success=True,
            content="# Reference Pack",
        ),
    )
    # A successful-looking observation without the host receipt is not
    # grounding evidence, even when the call has no offset/limit.
    assert guard([*first_events, read, observed]) is not None

    pack_md = target.files["references/Brief/PACK.md"].decode("utf-8")
    partial = observed.model_copy(
        update={
            "tool_result": observed.tool_result.model_copy(
                update={
                    "effect_receipts": (
                        snapshot_receipt(
                            path="references/Brief/PACK.md",
                            sha256=hashlib.sha256(pack_md.encode()).hexdigest(),
                            total_lines=pack_md.count("\n"),
                            raw_size_bytes=len(pack_md.encode()),
                            rendered_content=pack_md[:8],
                        ).model_copy(update={"complete": False}),
                    )
                }
            )
        }
    )
    assert guard([*first_events, read, partial]) is not None

    complete = observed.model_copy(
        update={
            "tool_result": observed.tool_result.model_copy(
                update={
                    "content": pack_md,
                    "effect_receipts": (
                        snapshot_receipt(
                            path="references/Brief/PACK.md",
                            sha256=hashlib.sha256(pack_md.encode()).hexdigest(),
                            total_lines=pack_md.count("\n"),
                            raw_size_bytes=len(pack_md.encode()),
                            rendered_content=pack_md,
                        ),
                    ),
                }
            )
        }
    )
    assert guard([*first_events, read, complete]) is None

    await prepare([])
    assert sink.events == first_events

    target.files.clear()
    target.generation += 1
    await prepare([])
    assert "references/Brief/PACK.md" in target.files
    assert sink.events == first_events
