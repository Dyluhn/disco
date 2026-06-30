"""CD-TOOLS-6 — the VERIFY phase is a READ-ONLY DIAGNOSTICS phase. The wired ContractScopeGuard
must let the verifier INSPECT (verify_web_app / browser / file_read / server_status) while DENYING
every mutator (with the VERIFIER_ONLY_TOOL_BLOCKED code). EDIT/REPAIR are unchanged."""

from __future__ import annotations

from disco.core.contract import compile_tool_scopes
from disco.core.contract.enforce import decide_tool_in_scope
from disco.core.contract.models import ContractKind
from disco.core.contract.registry import BuildContractRegistry
from disco.core.contract.scopes import Phase

_DIAGNOSTICS = ["verify_web_app", "file_read", "file_list", "search", "server_status",
                "preview_status", "preview_logs", "think"]
# raw `browser` is a MUTATOR-grade tool here (BrowserArgs admits click/fill/submit) → DENIED in
# VERIFY; the verifier inspects via verify_web_app's encapsulated read-only checks instead.
_MUTATORS = ["file_write", "file_edit", "file_replace_lines", "exact_replace", "safe_write_file",
             "shell", "code_exec", "app_create", "deck_patch", "preview_start", "browser"]

_FINALIZER = "ready_for_static_site_verification"


def _scopes():
    c = BuildContractRegistry.default().get(ContractKind.STATIC_SITE)
    assert c is not None
    return compile_tool_scopes(c)


def test_verify_phase_allows_each_diagnostic():
    s = _scopes()
    for tool in _DIAGNOSTICS:
        d = decide_tool_in_scope(s, Phase.VERIFY, tool, is_mutating=False)
        assert d.allowed, f"{tool} should be allowed in VERIFY"


def test_verify_phase_allows_verify_web_app_even_though_it_is_not_read_only():
    # verify_web_app is read_only=False (it drives the sandbox) — without the explicit verify scope
    # it'd be denied as a mutator, stranding the verifier. It must be ALLOWED.
    s = _scopes()
    assert decide_tool_in_scope(s, Phase.VERIFY, "verify_web_app", is_mutating=True).allowed


def test_raw_browser_is_denied_in_verify():
    # raw browser (click/fill/submit) would turn VERIFY into general automation → DENIED; inspect
    # via verify_web_app instead (codex CD-TOOLS-6 round-1).
    s = _scopes()
    d = decide_tool_in_scope(s, Phase.VERIFY, "browser", is_mutating=True)
    assert not d.allowed and d.code == "VERIFIER_ONLY_TOOL_BLOCKED"


def test_verify_phase_denies_every_mutator_with_code():
    s = _scopes()
    for tool in _MUTATORS:
        d = decide_tool_in_scope(s, Phase.VERIFY, tool, is_mutating=True)
        assert not d.allowed, f"{tool} must be denied in VERIFY"
        assert d.code == "VERIFIER_ONLY_TOOL_BLOCKED", f"{tool} denial should carry the code"


def test_finalizer_callable_in_any_phase():
    s = _scopes()
    for phase in (Phase.EDIT, Phase.VERIFY, Phase.REPAIR):
        # the finalizer is a control signal (read_only) — never blocked
        assert decide_tool_in_scope(s, phase, _FINALIZER, is_mutating=False).allowed


def test_edit_phase_unchanged_mutator_in_edit_not_verifier_blocked():
    # a mutator wrongly used in EDIT is still denied, but NOT with the verifier-only code (that code
    # is specific to the VERIFY phase) — proves the code is scoped to VERIFY, no over-tagging.
    s = _scopes()
    d = decide_tool_in_scope(s, Phase.EDIT, "safe_write_file", is_mutating=True)
    assert not d.allowed  # not in edit_tools (which is file_edit/exact_replace)
    assert d.code != "VERIFIER_ONLY_TOOL_BLOCKED"
    # and the contract's real edit tools ARE allowed in EDIT
    assert decide_tool_in_scope(s, Phase.EDIT, "file_edit", is_mutating=True).allowed
