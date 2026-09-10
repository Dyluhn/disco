"""Trust evaluation, capability intersection, precedence and provenance/BOM.

These are the cross-source questions no single record seam can answer. The
properties held here are the ones the package acceptance names: installed
material cannot widen host/profile/target policy, capability intersections are
deterministic, precedence is deterministic when several inputs supply the same
thing, and the resulting artifact records its exact inputs.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    BUILTIN_CAPABILITIES,
    BuiltinInputCategory,
    BuiltinInputMount,
    CapabilityLayer,
    ComponentId,
    TrustLevel,
)
from disco.core.build_platform.library_composition import (
    TRUST_CEILINGS,
    BillOfMaterialsEntry,
    InstalledSource,
    LibraryBillOfMaterials,
    TrustDenialReason,
    content_digest,
    evaluate_trust,
    intersect_capabilities,
    resolve_precedence,
)

HOST = CapabilityLayer(source="host", allowed=frozenset(BUILTIN_CAPABILITIES))
PROFILE = CapabilityLayer(source="profile", allowed=frozenset(BUILTIN_CAPABILITIES))


def _source(
    name: str,
    *,
    trust: TrustLevel = TrustLevel.TRUSTED_LOCAL,
    capabilities: frozenset[str] = frozenset(),
    provides: tuple[str, ...] = (),
    category: BuiltinInputCategory = BuiltinInputCategory.LIBRARY_RECIPE,
    mount: BuiltinInputMount | None = None,
    payload: str = "payload",
) -> InstalledSource:
    return InstalledSource(
        id=ComponentId(namespace="disco_libraryrecipe", name=name, version="1.0.0"),
        category=category,
        mount=mount or BuiltinInputMount.CONSTRUCTION,
        trust=trust,
        required_capabilities=capabilities,
        provides=provides,
        provenance=f"test:{name}",
        content_digest=content_digest(payload),
    )


# ---------------------------------------------------------------------------
# Trust-tier EVALUATION — the tier decides, not just the record's own ceiling
# ---------------------------------------------------------------------------


def test_trust_ceilings_form_a_monotone_ladder() -> None:
    # Each tier's ceiling is a subset of the tier above it. This is what makes
    # the intersection well behaved rather than a set of unrelated allowlists.
    host = TRUST_CEILINGS[TrustLevel.HOST_POLICY]
    local = TRUST_CEILINGS[TrustLevel.TRUSTED_LOCAL]
    portable = TRUST_CEILINGS[TrustLevel.UNTRUSTED_PORTABLE]
    assert portable < local < host
    assert host == frozenset(BUILTIN_CAPABILITIES)


def test_untrusted_source_is_denied_a_capability_inside_the_host_ceiling() -> None:
    # This is the gap the package closes. ``network.egress`` IS a built-in
    # capability, so the per-record widening check admits it; the trust tier is
    # what refuses it. Before trust was evaluated, this source was admitted.
    source = _source(
        "portable", trust=TrustLevel.UNTRUSTED_PORTABLE, capabilities=frozenset({"network.egress"})
    )
    evaluation = evaluate_trust((source,))
    assert evaluation.admitted == ()
    assert len(evaluation.denials) == 1
    denial = evaluation.denials[0]
    assert denial.reason is TrustDenialReason.CAPABILITY_ABOVE_TRUST_CEILING
    assert denial.capabilities == ("network.egress",)


def test_host_policy_source_reaches_the_same_capability() -> None:
    source = _source(
        "hosted", trust=TrustLevel.HOST_POLICY, capabilities=frozenset({"network.egress"})
    )
    assert evaluate_trust((source,)).admitted_ids == ("disco_libraryrecipe.hosted@1.0.0",)


def test_capability_outside_the_host_ceiling_is_a_distinct_named_denial() -> None:
    # A capability that is not a capability at all is a different finding from a
    # real capability the tier cannot reach; conflating them is what made trust
    # look enforced when it was only stored.
    source = _source("bogus", capabilities=frozenset({"kernel.debug"}))
    denial = evaluate_trust((source,)).denials[0]
    assert denial.reason is TrustDenialReason.CAPABILITY_ABOVE_HOST_CEILING
    assert denial.capabilities == ("kernel.debug",)


def test_category_and_mount_must_agree() -> None:
    source = _source(
        "wrong_mount",
        category=BuiltinInputCategory.LIBRARY_RECIPE,
        mount=BuiltinInputMount.SCAFFOLD,
    )
    denial = evaluate_trust((source,)).denials[0]
    assert denial.reason is TrustDenialReason.MOUNT_DOES_NOT_MATCH_CATEGORY


def test_trust_evaluation_is_deterministic_under_input_order() -> None:
    a = _source("alpha", trust=TrustLevel.UNTRUSTED_PORTABLE)
    b = _source("beta", trust=TrustLevel.TRUSTED_LOCAL)
    assert evaluate_trust((a, b)).admitted_ids == evaluate_trust((b, a)).admitted_ids


# ---------------------------------------------------------------------------
# Capability INTERSECTION — deterministic, and it can only narrow
# ---------------------------------------------------------------------------


def test_intersection_with_no_sources_is_the_host_profile_intersection() -> None:
    policy = intersect_capabilities((), host=HOST, profile=PROFILE)
    assert policy.allowed == frozenset(BUILTIN_CAPABILITIES)


def test_installed_material_can_never_widen_host_policy() -> None:
    # The package-acceptance property, stated so a test can falsify it: adding
    # any source narrows or preserves the grant, and never widens it.
    base = intersect_capabilities((), host=HOST, profile=PROFILE)
    for trust in TrustLevel:
        widened = intersect_capabilities(
            (_source(f"s_{trust.value}", trust=trust),), host=HOST, profile=PROFILE
        )
        assert widened.allowed <= base.allowed


def test_untrusted_source_narrows_the_grant_to_read_only() -> None:
    policy = intersect_capabilities(
        (_source("portable", trust=TrustLevel.UNTRUSTED_PORTABLE),), host=HOST, profile=PROFILE
    )
    assert policy.allowed == frozenset({"workspace.read"})


def test_intersection_names_who_denied_each_capability() -> None:
    policy = intersect_capabilities(
        (_source("portable", trust=TrustLevel.UNTRUSTED_PORTABLE),), host=HOST, profile=PROFILE
    )
    denied = {denial.capability: denial.denied_by for denial in policy.denied}
    assert "network.egress" in denied
    assert denied["network.egress"] == ("library_recipe:disco_libraryrecipe.portable@1.0.0",)


def test_intersection_is_deterministic_under_input_order() -> None:
    a = _source("alpha", trust=TrustLevel.UNTRUSTED_PORTABLE)
    b = _source("beta", trust=TrustLevel.TRUSTED_LOCAL)
    forward = intersect_capabilities((a, b), host=HOST, profile=PROFILE)
    reverse = intersect_capabilities((b, a), host=HOST, profile=PROFILE)
    assert forward == reverse


def test_a_narrower_profile_layer_still_binds() -> None:
    profile = CapabilityLayer(source="profile", allowed=frozenset({"workspace.read"}))
    policy = intersect_capabilities(
        (_source("hosted", trust=TrustLevel.HOST_POLICY),), host=HOST, profile=profile
    )
    assert policy.allowed == frozenset({"workspace.read"})


# ---------------------------------------------------------------------------
# Deterministic PRECEDENCE when several inputs supply the same thing
# ---------------------------------------------------------------------------


def test_more_authoritative_trust_wins_a_contested_slot() -> None:
    low = _source("low", trust=TrustLevel.UNTRUSTED_PORTABLE, provides=("theme",))
    high = _source("high", trust=TrustLevel.TRUSTED_LOCAL, provides=("theme",))
    bom = resolve_precedence((low, high))
    assert [entry.source.name for entry in bom.included] == ["high"]
    superseded = bom.entry_for(low.id)
    assert superseded is not None
    assert superseded.included is False
    assert superseded.superseded_by is not None
    assert superseded.superseded_by.name == "high"


def test_precedence_does_not_depend_on_registration_order() -> None:
    # "Last one in wins" is exactly the non-determinism this resolves.
    low = _source("low", trust=TrustLevel.UNTRUSTED_PORTABLE, provides=("theme",))
    high = _source("high", trust=TrustLevel.TRUSTED_LOCAL, provides=("theme",))
    assert resolve_precedence((low, high)).digest == resolve_precedence((high, low)).digest


def test_mount_order_breaks_a_trust_tie() -> None:
    context = _source(
        "ctx",
        category=BuiltinInputCategory.REFERENCE_PACK,
        mount=BuiltinInputMount.CONTEXT,
        provides=("theme",),
    )
    construction = _source("cons", provides=("theme",))
    bom = resolve_precedence((construction, context))
    assert [entry.source.name for entry in bom.included] == ["ctx"]


def test_canonical_identity_is_the_total_tiebreak() -> None:
    first = _source("aaa", provides=("theme",))
    second = _source("bbb", provides=("theme",))
    bom = resolve_precedence((second, first))
    assert [entry.source.name for entry in bom.included] == ["aaa"]


def test_a_source_can_win_one_slot_and_lose_another() -> None:
    low = _source("low", trust=TrustLevel.UNTRUSTED_PORTABLE, provides=("theme", "layout"))
    high = _source("high", trust=TrustLevel.TRUSTED_LOCAL, provides=("theme",))
    bom = resolve_precedence((low, high))
    low_entry = bom.entry_for(low.id)
    assert low_entry is not None
    assert low_entry.included is True
    assert "wins layout" in low_entry.reason
    assert "superseded for theme" in low_entry.reason


def test_uncontested_sources_are_all_included() -> None:
    a = _source("a", provides=("theme",))
    b = _source("b", provides=("layout",))
    bom = resolve_precedence((a, b))
    assert len(bom.included) == 2


# ---------------------------------------------------------------------------
# Provenance / BOM — the artifact records its exact inputs
# ---------------------------------------------------------------------------


def test_bill_of_materials_records_every_considered_input() -> None:
    # Superseded candidates appear too: a bill that omitted them would not let a
    # reader reconstruct why the artifact contains what it contains.
    low = _source("low", trust=TrustLevel.UNTRUSTED_PORTABLE, provides=("theme",))
    high = _source("high", trust=TrustLevel.TRUSTED_LOCAL, provides=("theme",))
    bom = resolve_precedence((low, high))
    assert len(bom.entries) == 2
    assert len(bom.included) == 1


def test_bill_of_materials_entries_carry_exact_digest_and_provenance() -> None:
    source = _source("only", payload="exact-bytes")
    entry = resolve_precedence((source,)).entries[0]
    assert entry.content_digest == content_digest("exact-bytes")
    assert entry.content_digest.startswith("sha256:")
    assert entry.provenance == "test:only"
    assert entry.trust is TrustLevel.TRUSTED_LOCAL
    assert entry.mount is BuiltinInputMount.CONSTRUCTION


def test_bill_of_materials_digest_changes_when_an_input_changes() -> None:
    before = resolve_precedence((_source("only", payload="v1"),))
    after = resolve_precedence((_source("only", payload="v2"),))
    assert before.digest != after.digest


def test_bill_of_materials_ordinals_are_dense_and_ordered() -> None:
    sources = tuple(_source(name) for name in ("c", "a", "b"))
    bom = resolve_precedence(sources)
    assert [entry.ordinal for entry in bom.entries] == [0, 1, 2]
    assert [entry.source.name for entry in bom.entries] == ["a", "b", "c"]


def test_content_digest_is_stable_and_order_insensitive_for_mappings() -> None:
    assert content_digest({"a": 1, "b": 2}) == content_digest({"b": 2, "a": 1})
    assert content_digest("x") != content_digest("y")


def test_empty_bill_of_materials_is_representable() -> None:
    bom = LibraryBillOfMaterials()
    assert bom.entries == ()
    assert bom.included == ()
    assert bom.entry_for(ComponentId(namespace="disco", name="x", version="1")) is None


def test_bill_of_materials_entry_rejects_a_malformed_digest() -> None:
    with pytest.raises(ValueError):
        BillOfMaterialsEntry(
            ordinal=0,
            source=ComponentId(namespace="disco", name="x", version="1"),
            category=BuiltinInputCategory.LIBRARY_RECIPE,
            mount=BuiltinInputMount.CONSTRUCTION,
            trust=TrustLevel.TRUSTED_LOCAL,
            provenance="test",
            content_digest="not-a-digest",
            included=True,
        )
