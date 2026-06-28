"""WPP-1 tests: WorkflowPromptPack format + loader, and the contract↔pack coherence
invariant (every contract's prompt_pack id resolves to a complete pack)."""

from __future__ import annotations

import pytest

from disco.core.contract import BuildContractRegistry, ContractKind
from disco.core.workflows import (
    REQUIRED_SECTIONS,
    PromptPack,
    PromptPackRegistry,
    parse_prompt_pack,
)

_PACK_IDS = ("build_static_site", "build_appkit_leadgen", "build_deck", "build_document")


def test_parse_keys_sections_by_slugged_header() -> None:
    pack = parse_prompt_pack("x", "## Role\nyou build\n\n## Done criteria\nit works\n")
    assert pack.section("role") == "you build"
    assert pack.section("done_criteria") == "it works"
    assert pack.section("absent") == ""


def test_required_sections_constant_is_complete() -> None:
    # the 11 sections a pack must define
    assert len(REQUIRED_SECTIONS) == 11
    assert "role" in REQUIRED_SECTIONS and "done_criteria" in REQUIRED_SECTIONS


def test_registry_loads_every_bundled_pack() -> None:
    reg = PromptPackRegistry()
    ids = reg.ids()
    for pid in _PACK_IDS:
        assert pid in ids, pid
        pack = reg.get(pid)
        assert isinstance(pack, PromptPack)
        assert pack.pack_id == pid


def test_every_bundled_pack_has_all_required_sections() -> None:
    reg = PromptPackRegistry()
    for pid in _PACK_IDS:
        pack = reg.require(pid)
        missing = pack.missing_required()
        assert not missing, f"{pid} missing required sections: {missing}"


def test_render_returns_full_text() -> None:
    reg = PromptPackRegistry()
    pack = reg.require("build_static_site")
    assert pack.render() == pack.raw
    assert "ready_for_static_site_verification" in pack.render()


def test_missing_pack_get_is_none_require_raises() -> None:
    reg = PromptPackRegistry()
    assert reg.get("does_not_exist") is None
    with pytest.raises(KeyError):
        reg.require("does_not_exist")


# --- the coherence invariant: every contract's prompt_pack resolves -----------
def test_every_contract_prompt_pack_resolves_to_a_complete_pack() -> None:
    creg = BuildContractRegistry.default()
    preg = PromptPackRegistry()
    for kind in ContractKind:
        c = creg.get(kind)
        assert c is not None
        if c.prompt_pack is None:
            continue  # workflow.output / custom legitimately have no pack
        pack = preg.get(c.prompt_pack)
        assert pack is not None, f"{kind.value} → missing prompt pack {c.prompt_pack!r}"
        assert not pack.missing_required(), f"{c.prompt_pack} incomplete: {pack.missing_required()}"


# --- WPP-1 round-1 hardening (Codex) ------------------------------------------
def test_interactive_prototype_now_has_its_own_pack() -> None:
    reg = PromptPackRegistry()
    assert "build_interactive_prototype" in reg.ids()
    assert not reg.require("build_interactive_prototype").missing_required()


def test_parse_duplicate_section_raises() -> None:
    with pytest.raises(ValueError):
        parse_prompt_pack("dup", "## Role\na\n## Role\nb\n")


def test_parse_ignores_headers_inside_code_fences() -> None:
    text = "## Role\nyou build\n```\n## Not A Section\nsome code\n```\nmore role\n"
    pack = parse_prompt_pack("x", text)
    assert "not_a_section" not in pack.sections
    assert "you build" in pack.section("role") and "more role" in pack.section("role")


def test_every_contract_pack_mentions_its_finalizer() -> None:
    # coherence: a pack assigned to a contract must instruct THAT contract's finalizer
    creg = BuildContractRegistry.default()
    preg = PromptPackRegistry()
    for kind in ContractKind:
        c = creg.get(kind)
        assert c is not None
        if c.prompt_pack is None:
            continue
        pack = preg.require(c.prompt_pack)
        assert c.verify.finalizer in pack.raw, (
            f"{kind.value}: pack {c.prompt_pack} does not mention finalizer {c.verify.finalizer}"
        )


def test_packs_loadable_via_importlib_resources() -> None:
    # production-safe accessor (works from an installed wheel, not just src)
    from importlib.resources import files
    res = files("disco.core.workflows.prompt_packs") / "build_static_site.md"
    assert res.is_file()
    assert "## Role" in res.read_text(encoding="utf-8")
