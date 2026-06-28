"""CONTRACT-ENFORCE tests: per-phase contract scope decisions + the guard."""

from __future__ import annotations

from disco.core.contract import (
    BuildContractRegistry,
    ContractKind,
    ContractScopeGuard,
    Phase,
    compile_tool_scopes,
    decide_tool_in_scope,
)


def _appkit():
    c = BuildContractRegistry.default().get(ContractKind.APPKIT_LEADGEN)
    assert c is not None
    return c


def test_in_phase_tool_allowed() -> None:
    s = compile_tool_scopes(_appkit())
    assert decide_tool_in_scope(s, Phase.EDIT, "app_update_content").allowed is True
    assert decide_tool_in_scope(s, Phase.BOOTSTRAP, "app_create").allowed is True


def test_dangerous_tool_denied_outside_its_phase() -> None:
    s = compile_tool_scopes(_appkit())
    # file_write is repair-only for appkit → DENIED in EDIT, allowed in REPAIR
    d_edit = decide_tool_in_scope(s, Phase.EDIT, "file_write")
    assert d_edit.allowed is False and "file_write" in d_edit.reason
    assert decide_tool_in_scope(s, Phase.REPAIR, "file_write").allowed is True


def test_bootstrap_tool_denied_during_edit() -> None:
    s = compile_tool_scopes(_appkit())
    # app_create is a bootstrap tool → governed, not permitted mid-EDIT
    assert decide_tool_in_scope(s, Phase.EDIT, "app_create").allowed is False


def test_shell_denied_even_though_unnamed() -> None:
    # appkit never names shell, but it is a dangerous escape hatch → governed + denied
    s = compile_tool_scopes(_appkit())
    assert decide_tool_in_scope(s, Phase.EDIT, "shell").allowed is False


def test_unlisted_mutator_gated_via_metadata_not_name() -> None:
    # a mutating tool NOT in the DANGEROUS_TOOLS name list (e.g. a future writer the
    # list never learned about) is still gated when the dispatch boundary reports it
    # mutates — enforcement is metadata-driven, so there is no name-list bypass.
    s = compile_tool_scopes(_appkit())
    assert decide_tool_in_scope(s, Phase.EDIT, "totally_new_writer", is_mutating=True).allowed is False
    # and a read-only tool the contract doesn't scope is ungoverned → passes
    assert decide_tool_in_scope(s, Phase.EDIT, "some_reader", is_mutating=False).allowed is True


def test_ungoverned_utilities_always_pass() -> None:
    s = compile_tool_scopes(_appkit())
    for util in ("think", "preview_start", "file_read", "ready_for_app_verification"):
        assert decide_tool_in_scope(s, Phase.EDIT, util).allowed is True, util


def test_custom_contract_permits_broad_repair() -> None:
    c = BuildContractRegistry.default().get(ContractKind.CUSTOM)
    assert c is not None
    s = compile_tool_scopes(c)
    assert decide_tool_in_scope(s, Phase.REPAIR, "file_write").allowed is True
    assert decide_tool_in_scope(s, Phase.EDIT, "file_write").allowed is False  # edit ≠ repair


def test_guard_reflects_live_phase() -> None:
    phase = [Phase.EDIT]
    guard = ContractScopeGuard.for_contract(_appkit(), lambda: phase[0])
    assert guard.check("file_write").allowed is False  # EDIT: raw write blocked
    assert guard.check("app_update_content").allowed is True  # EDIT: semantic tool ok
    phase[0] = Phase.REPAIR
    assert guard.check("file_write").allowed is True  # REPAIR: raw write permitted
