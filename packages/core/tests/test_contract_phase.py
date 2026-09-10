"""CONTRACT-ACTIVATE tests: the build-phase state machine + the guard wired to it."""

from __future__ import annotations

from disco.core.contract import (
    BuildContractRegistry,
    BuildPhaseTracker,
    ContractKind,
    ContractScopeGuard,
    Phase,
)


def _appkit():
    c = BuildContractRegistry.default().get(ContractKind.APPKIT_LEADGEN)
    assert c is not None
    return c


def test_starts_in_bootstrap() -> None:
    assert BuildPhaseTracker(_appkit()).current() is Phase.BOOTSTRAP


def test_bootstrap_tool_advances_to_edit() -> None:
    t = BuildPhaseTracker(_appkit())
    t.note_tool_success("app_create")  # appkit bootstrap tool
    assert t.current() is Phase.EDIT


def test_non_bootstrap_success_does_not_leave_bootstrap() -> None:
    t = BuildPhaseTracker(_appkit())
    t.note_tool_success("think")  # not a bootstrap tool
    assert t.current() is Phase.BOOTSTRAP


def test_finalizer_moves_to_verify() -> None:
    t = BuildPhaseTracker(_appkit())
    t.note_tool_success("app_create")
    t.note_tool_success("ready_for_app_verification")  # the finalizer
    assert t.current() is Phase.VERIFY


def test_verifier_pass_exports_fail_repairs() -> None:
    t = BuildPhaseTracker(_appkit())
    t.note_finalizer_called()
    t.note_verifier_result(passed=False)
    assert t.current() is Phase.REPAIR
    t.note_verifier_result(passed=True)
    assert t.current() is Phase.EXPORT


def test_guard_follows_the_tracker_live() -> None:
    # the executor reads tracker.current; prove the enforcement decision tracks phase
    t = BuildPhaseTracker(_appkit())
    guard = ContractScopeGuard.for_contract(_appkit(), t.current)

    # BOOTSTRAP: app_create allowed, file_write (mutator) denied
    assert guard.check("app_create", is_mutating=True).allowed is True
    assert guard.check("file_write", is_mutating=True).allowed is False

    t.note_tool_success("app_create")  # → EDIT
    # EDIT: semantic edit tool allowed, raw file_write denied (targeted-edit law)
    assert guard.check("app_update_content", is_mutating=True).allowed is True
    assert guard.check("file_write", is_mutating=True).allowed is False

    t.note_finalizer_called()
    t.note_verifier_result(passed=False)  # → REPAIR
    # REPAIR: the escape hatch is now permitted to fix the artifact
    assert guard.check("file_write", is_mutating=True).allowed is True
