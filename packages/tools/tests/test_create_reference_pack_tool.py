"""`create_reference_pack`: copies exact workspace bytes through the host writer,
refuses bad paths and duplicates, fails closed without a writer, and sits in the
free-form build scope."""

from __future__ import annotations

from typing import Any

from disco.core.llm import ModelExecutionPolicy
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
from disco.tools.reference_packs import ReferencePackWriteError
from tool_fakes import FakeSandboxInstance, call


class _Writer:
    def __init__(self, fail: ReferencePackWriteError | None = None) -> None:
        self.calls: list[tuple[str, str, str, list[tuple[str, bytes]]]] = []
        self._fail = fail

    async def __call__(
        self, owner_id: str, name: str, description: str, files: list[tuple[str, bytes]]
    ) -> dict[str, Any]:
        self.calls.append((owner_id, name, description, files))
        if self._fail is not None:
            raise self._fail
        return {"pack_id": "rp_" + "0" * 32, "name": name, "file_count": len(files)}


def _executor(writer: _Writer | None) -> tuple[DefaultToolExecutor, FakeSandboxInstance]:
    sandbox = FakeSandboxInstance()
    sandbox._fs["uploads/brand.pdf"] = b"%PDF-1.4 brand"
    sandbox._fs["notes/spec.md"] = b"# Spec"
    executor = DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=ModelExecutionPolicy.standard()),
        sandbox=sandbox,
        owner_id="alice",
        reference_pack_writer=writer,
    )
    return executor, sandbox


async def test_copies_exact_bytes_in_order_through_the_writer():
    writer = _Writer()
    executor, _ = _executor(writer)
    outcome = await executor.execute(
        call(
            "create_reference_pack",
            name="Brand kit",
            description="logos",
            files=["uploads/brand.pdf", "./notes/spec.md"],
        )
    )
    assert outcome.success, outcome.error
    assert writer.calls == [
        ("alice", "Brand kit", "logos", [("brand.pdf", b"%PDF-1.4 brand"), ("spec.md", b"# Spec")])
    ]
    assert outcome.structured == {
        "pack": {"pack_id": "rp_" + "0" * 32, "name": "Brand kit", "file_count": 2}
    }
    assert "Settings" in outcome.content


async def test_rejects_traversal_internal_paths_missing_files_and_duplicates():
    writer = _Writer()
    executor, _ = _executor(writer)
    for bad in (["../etc/passwd"], [".pmx/screenshots/x.png"], ["references/pack/PACK.md"]):
        outcome = await executor.execute(call("create_reference_pack", name="p", files=bad))
        assert not outcome.success and outcome.error and outcome.error.startswith("invalid_path")
    outcome = await executor.execute(call("create_reference_pack", name="p", files=["nope.txt"]))
    assert not outcome.success and outcome.error and outcome.error.startswith("file_not_found")
    outcome = await executor.execute(
        call("create_reference_pack", name="p", files=["notes/spec.md", "notes/spec.md"])
    )
    assert not outcome.success and outcome.error and outcome.error.startswith("duplicate_path")
    assert writer.calls == []


async def test_writer_errors_and_missing_writer_fail_closed():
    executor, _ = _executor(
        _Writer(fail=ReferencePackWriteError("invalid_reference_pack", "too big"))
    )
    outcome = await executor.execute(
        call("create_reference_pack", name="p", files=["notes/spec.md"])
    )
    assert not outcome.success and outcome.error == "invalid_reference_pack: too big"
    executor, _ = _executor(None)
    outcome = await executor.execute(
        call("create_reference_pack", name="p", files=["notes/spec.md"])
    )
    assert not outcome.success and outcome.error and "unavailable" in outcome.error


def test_in_the_free_form_build_scope_and_registry():
    scope = agent_scope(model_policy=ModelExecutionPolicy())
    tool = build_default_registry().get("create_reference_pack", scope=scope)
    assert tool is not None
    assert tool.definition.runs_in == "sandbox" and not tool.definition.read_only
