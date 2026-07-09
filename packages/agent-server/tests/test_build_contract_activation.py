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


def test_starter_kit_for_resolves_the_contract_starter() -> None:
    rt = _runtime()
    assert rt._starter_kit_for("none") is None  # no declared build → no starter
    rt.set_build_kind("site", "static.site")
    assert rt._starter_kit_for("site") == "app_shell"
    rt.set_build_kind("app", "appkit.leadgen")
    assert rt._starter_kit_for("app") == "lead_form"
    rt.set_build_kind("deck", "deck")
    assert rt._starter_kit_for("deck") is None  # deck has no file-map starter


def test_finalizer_alias_only_for_non_custom_declared_kind() -> None:
    rt = _runtime()
    # no declared kind → no alias (a plain build)
    assert rt._finalizer_alias_for("none") is None
    # a declared "custom" kind → no alias (never fabricate ready_for_artifact_verification)
    rt.set_build_kind("cust", "custom")
    assert rt._finalizer_alias_for("cust") is None
    # an unknown kind falls back to CUSTOM → still no alias
    rt.set_build_kind("unk", "totally.unknown.kind")
    assert rt._finalizer_alias_for("unk") is None
    # a real declared kind → its finalizer
    rt.set_build_kind("app", "appkit.leadgen")
    assert rt._finalizer_alias_for("app") == "ready_for_app_verification"
    rt.set_build_kind("doc", "document")
    assert rt._finalizer_alias_for("doc") == "ready_for_document_verification"


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


def test_expected_delivery_mode_reflects_contract() -> None:
    rt = _runtime()
    # a plain (non-build) conversation has no delivery shape
    assert rt.expected_delivery_mode("plain") is None
    # a declared appkit run → "app" (open in preview)
    rt.set_build_kind("d1", "appkit.leadgen")
    assert rt.expected_delivery_mode("d1") == "app"
    # a declared deck run → "files" (download)
    rt.set_build_kind("d2", "deck")
    assert rt.expected_delivery_mode("d2") == "files"


@pytest.mark.asyncio
async def test_forget_conversation_evicts_build_state() -> None:
    rt = _runtime()
    rt.set_build_kind("c6", "appkit.leadgen")
    rt._build_scope_guard("c6")
    assert "c6" in rt._build_trackers and "c6" in rt._build_kind
    await rt.forget_conversation("c6")
    assert "c6" not in rt._build_trackers and "c6" not in rt._build_kind


# ---------------------------------------------------------------------------
# P7 truth-in-advertising (2026-07-09 coffee-shop autopsy): scaffold_starter is
# withheld from the ADVERTISED set whenever no starter kit resolves — which,
# with CONTRACT-ACTIVATE unwired in production, is every run today. It stays in
# allowed_tools (typed no_starter error remains the backstop) and reappears the
# moment a kind with a starter kit is activated.
# ---------------------------------------------------------------------------


def test_scaffold_starter_not_advertised_without_starter_kit() -> None:
    from disco.core.llm import ModelExecutionPolicy
    from disco.tools import agent_scope, artifact_scope

    rt = _runtime()
    # No set_build_kind (the production reality) → no starter kit resolves.
    for base in (
        artifact_scope(),
        agent_scope(model_policy=ModelExecutionPolicy()),
    ):
        scope = rt._narrow_scope_for_starter(base, "c-nokit")
        advertised = (
            scope.advertised_tools if scope.advertised_tools is not None else scope.allowed_tools
        )
        assert "scaffold_starter" not in advertised, (
            "scaffold_starter must NOT be advertised when no starter kit resolves "
            "(it would fail no_starter on every call — the false affordance)"
        )
        # …but stays CALLABLE (security allowlist unchanged) so the typed
        # backstop error, replay, and qualified-name calls keep working.
        assert "scaffold_starter" in scope.allowed_tools


def test_scaffold_starter_advertised_when_kind_resolves_a_kit() -> None:
    from disco.tools import artifact_scope

    rt = _runtime()
    rt.set_build_kind("c-site", "static.site")  # contract declares app_shell
    scope = rt._narrow_scope_for_starter(artifact_scope(), "c-site")
    advertised = (
        scope.advertised_tools if scope.advertised_tools is not None else scope.allowed_tools
    )
    assert "scaffold_starter" in advertised, (
        "an activated contract WITH a starter kit must advertise scaffold_starter"
    )


def test_set_build_kind_evicts_cached_loop_and_executor() -> None:
    """codex four-fix defect #2: the executor bakes contract-derived state at
    build time (starter-kit ToolContext stamp + scaffold advertise split), so a
    cached loop/executor must be evicted when the kind changes — otherwise a
    later activation keeps serving the OLD contract. Reuses the guarded
    settings-eviction path (never evicts under a live run)."""
    rt = _runtime()
    rt._loops["c-act"] = MagicMock()
    rt._executors["c-act"] = MagicMock(_sandbox=None)
    rt.set_build_kind("c-act", "static.site")
    assert "c-act" not in rt._loops
    assert "c-act" not in rt._executors
    # ...and the newly-resolved contract now advertises the starter tool.
    from disco.tools import artifact_scope

    scope = rt._narrow_scope_for_starter(artifact_scope(), "c-act")
    advertised = (
        scope.advertised_tools if scope.advertised_tools is not None else scope.allowed_tools
    )
    assert "scaffold_starter" in advertised
