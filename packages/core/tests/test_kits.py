"""P7-KITS tests: StarterKit registry (host-owned scaffolds) + BrandKit projection +
the contract↔starter coherence invariant."""

from __future__ import annotations

import json

import pytest

from disco.core.appkit import AppSpec, render_html
from disco.core.contract import BuildContractRegistry, ContractKind
from disco.core.kits import (
    BRAND_NAMES,
    StarterKitRegistry,
    appkit_brand,
    brand_to_appkit_tokens,
    lead_form_appspec,
)


# --- starter registry ---------------------------------------------------------
def test_registry_resolves_builtins() -> None:
    reg = StarterKitRegistry.default()
    assert reg.ids() == frozenset({"app_shell", "lead_form"})
    assert reg.get("app_shell") is not None
    assert reg.get("lead_form") is not None
    assert reg.get("nope") is None


def test_app_shell_scaffolds_renderable_index() -> None:
    files = StarterKitRegistry.default().get("app_shell").scaffold("Acme Co")  # type: ignore[union-attr]
    assert set(files) == {"index.html"}
    assert "<title>Acme Co</title>" in files["index.html"]
    assert "<style>" in files["index.html"] and "<link" not in files["index.html"]


def test_lead_form_scaffolds_appspec_and_html() -> None:
    files = StarterKitRegistry.default().get("lead_form").scaffold("Acme Roofing")  # type: ignore[union-attr]
    assert set(files) == {".disco/appspec.json", "index.html"}
    spec = AppSpec.model_validate_json(files[".disco/appspec.json"])
    assert spec.title == "Acme Roofing"
    assert [s.id for s in spec.sections] == ["hero", "lead"]
    assert "Acme Roofing" in files["index.html"] and "<form" in files["index.html"]


def test_lead_form_is_byte_identical_to_app_create_default() -> None:
    # SINGLE SOURCE: the lead_form starter == what app_create writes for sections=None.
    title = "Byte Equivalence Co"
    spec = lead_form_appspec(title)
    files = StarterKitRegistry.default().get("lead_form").scaffold(title)  # type: ignore[union-attr]
    assert files[".disco/appspec.json"] == spec.model_dump_json(indent=2) + "\n"
    assert files["index.html"] == render_html(spec)


def test_scaffold_paths_are_safe() -> None:
    from disco.core.kits.starter import StarterKit

    bad = StarterKit("evil", lambda _t: {"../etc/passwd": "x"})
    with pytest.raises(ValueError):
        bad.scaffold("t")
    bad2 = StarterKit("evil2", lambda _t: {"/abs/path": "x"})
    with pytest.raises(ValueError):
        bad2.scaffold("t")


# --- contract↔starter coherence ----------------------------------------------
def test_every_contract_starter_resolves() -> None:
    sreg = StarterKitRegistry.default()
    creg = BuildContractRegistry.default()
    for kind in ContractKind:
        c = creg.get(kind)
        assert c is not None
        sk = c.artifact.starter_kit
        if sk is not None:  # deck/document/workflow/custom legitimately have none
            assert sreg.get(sk) is not None, f"{kind.value} starter {sk!r} does not resolve"


def test_deck_no_longer_claims_an_unresolvable_starter() -> None:
    c = BuildContractRegistry.default().get(ContractKind.DECK)
    assert c is not None
    assert c.artifact.starter_kit is None  # slides_generate is the materializer
    assert c.artifact.required_files == ("deck.authored.json",)  # the real AuthoredDeck source


# --- brand projection ---------------------------------------------------------
def test_brand_names_come_from_the_real_theme_registry() -> None:
    assert {"disco", "neutral"} <= BRAND_NAMES  # projects the existing core.brand themes


def test_brand_projects_into_appkit_token_keys() -> None:
    tokens = appkit_brand("neutral")
    assert set(tokens) == {"primary", "accent", "bg", "fg", "font"}
    assert all(isinstance(v, str) and v for v in tokens.values())


def test_unknown_brand_raises() -> None:
    with pytest.raises(ValueError):
        appkit_brand("not_a_brand")
