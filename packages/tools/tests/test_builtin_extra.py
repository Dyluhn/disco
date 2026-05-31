"""Coverage for the file_edit and extract tools + a couple of small paths."""

from __future__ import annotations

from conftest import FakeSandboxInstance, call
from perpleximanus.tools import (
    CapabilityBroker,
    DefaultToolExecutor,
    ProcessSandboxService,
    SandboxSpec,
    ToolScope,
    build_default_registry,
    research_scope,
    validate_args,
)
from perpleximanus.tools.builtin import FileReadTool


async def test_file_edit_replaces_and_reports_missing():
    sandbox = FakeSandboxInstance()
    await sandbox.write_file("doc.txt", b"the quick brown fox")
    ex = DefaultToolExecutor(
        build_default_registry(),
        ToolScope(allowed_tools=frozenset({"file_edit", "file_read"})),
        sandbox=sandbox,
    )
    ok = await ex.execute(call("file_edit", path="doc.txt", old="quick", new="slow"))
    assert ok.success
    read = await ex.execute(call("file_read", path="doc.txt"))
    assert read.content == "the slow brown fox"

    missing = await ex.execute(call("file_edit", path="doc.txt", old="zebra", new="x"))
    assert missing.success is False and missing.error == "old_text_not_found"


async def test_extract_tool_resolves_via_capability():
    async def extract_handler(*, url):
        return f"clean content of {url}"

    broker = CapabilityBroker()
    broker.register("extract", extract_handler)
    ex = DefaultToolExecutor(build_default_registry(), research_scope(), broker=broker)
    res = await ex.execute(call("extract", url="https://example.test/a"))
    assert res.success and "clean content of https://example.test/a" in res.content


def test_validate_args_helper_raises_on_bad_input():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        validate_args(FileReadTool().definition, {})  # missing required `path`
    ok = validate_args(FileReadTool().definition, {"path": "x"})
    assert ok.path == "x"


async def test_process_instance_has_no_display_and_kill_without_sandbox():
    inst = await ProcessSandboxService().create(
        SandboxSpec(), owner_id="local", conversation_id="c"
    )
    assert inst.display_url() is None
    await inst.destroy()
    # kill() on a sandbox-less executor is a no-op that still revokes capabilities.
    ex = DefaultToolExecutor(build_default_registry(), research_scope())
    await ex.kill()
    res = await ex.execute(call("search", query="x"))
    assert res.success is False  # killed
