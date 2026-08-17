"""Progressive-disclosure selection, guided authoring and owned-byte ejection.

The properties held here are the remaining package-acceptance bullets: selection
uses progressive disclosure and only user-supplied direct references; installed
packs do not enter prompts ambiently; no live pointer remains after ejection;
and a persisted reference retains a compatibility/ejection path after a registry
entry is disabled.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    BuiltinInputCategory,
    BuiltinInputMount,
    ComponentId,
    TrustLevel,
)
from disco.core.build_platform.latent_effects import ValueSurface
from disco.core.build_platform.library_catalog import (
    AuthoringRejected,
    InstalledLibraryCatalog,
    LibraryCatalogError,
    author_document,
)
from disco.core.build_platform.library_composition import InstalledSource, content_digest
from disco.core.build_platform.reference_packs import REFERENCE_PACK_VALUE_SURFACES

PAYLOAD = "the owned bytes of one installed source"


def _source(
    name: str, *, payload: str = PAYLOAD, provides: tuple[str, ...] = ()
) -> InstalledSource:
    return InstalledSource(
        id=ComponentId(namespace="disco_libraryrecipe", name=name, version="1.0.0"),
        category=BuiltinInputCategory.LIBRARY_RECIPE,
        mount=BuiltinInputMount.CONSTRUCTION,
        trust=TrustLevel.TRUSTED_LOCAL,
        provides=provides,
        provenance=f"test:{name}",
        content_digest=content_digest(payload),
    )


def _catalog(*names: str) -> tuple[InstalledLibraryCatalog, list[InstalledSource]]:
    catalog = InstalledLibraryCatalog()
    sources = [_source(name) for name in names]
    for source in sources:
        catalog.install(source, PAYLOAD)
    return catalog, sources


# ---------------------------------------------------------------------------
# Installation takes ownership of exact bytes
# ---------------------------------------------------------------------------


def test_install_rejects_a_payload_that_does_not_match_its_declared_digest() -> None:
    catalog = InstalledLibraryCatalog()
    with pytest.raises(LibraryCatalogError, match="does not match declared"):
        catalog.install(_source("acme"), "different bytes")
    assert catalog.installed_ids() == ()


def test_duplicate_installation_fails_closed() -> None:
    catalog, _ = _catalog("acme")
    with pytest.raises(LibraryCatalogError, match="already installed"):
        catalog.install(_source("acme"), PAYLOAD)


# ---------------------------------------------------------------------------
# Progressive disclosure — summaries carry no content
# ---------------------------------------------------------------------------


def test_disclosure_returns_summaries_without_any_value_surface() -> None:
    catalog, _ = _catalog("acme")
    summary = catalog.disclose()[0]
    assert summary.content_digest.startswith("sha256:")
    # A summary is enough to choose and not enough to inject: no field of it
    # carries the payload bytes.
    assert PAYLOAD not in summary.model_dump_json()


def test_disclosure_is_deterministically_ordered() -> None:
    catalog, _ = _catalog("zeta", "alpha", "mid")
    assert [item.id.name for item in catalog.disclose()] == ["alpha", "mid", "zeta"]


def test_content_requires_a_direct_reference_to_one_identity() -> None:
    catalog, sources = _catalog("acme")
    assert catalog.payload_of(sources[0].id) == PAYLOAD


def test_there_is_no_select_everything_path() -> None:
    # Ambient inclusion is prevented by the absence of the capability, not by a
    # caller remembering not to use it. An empty request yields nothing.
    catalog, _ = _catalog("acme", "other")
    assert catalog.select(()) == ()
    assert not any(
        name for name in dir(catalog) if name in {"select_all", "all_sources", "everything"}
    )


def test_selection_returns_only_the_user_supplied_direct_references() -> None:
    catalog, sources = _catalog("acme", "other", "third")
    selected = catalog.select((sources[0].id, sources[2].id))
    assert [item.id.name for item in selected] == ["acme", "third"]


def test_selection_is_deterministic_under_request_order() -> None:
    catalog, sources = _catalog("acme", "other")
    forward = catalog.select((sources[0].id, sources[1].id))
    reverse = catalog.select((sources[1].id, sources[0].id))
    assert forward == reverse


def test_selecting_an_uninstalled_reference_fails_closed() -> None:
    catalog, _ = _catalog("acme")
    with pytest.raises(LibraryCatalogError, match="is not installed"):
        catalog.select(
            (ComponentId(namespace="disco_libraryrecipe", name="ghost", version="1.0.0"),)
        )


# ---------------------------------------------------------------------------
# Guided authoring — a raw document is classified BEFORE typed coercion
# ---------------------------------------------------------------------------


def test_authoring_accepts_a_real_user_supplied_direct_reference() -> None:
    document = {"label": "Acme Landing", "docs": "See https://docs.example.com/api for usage."}
    assert author_document(document, REFERENCE_PACK_VALUE_SURFACES) == document


def test_authoring_rejects_a_scalar_overload_with_the_named_rule() -> None:
    # POS-006 (R4). A typed record cannot represent this case at all, so the
    # authoring path is the only place the rule can actually fire -- which is
    # exactly why guided authoring classifies before it coerces.
    with pytest.raises(AuthoringRejected, match=r"parameter_smuggling \(R4\)"):
        author_document({"version": ["1.0.0", "2.0.0"]}, REFERENCE_PACK_VALUE_SURFACES)


def test_authoring_rejects_an_undeclared_field() -> None:
    with pytest.raises(AuthoringRejected, match=r"parameter_smuggling \(R4\)"):
        author_document({"undeclared": "anything"}, REFERENCE_PACK_VALUE_SURFACES)


def test_authoring_names_the_offending_field() -> None:
    with pytest.raises(AuthoringRejected, match="callback_url"):
        author_document(
            {"callback_url": "https://evil.example.com/collect?key=1"},
            REFERENCE_PACK_VALUE_SURFACES,
        )


def test_authoring_respects_the_declared_surface_of_each_field() -> None:
    # The same bytes are legitimate in a documentation surface and latent in a
    # plain data surface. Nothing is inferred from the field's name.
    link = "https://docs.example.com/api"
    author_document({"docs": link}, REFERENCE_PACK_VALUE_SURFACES)
    with pytest.raises(AuthoringRejected, match=r"network_egress \(R9\)"):
        author_document({"callback_url": link}, REFERENCE_PACK_VALUE_SURFACES)


def test_authoring_is_bounded_by_the_declared_surfaces_it_is_given() -> None:
    surfaces = {"only": ValueSurface.DATA}
    assert author_document({"only": "plain"}, surfaces) == {"only": "plain"}


# ---------------------------------------------------------------------------
# Disable — persisted references keep a compatibility/ejection path
# ---------------------------------------------------------------------------


def test_disabled_entry_is_refused_for_new_selection() -> None:
    catalog, sources = _catalog("acme")
    catalog.disable(sources[0].id)
    with pytest.raises(LibraryCatalogError, match="disabled and cannot be newly selected"):
        catalog.select((sources[0].id,))


def test_disabled_entry_still_resolves_for_an_already_persisted_reference() -> None:
    catalog, sources = _catalog("acme")
    catalog.disable(sources[0].id)
    resolution = catalog.resolve_persisted(sources[0].id)
    assert resolution.enabled is False
    assert resolution.ejectable is True
    assert resolution.content_digest == content_digest(PAYLOAD)
    assert "compatibility" in resolution.detail


def test_disable_is_reported_in_disclosure() -> None:
    catalog, sources = _catalog("acme")
    catalog.disable(sources[0].id)
    assert catalog.disclose()[0].enabled is False


def test_disabling_an_uninstalled_reference_fails_closed() -> None:
    catalog, _ = _catalog("acme")
    with pytest.raises(LibraryCatalogError, match="is not installed"):
        catalog.disable(ComponentId(namespace="disco_libraryrecipe", name="ghost", version="1.0.0"))


# ---------------------------------------------------------------------------
# Owned-byte ejection — no live pointer remains
# ---------------------------------------------------------------------------


def test_ejection_returns_the_owned_bytes_with_their_exact_digest() -> None:
    catalog, sources = _catalog("acme")
    ejected = catalog.eject(sources[0].id)
    assert ejected.payload == PAYLOAD
    assert ejected.content_digest == content_digest(PAYLOAD)
    assert ejected.byte_count == len(PAYLOAD.encode("utf-8"))


def test_no_live_pointer_remains_after_ejection() -> None:
    # The acceptance property, stated so a test can falsify it: every index is
    # enumerated, and all of them must be empty of this identity.
    catalog, sources = _catalog("acme")
    assert catalog.live_pointers(sources[0].id) == ("sources", "payloads", "enabled")
    catalog.eject(sources[0].id)
    assert catalog.live_pointers(sources[0].id) == ()
    assert catalog.installed_ids() == ()
    assert catalog.disclose() == ()


def test_ejected_reference_has_no_remaining_compatibility_path() -> None:
    # Disabling retains the descriptor; ejection removes it. The two operations
    # are deliberately different strengths.
    catalog, sources = _catalog("acme")
    catalog.eject(sources[0].id)
    with pytest.raises(LibraryCatalogError, match="no compatibility path remains"):
        catalog.resolve_persisted(sources[0].id)


def test_ejected_reference_can_no_longer_be_selected_or_read() -> None:
    catalog, sources = _catalog("acme")
    catalog.eject(sources[0].id)
    with pytest.raises(LibraryCatalogError, match="is not installed"):
        catalog.select((sources[0].id,))
    with pytest.raises(LibraryCatalogError, match="is not installed"):
        catalog.payload_of(sources[0].id)


def test_ejection_leaves_sibling_sources_untouched() -> None:
    catalog, sources = _catalog("acme", "other")
    catalog.eject(sources[0].id)
    assert catalog.installed_ids() == ("disco_libraryrecipe.other@1.0.0",)
    assert catalog.payload_of(sources[1].id) == PAYLOAD


def test_a_disabled_entry_is_still_ejectable() -> None:
    catalog, sources = _catalog("acme")
    catalog.disable(sources[0].id)
    assert catalog.resolve_persisted(sources[0].id).ejectable is True
    catalog.eject(sources[0].id)
    assert catalog.live_pointers(sources[0].id) == ()


def test_ejecting_an_uninstalled_reference_fails_closed() -> None:
    catalog, _ = _catalog("acme")
    with pytest.raises(LibraryCatalogError, match="is not installed"):
        catalog.eject(ComponentId(namespace="disco_libraryrecipe", name="ghost", version="1.0.0"))


def test_reinstallation_after_ejection_is_permitted() -> None:
    # Ejection is complete, not tombstoned: the identity is free afterwards.
    catalog, sources = _catalog("acme")
    catalog.eject(sources[0].id)
    catalog.install(_source("acme"), PAYLOAD)
    assert catalog.installed_ids() == ("disco_libraryrecipe.acme@1.0.0",)
