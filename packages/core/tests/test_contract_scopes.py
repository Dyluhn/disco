"""CONTRACT-3 tests: Contract→ToolScope compiler — hard per-phase allowlists."""

from __future__ import annotations

from disco.core.contract import (
    ArtifactContract,
    BuildContract,
    BuildContractRegistry,
    ContractKind,
    ContractToolScopes,
    EditContract,
    PHASE_NEUTRAL_TOOLS,
    Phase,
    ToolPack,
    VerificationContract,
    compile_tool_scopes,
)


def _contract(*, bootstrap=(), edit=(), repair=(), rewrite=False, finalizer="ready_for_artifact_verification"):
    return BuildContract(
        kind=ContractKind.CUSTOM,
        artifact=ArtifactContract(kind=ContractKind.CUSTOM),
        bootstrap=ToolPack(name="b", tools=bootstrap),
        edit=EditContract(edit_tools=edit, repair_tools=repair, rewrite_allowed=rewrite),
        verify=VerificationContract(finalizer=finalizer),
    )


def _neutral(finalizer: str = "ready_for_artifact_verification") -> frozenset[str]:
    return PHASE_NEUTRAL_TOOLS | frozenset({finalizer})


def test_scopes_project_the_packs_per_phase() -> None:
    c = _contract(bootstrap=("app_create",), edit=("app_update_content",), repair=("file_write",))
    s = compile_tool_scopes(c)
    neutral = _neutral()
    assert s.bootstrap == neutral | frozenset({"app_create"})
    assert s.edit == neutral | frozenset({"app_update_content"})
    assert s.repair == neutral | frozenset({"file_write"})
    # REL-3 — verify/export carry only the phase-neutral tools; mutation packs do not bleed in.
    assert s.verify == neutral
    assert s.export == neutral
    assert "ready_for_artifact_verification" in s.verify
    assert {"verify_web_app", "file_read", "server_status", "preview_start"} <= s.verify
    assert not ({"file_write", "exact_replace", "safe_write_file", "shell"} & s.verify)


def test_tool_absent_from_phase_is_hard_excluded() -> None:
    # the campaign invariant: a bootstrap pack of ONLY app_create cannot file_write
    s = compile_tool_scopes(_contract(bootstrap=("app_create",), edit=("app_update_content",)))
    assert s.allowed(Phase.BOOTSTRAP, "app_create") is True
    assert s.allowed(Phase.BOOTSTRAP, "file_write") is False  # not in the bootstrap pack
    assert s.allowed(Phase.EDIT, "app_update_content") is True
    assert s.allowed(Phase.EDIT, "file_write") is False


def test_repair_scope_is_bounded_to_repair_tools() -> None:
    s = compile_tool_scopes(_contract(edit=("file_edit",), repair=("file_write",)))
    assert s.allowed(Phase.REPAIR, "file_write") is True
    assert s.allowed(Phase.REPAIR, "file_edit") is False  # edit tool is not a repair tool
    assert s.allowed(Phase.EDIT, "file_write") is False  # rewrite stays out of edit


def test_verify_scope_is_neutral_finalizer_without_mutators() -> None:
    # CD-TOOLS-6 / REL-3 — verify carries the finalizer plus phase-neutral diagnostics,
    # and NO contract mutation tool.
    s = compile_tool_scopes(_contract(finalizer="ready_for_app_verification"))
    assert "ready_for_app_verification" in s.verify
    assert {"verify_web_app", "file_read", "server_status", "preview_start"} <= s.verify
    assert not ({"file_write", "exact_replace", "safe_write_file", "app_create"} & s.verify)
    assert s.allowed(Phase.VERIFY, "ready_for_app_verification") is True
    assert s.allowed(Phase.VERIFY, "file_write") is False


def test_static_site_contract_cannot_rewrite_during_edit() -> None:
    # a real built-in: editing must use file_edit/file_replace_lines, NOT file_write
    c = BuildContractRegistry.default().get(ContractKind.STATIC_SITE)
    assert c is not None
    s = compile_tool_scopes(c)
    assert s.allowed(Phase.BOOTSTRAP, "file_write") is True  # bootstrap may create files
    assert s.allowed(Phase.EDIT, "file_edit") is True
    assert s.allowed(Phase.EDIT, "file_write") is False  # cannot clobber-rewrite during edit


def test_custom_contract_permits_broad_repair_only_as_declared() -> None:
    c = BuildContractRegistry.default().get(ContractKind.CUSTOM)
    assert c is not None and c.edit.rewrite_allowed is True
    s = compile_tool_scopes(c)
    # custom DECLARED file_write as a repair tool → allowed in repair, still not in edit
    assert s.allowed(Phase.REPAIR, "file_write") is True
    assert s.allowed(Phase.EDIT, "file_write") is False


def test_shell_hard_excluded_from_bootstrap_unless_declared() -> None:
    # the done-when invariant: generic file/shell are NOT available during bootstrap
    # unless the contract's bootstrap pack explicitly declares them.
    without = compile_tool_scopes(_contract(bootstrap=("app_create",)))
    assert without.allowed(Phase.BOOTSTRAP, "shell") is False
    assert without.allowed(Phase.BOOTSTRAP, "file_write") is False
    # paired positive: a contract that DOES declare shell in bootstrap allows it
    with_shell = compile_tool_scopes(_contract(bootstrap=("shell", "file_write")))
    assert with_shell.allowed(Phase.BOOTSTRAP, "shell") is True
    assert with_shell.allowed(Phase.BOOTSTRAP, "file_write") is True


def test_scopes_roundtrip() -> None:
    s = compile_tool_scopes(_contract(bootstrap=("a", "b"), edit=("c",)))
    assert ContractToolScopes.model_validate(s.model_dump(mode="json")) == s


def test_phase_neutral_tools_are_projected_to_every_phase() -> None:
    s = compile_tool_scopes(_contract(bootstrap=("app_create",), edit=("file_edit",)))
    for phase in Phase:
        for tool in PHASE_NEUTRAL_TOOLS | frozenset({"ready_for_artifact_verification"}):
            assert s.allowed(phase, tool) is True, f"{tool} should be neutral in {phase.value}"


def test_every_builtin_compiles() -> None:
    reg = BuildContractRegistry.default()
    for kind in ContractKind:
        c = reg.get(kind)
        assert c is not None
        s = compile_tool_scopes(c)
        # REL-3 — every phase gets the neutral set + active finalizer, NEVER broad mutators.
        for phase in Phase:
            assert c.verify.finalizer in s.for_phase(phase)
            assert "verify_web_app" in s.for_phase(phase)
            assert "update_plan_progress" in s.for_phase(phase)
        assert not ({"file_write", "exact_replace", "safe_write_file", "shell", "browser"} & s.verify)
