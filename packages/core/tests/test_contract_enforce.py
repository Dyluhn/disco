"""CONTRACT-ENFORCE tests: per-phase contract scope decisions + the guard."""

from __future__ import annotations

import pytest
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


def test_static_site_semantic_edits_allowed_in_edit() -> None:
    c = BuildContractRegistry.default().get(ContractKind.STATIC_SITE)
    assert c is not None
    s = compile_tool_scopes(c)
    assert decide_tool_in_scope(s, Phase.EDIT, "file_edit", is_mutating=True).allowed
    assert decide_tool_in_scope(s, Phase.EDIT, "file_replace_lines", is_mutating=True).allowed


def test_dangerous_tool_denied_outside_its_phase() -> None:
    s = compile_tool_scopes(_appkit())
    # file_write is repair-only for appkit → DENIED in EDIT, allowed in REPAIR
    d_edit = decide_tool_in_scope(s, Phase.EDIT, "file_write")
    assert d_edit.allowed is False and "file_write" in d_edit.reason
    assert decide_tool_in_scope(s, Phase.REPAIR, "file_write").allowed is True


@pytest.mark.parametrize("phase", tuple(Phase))
@pytest.mark.parametrize(
    ("tool", "is_mutating"),
    (
        # bookkeeping / virtual meta
        ("update_plan_progress", True),
        ("plan_step", True),
        ("think", False),
        ("notify_user", False),
        ("ask_user", False),
        ("questions_v2", False),
        ("clarify", False),
        ("finish", False),
        ("ready_for_app_verification", False),
        # read / inspect
        ("file_read", False),
        ("file_list", False),
        ("search", False),
        ("extract", False),
        ("server_status", False),
        # preview / verification tools whose registry metadata may be non-read-only
        ("preview_start", True),
        ("preview_status", False),
        ("preview_logs", False),
        ("preview_stop", True),
        ("verify_web_app", True),
    ),
)
def test_phase_neutral_tools_allowed_in_every_phase(
    phase: Phase, tool: str, is_mutating: bool
) -> None:
    s = compile_tool_scopes(_appkit())
    assert decide_tool_in_scope(s, phase, tool, is_mutating=is_mutating).allowed is True


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
    assert (
        decide_tool_in_scope(s, Phase.EDIT, "totally_new_writer", is_mutating=True).allowed is False
    )
    # and a read-only tool the contract doesn't scope is ungoverned → passes
    assert decide_tool_in_scope(s, Phase.EDIT, "some_reader", is_mutating=False).allowed is True


def test_phase_neutral_utilities_pass_in_edit() -> None:
    s = compile_tool_scopes(_appkit())
    for util in ("think", "preview_start", "file_read", "ready_for_app_verification"):
        assert decide_tool_in_scope(s, Phase.EDIT, util).allowed is True, util


def test_custom_contract_permits_broad_repair() -> None:
    c = BuildContractRegistry.default().get(ContractKind.CUSTOM)
    assert c is not None
    s = compile_tool_scopes(c)
    assert decide_tool_in_scope(s, Phase.REPAIR, "file_write").allowed is True
    # CONTRACT-ACTIVATE repin (2026-07-10): CUSTOM (the every-unmapped-build
    # fallback) now carries the working set in EDIT too; the remaining real
    # denial classes are cross-kind mutators and the read-only VERIFY phase.
    assert decide_tool_in_scope(s, Phase.EDIT, "file_write").allowed is True
    # is_mutating mirrors the real dispatch boundary (read_only metadata);
    # without it the name-list fallback treats unknown tools as ungoverned.
    assert decide_tool_in_scope(s, Phase.EDIT, "doc_set_section", is_mutating=True).allowed is False
    assert decide_tool_in_scope(s, Phase.VERIFY, "file_write").allowed is False


def test_guard_reflects_live_phase() -> None:
    phase = [Phase.EDIT]
    guard = ContractScopeGuard.for_contract(_appkit(), lambda: phase[0])
    assert guard.check("file_write").allowed is False  # EDIT: raw write blocked
    assert guard.check("app_update_content").allowed is True  # EDIT: semantic tool ok
    phase[0] = Phase.REPAIR
    assert guard.check("file_write").allowed is True  # REPAIR: raw write permitted
