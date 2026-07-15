"""WPP-3 tests: skill mount policy — skills mount only via contract, no global soup."""

from __future__ import annotations

from disco.core.contract import BuildContractRegistry, ContractKind
from disco.core.workflows import is_skill_mountable, resolve_mounted_skills


def test_contract_without_skills_mounts_nothing() -> None:
    c = BuildContractRegistry.default().get(ContractKind.STATIC_SITE)
    assert c is not None
    assert resolve_mounted_skills(c) == frozenset()


def test_appkit_mounts_only_its_declared_skills() -> None:
    c = BuildContractRegistry.default().get(ContractKind.APPKIT_LEADGEN)
    assert c is not None
    assert resolve_mounted_skills(c) == frozenset(
        {"appkit.leadgen", "cloudflare_export", "design_recipe"}
    )


def test_no_global_skill_soup() -> None:
    # a skill declared by one contract is NOT mountable under another
    reg = BuildContractRegistry.default()
    static = reg.get(ContractKind.STATIC_SITE)
    appkit = reg.get(ContractKind.APPKIT_LEADGEN)
    assert static is not None and appkit is not None
    assert is_skill_mountable("cloudflare_export", appkit) is True
    assert is_skill_mountable("cloudflare_export", static) is False  # not declared → not mountable


def test_undeclared_skill_is_not_mountable() -> None:
    c = BuildContractRegistry.default().get(ContractKind.APPKIT_LEADGEN)
    assert c is not None
    assert is_skill_mountable("some_random_skill", c) is False


def test_base_skills_union_but_default_empty() -> None:
    c = BuildContractRegistry.default().get(ContractKind.DECK)
    assert c is not None
    # default: only the contract's (deck declares none)
    assert resolve_mounted_skills(c) == frozenset()
    # an explicit host base set is unioned in (never inferred)
    assert resolve_mounted_skills(c, base_skills=("safety_always_on",)) == frozenset(
        {"safety_always_on"}
    )
    assert is_skill_mountable("safety_always_on", c, base_skills=("safety_always_on",)) is True


def test_skills_field_roundtrips_on_contract() -> None:
    from disco.core.contract import BuildContract

    c = BuildContractRegistry.default().get(ContractKind.APPKIT_LEADGEN)
    assert c is not None
    assert BuildContract.model_validate(c.model_dump(mode="json")) == c
    assert "appkit.leadgen" in c.skills


def test_only_appkit_builtin_declares_skills() -> None:
    # registry-level invariant: NO built-in mounts skills except where explicitly
    # declared — guards against a future contract silently adding a global skill.
    reg = BuildContractRegistry.default()
    declared = {kind: reg.get(kind).skills for kind in ContractKind if reg.get(kind) is not None}  # type: ignore[union-attr]
    assert declared[ContractKind.APPKIT_LEADGEN]  # appkit declares some
    for kind, skills in declared.items():
        if kind is ContractKind.APPKIT_LEADGEN:
            continue
        assert skills == (), f"{kind.value} unexpectedly declares skills {skills}"
