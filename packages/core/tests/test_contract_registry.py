"""CONTRACT-2 tests: BuildContractRegistry — every kind resolvable + coherent,
brief lookup, and the custom fallback."""

from __future__ import annotations

from disco.core.contract import BuildContract, BuildContractRegistry, ContractKind


def test_default_registry_has_every_kind() -> None:
    reg = BuildContractRegistry.default()
    assert reg.kinds() == frozenset(ContractKind)
    for kind in ContractKind:
        c = reg.get(kind)
        assert c is not None and c.kind is kind


def test_every_builtin_contract_is_coherent_and_serializable() -> None:
    reg = BuildContractRegistry.default()
    for kind in ContractKind:
        c = reg.get(kind)
        assert c is not None
        # kind/artifact coherence + finalizer convention are enforced by the models;
        # a successful round-trip proves the built-in is well-formed.
        assert BuildContract.model_validate(c.model_dump(mode="json")) == c
        assert c.verify.finalizer.startswith("ready_for_") and c.verify.finalizer.endswith("_verification")


def test_get_for_brief_by_kind() -> None:
    reg = BuildContractRegistry.default()
    c = reg.get_for_brief({"kind": "static.site"})
    assert c.kind is ContractKind.STATIC_SITE
    assert "index.html" in c.artifact.required_files


def test_get_for_brief_missing_kind_is_custom() -> None:
    reg = BuildContractRegistry.default()
    # a brief that declares NO kind is a legitimate custom build
    assert reg.get_for_brief({}).kind is ContractKind.CUSTOM
    assert reg.get_for_brief(None).kind is ContractKind.CUSTOM


def test_get_for_brief_unknown_kind_raises_by_default() -> None:
    reg = BuildContractRegistry.default()
    import pytest

    for bad in ("nonsense", "static_site", 123, ["x"]):
        with pytest.raises(ValueError):
            reg.get_for_brief({"kind": bad})  # type: ignore[dict-item]
    # opt-in compatibility fallback
    assert reg.get_for_brief({"kind": "nonsense"}, strict_kind=False).kind is ContractKind.CUSTOM


def test_get_for_brief_partial_registry_never_keyerrors() -> None:
    reg = BuildContractRegistry()  # empty — no CUSTOM registered
    c = reg.get_for_brief({})
    assert c.kind is ContractKind.CUSTOM  # deterministic minimal fallback, no KeyError
    c2 = reg.get_for_brief({"kind": "deck"})
    assert c2.kind is ContractKind.CUSTOM  # deck unregistered → custom fallback


def test_all_builtin_contract_tools_are_registered() -> None:
    # every tool a built-in contract references MUST exist in the live tool registry
    # (no false affordance — a contract can't scope a tool that doesn't ship).
    from disco.tools.builtin import build_default_registry

    registered = build_default_registry().names()
    reg = BuildContractRegistry.default()
    for kind in ContractKind:
        c = reg.get(kind)
        assert c is not None
        tools = set(c.bootstrap.tools) | set(c.edit.edit_tools) | set(c.edit.repair_tools)
        missing = tools - registered
        assert not missing, f"{kind.value} contract references unregistered tools: {sorted(missing)}"


def test_appkit_leadgen_contract_shape() -> None:
    c = BuildContractRegistry.default().get(ContractKind.APPKIT_LEADGEN)
    assert c is not None
    assert ".disco/appspec.json" in c.artifact.required_files
    # P4: appkit now scopes the real semantic AppKit mutation tools
    assert "app_create" in c.bootstrap.tools
    assert "app_update_content" in c.edit.edit_tools
    assert "file_write" in c.edit.repair_tools  # raw write is repair-only
    assert c.verify.finalizer == "ready_for_app_verification"
    assert c.export is not None and c.export.name == "cloudflare_project"
    assert c.prompt_pack == "build_appkit_leadgen"
    assert c.ui_card == "AppCard"


def test_custom_contract_allows_rewrite() -> None:
    c = BuildContractRegistry.default().get(ContractKind.CUSTOM)
    assert c is not None and c.edit.rewrite_allowed is True


def test_register_overrides() -> None:
    reg = BuildContractRegistry()
    assert reg.get(ContractKind.DECK) is None  # empty registry
    custom_deck = BuildContract.minimal(ContractKind.DECK, finalizer="ready_for_deck_verification")
    reg.register(custom_deck)
    assert reg.get(ContractKind.DECK) == custom_deck
