"""CONTRACT-ENFORCE integration: the executor (the universal tool chokepoint) hard-
denies an out-of-phase tool when a BuildContract governs the run.

Acceptance: for an appkit.leadgen contract in the EDIT phase, a raw file_write is
rejected at the executor (kind 'denied', message names the out-of-contract scope) and
NEVER runs; the semantic app_update_content and the utility `think` are NOT scope-denied;
and once the phase is REPAIR, file_write is permitted again. With no guard, behavior is
unchanged.
"""

from __future__ import annotations

import pytest

from disco.core.contract import (
    BuildContractRegistry,
    BuildPhaseTracker,
    ContractKind,
    ContractScopeGuard,
    Phase,
)
from disco.core.events import ToolResult
from disco.core.llm import ModelExecutionPolicy
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry

from tool_fakes import FakeSandboxInstance, call

_STANDARD = ModelExecutionPolicy.standard()


def _scope_denied(res: ToolResult) -> bool:
    """True iff the executor rejected the call via the contract-scope guard."""
    return (not res.success) and "out of contract scope" in (res.error or res.content or "")


def _appkit():
    c = BuildContractRegistry.default().get(ContractKind.APPKIT_LEADGEN)
    assert c is not None
    return c


def _executor(phase_box, sbx):
    guard = ContractScopeGuard.for_contract(_appkit(), lambda: phase_box[0])
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=_STANDARD),
        sandbox=sbx,
        scope_guard=guard,
    )


@pytest.mark.asyncio
async def test_file_write_denied_in_edit_phase() -> None:
    sbx = FakeSandboxInstance()
    ex = _executor([Phase.EDIT], sbx)
    res = await ex.execute(call("file_write", path="index.html", content="<h1>hi</h1>"))
    assert _scope_denied(res)
    assert res.structured is not None and res.structured["kind"] == "denied"
    assert "index.html" not in sbx._fs  # the write NEVER happened


@pytest.mark.asyncio
async def test_semantic_tool_not_denied_by_guard() -> None:
    sbx = FakeSandboxInstance()
    ex = _executor([Phase.EDIT], sbx)
    # app_update_content is in the EDIT scope → the guard lets it through (it then
    # fails for an unrelated reason — no app yet — NOT the scope guard).
    res = await ex.execute(call("app_update_content", section_id="hero", field="x", value="y"))
    assert not _scope_denied(res)


@pytest.mark.asyncio
async def test_utility_tool_always_passes_guard() -> None:
    sbx = FakeSandboxInstance()
    ex = _executor([Phase.EDIT], sbx)
    res = await ex.execute(call("think", thought="planning my next edit"))
    assert not _scope_denied(res)


@pytest.mark.asyncio
async def test_file_write_allowed_in_repair_phase() -> None:
    sbx = FakeSandboxInstance()
    ex = _executor([Phase.REPAIR], sbx)
    res = await ex.execute(call("file_write", path="index.html", content="<h1>fix</h1>"))
    assert not _scope_denied(res)  # repair permits raw write
    assert res.success and sbx._fs.get("index.html")


@pytest.mark.asyncio
async def test_live_phase_advance_then_block_end_to_end() -> None:
    # CONTRACT-ACTIVATE end-to-end: a real bootstrap tool succeeds → the tracker
    # advances BOOTSTRAP→EDIT via the executor's on_tool_success → a subsequent raw
    # file_write is then denied. This is exactly the runtime's wiring.
    sbx = FakeSandboxInstance()
    tracker = BuildPhaseTracker(_appkit())
    ex = DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=_STANDARD),
        sandbox=sbx,
        scope_guard=ContractScopeGuard.for_contract(_appkit(), tracker.current),
        on_tool_success=tracker.note_tool_success,
    )
    assert tracker.current() is Phase.BOOTSTRAP
    # file_write is blocked even in BOOTSTRAP (appkit bootstrap = app_create only)
    assert _scope_denied(await ex.execute(call("file_write", path="index.html", content="x")))
    # the real bootstrap tool runs + succeeds → advances the phase to EDIT
    created = await ex.execute(call("app_create", title="Acme"))
    assert created.success and tracker.current() is Phase.EDIT
    assert ".disco/appspec.json" in sbx._fs
    # now in EDIT: raw file_write is denied (must use the semantic app_* tools)
    assert _scope_denied(await ex.execute(call("file_write", path="index.html", content="y")))


@pytest.mark.asyncio
async def test_no_guard_means_no_enforcement() -> None:
    # a plain agent run (no contract) is unchanged: file_write just works
    sbx = FakeSandboxInstance()
    ex = DefaultToolExecutor(build_default_registry(), agent_scope(model_policy=_STANDARD), sandbox=sbx)
    res = await ex.execute(call("file_write", path="index.html", content="<h1>plain</h1>"))
    assert res.success and sbx._fs.get("index.html")
