"""CONTRACT-ACTIVATE: the runtime resolves a per-conversation build contract + a live
phase tracker, and hands the executor a ContractScopeGuard wired to that phase.

This proves the runtime-side of the activation: the guard the runtime builds enforces
the conversation's contract, advances on tool success, and defaults to CUSTOM.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.contract import ContractKind, Phase
from disco.tools import ProcessSandboxService


def _runtime() -> ConversationRuntime:
    return ConversationRuntime(SqliteEventStore(":memory:"), router=MagicMock(), sandbox_service=ProcessSandboxService())


def test_default_conversation_uses_custom_contract() -> None:
    rt = _runtime()
    guard, on_success = rt._build_scope_guard("c1")
    assert guard is not None and on_success is not None
    # CUSTOM bootstrap permits raw file_write/shell; edit does NOT (no clobber-rewrite)
    assert guard.check("file_write", is_mutating=True).allowed is True  # BOOTSTRAP
    contract, tracker = rt._build_trackers["c1"]
    assert contract.kind is ContractKind.CUSTOM


def test_declared_kind_governs_with_its_contract() -> None:
    rt = _runtime()
    rt.set_build_kind("c2", "appkit.leadgen")
    guard, on_success = rt._build_scope_guard("c2")
    assert guard is not None and on_success is not None
    contract, tracker = rt._build_trackers["c2"]
    assert contract.kind is ContractKind.APPKIT_LEADGEN
    # appkit bootstrap = app_create only → raw file_write denied even at bootstrap
    assert guard.check("file_write", is_mutating=True).allowed is False
    assert guard.check("app_create", is_mutating=False).allowed is True


def test_guard_advances_with_phase_via_on_success() -> None:
    rt = _runtime()
    rt.set_build_kind("c3", "appkit.leadgen")
    guard, on_success = rt._build_scope_guard("c3")
    assert on_success is not None
    _contract, tracker = rt._build_trackers["c3"]
    assert tracker.current() is Phase.BOOTSTRAP
    on_success("app_create")  # the runtime calls this when a bootstrap tool succeeds
    assert tracker.current() is Phase.EDIT
    # same guard object now reflects EDIT: app_update_content ok, file_write denied
    assert guard.check("app_update_content", is_mutating=True).allowed is True
    assert guard.check("file_write", is_mutating=True).allowed is False


def test_set_build_kind_none_resets_to_custom() -> None:
    rt = _runtime()
    rt.set_build_kind("c4", "appkit.leadgen")
    rt._build_scope_guard("c4")
    rt.set_build_kind("c4", None)  # clears the declared kind + the tracker
    _guard, _ = rt._build_scope_guard("c4")
    assert rt._build_trackers["c4"][0].kind is ContractKind.CUSTOM


def test_note_verify_result_advances_export_and_repair() -> None:
    rt = _runtime()
    rt.set_build_kind("c5", "appkit.leadgen")
    guard, _ = rt._build_scope_guard("c5")
    _c, tracker = rt._build_trackers["c5"]
    rt.note_build_verify_result("c5", passed=False)
    assert tracker.current() is Phase.REPAIR
    assert guard is not None and guard.check("file_write", is_mutating=True).allowed is True  # repair allows
    rt.note_build_verify_result("c5", passed=True)
    assert tracker.current() is Phase.EXPORT
    # no tracker for an unknown conversation → no-op, never raises
    rt.note_build_verify_result("unknown", passed=True)


@pytest.mark.asyncio
async def test_forget_conversation_evicts_build_state() -> None:
    rt = _runtime()
    rt.set_build_kind("c6", "appkit.leadgen")
    rt._build_scope_guard("c6")
    assert "c6" in rt._build_trackers and "c6" in rt._build_kind
    await rt.forget_conversation("c6")
    assert "c6" not in rt._build_trackers and "c6" not in rt._build_kind
