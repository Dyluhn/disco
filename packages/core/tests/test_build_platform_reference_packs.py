"""Focused tests for the Reference Pack schema and registry entry (16-S1).

These exercise the production ``reference_packs`` module directly — the frozen
versioned ``ReferencePack`` record and the deterministic, fail-closed
``ReferencePackRegistry`` — never a serialized fixture or prose-only descriptor.
The slice is restricted to the Reference Pack schema and its bounded registry
entry: no selection, prompt injection, direct-reference workflow,
persistence/ejection, provider, or runtime-effect channel is added.

Required resolved behavior and controls covered:

- A versioned Reference Pack record carries engine, profile, target, and
  required-capability scope explicitly (no scope is implicit in a label).
- Registration and exact resolution are deterministic and fail closed for
  unsupported schema versions, duplicate identities, and engine/profile/target/
  capability scope mismatches.
- Distinct concepts use distinct typed IDs; a Reference Pack never reuses an
  AppKit, Freeform, target, or persisted-profile ID.
- Positive controls resolve the production registry path; mutation/negative
  controls prove version rejection, wrong engine/profile/target scope, denied
  capability widening, and duplicate identity.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    APPKIT_ENGINE_ID,
    APPKIT_PROFILE_ID,
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    WEB_TARGET_ID,
    ComponentId,
)
from disco.core.build_platform.reference_packs import (
    SUPPORTED_REFERENCE_PACK_SCHEMA_VERSION,
    ReferencePack,
    ReferencePackError,
    ReferencePackRegistry,
)


def _pack_id(name: str) -> ComponentId:
    return ComponentId(namespace="disco_refpack", name=name, version="1")


def _pack(
    *,
    name: str = "demo",
    schema_version: int = SUPPORTED_REFERENCE_PACK_SCHEMA_VERSION,
    engine: ComponentId = FREEFORM_ENGINE_ID,
    profile: ComponentId = FREEFORM_PROFILE_ID,
    target: ComponentId = WEB_TARGET_ID,
    required_capabilities: frozenset[str] = frozenset(),
) -> ReferencePack:
    return ReferencePack(
        id=_pack_id(name),
        schema_version=schema_version,
        engine=engine,
        profile=profile,
        target=target,
        required_capabilities=required_capabilities,
    )


# ---------------------------------------------------------------------------
# Positive controls — resolved production behavior
# ---------------------------------------------------------------------------


def test_resolved_record_carries_explicit_scope() -> None:
    pack = _pack(
        engine=FREEFORM_ENGINE_ID,
        profile=FREEFORM_PROFILE_ID,
        target=WEB_TARGET_ID,
        required_capabilities=frozenset({"workspace.read"}),
    )
    registry = ReferencePackRegistry()
    registry.register(pack)
    resolved = registry.resolve(pack.id)
    assert resolved.schema_version == SUPPORTED_REFERENCE_PACK_SCHEMA_VERSION
    # Scope is explicit on the resolved record, never implicit in a label.
    assert resolved.engine == FREEFORM_ENGINE_ID
    assert resolved.profile == FREEFORM_PROFILE_ID
    assert resolved.target == WEB_TARGET_ID
    assert resolved.required_capabilities == frozenset({"workspace.read"})


def test_appkit_scoped_record_resolves_with_distinct_scope() -> None:
    pack = _pack(
        name="appkit_demo",
        engine=APPKIT_ENGINE_ID,
        profile=APPKIT_PROFILE_ID,
        target=WEB_TARGET_ID,
    )
    registry = ReferencePackRegistry()
    registry.register(pack)
    resolved = registry.resolve(pack.id)
    assert resolved.engine == APPKIT_ENGINE_ID
    assert resolved.profile == APPKIT_PROFILE_ID
    assert resolved.target == WEB_TARGET_ID


def test_registration_and_resolution_are_deterministic() -> None:
    first = ReferencePackRegistry()
    second = ReferencePackRegistry()
    pack = _pack()
    first.register(pack)
    second.register(pack)
    assert first.resolve(pack.id) == second.resolve(pack.id)
    assert first.ids() == second.ids()
    assert first.ids() == (pack.id.canonical,)


def test_ids_are_sorted_deterministically() -> None:
    registry = ReferencePackRegistry()
    registry.register(_pack(name="b"))
    registry.register(_pack(name="a"))
    assert registry.ids() == (
        "disco_refpack.a@1",
        "disco_refpack.b@1",
    )


# ---------------------------------------------------------------------------
# Unsupported schema version
# ---------------------------------------------------------------------------


def test_unsupported_schema_version_fails_closed() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="unsupported reference pack schema version"):
        registry.register(_pack(schema_version=2))


def test_unsupported_schema_version_does_not_register() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError):
        registry.register(_pack(name="never", schema_version=99))
    with pytest.raises(ReferencePackError, match="is not registered"):
        registry.resolve(_pack_id("never"))


# ---------------------------------------------------------------------------
# Duplicate identity
# ---------------------------------------------------------------------------


def test_duplicate_identity_fails_closed() -> None:
    registry = ReferencePackRegistry()
    registry.register(_pack())
    with pytest.raises(ReferencePackError, match="duplicate reference pack id"):
        registry.register(_pack())


# ---------------------------------------------------------------------------
# Wrong engine/profile/target scope
# ---------------------------------------------------------------------------


def test_wrong_engine_profile_scope_fails_closed() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="engine/profile scope mismatch"):
        registry.register(
            _pack(name="mismatch", engine=APPKIT_ENGINE_ID, profile=FREEFORM_PROFILE_ID)
        )


def test_unknown_engine_fails_closed() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="not a known construction engine"):
        registry.register(
            _pack(
                name="bad_engine",
                engine=ComponentId(namespace="disco", name="unknown_engine", version="1"),
            )
        )


def test_unknown_profile_fails_closed() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="not a known build profile"):
        registry.register(
            _pack(
                name="bad_profile",
                profile=ComponentId(namespace="disco", name="unknown_profile", version="1"),
            )
        )


def test_unknown_target_fails_closed() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="not a known target"):
        registry.register(
            _pack(
                name="bad_target",
                target=ComponentId(namespace="disco", name="unknown_target", version="1"),
            )
        )


# ---------------------------------------------------------------------------
# Denied capability widening
# ---------------------------------------------------------------------------


def test_capability_widening_fails_closed() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="capability widening denied"):
        registry.register(
            _pack(required_capabilities=frozenset({"host.elevated_admin"}))
        )


# ---------------------------------------------------------------------------
# Distinct typed identity — never an AppKit/Freeform/target/persisted ID
# ---------------------------------------------------------------------------


def test_foreign_engine_or_profile_or_target_id_is_rejected() -> None:
    registry = ReferencePackRegistry()
    for foreign in (
        APPKIT_ENGINE_ID,
        APPKIT_PROFILE_ID,
        FREEFORM_ENGINE_ID,
        FREEFORM_PROFILE_ID,
        WEB_TARGET_ID,
    ):
        # These IDs are not Reference Pack IDs and must be rejected.  They live
        # outside the disco_refpack namespace, so either the namespace guard or
        # the distinct-identity reuse guard may fire first; both fail closed.
        with pytest.raises(ReferencePackError, match="namespace|reuse"):
            registry.register(
                ReferencePack(
                    id=foreign,
                    engine=FREEFORM_ENGINE_ID,
                    profile=FREEFORM_PROFILE_ID,
                    target=WEB_TARGET_ID,
                )
            )


def test_foreign_namespace_id_is_rejected() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="disco_refpack namespace"):
        registry.register(
            ReferencePack(
                id=ComponentId(namespace="disco", name="demo_pack", version="1"),
                engine=FREEFORM_ENGINE_ID,
                profile=FREEFORM_PROFILE_ID,
                target=WEB_TARGET_ID,
            )
        )


# ---------------------------------------------------------------------------
# Mutation / negative controls — rule removal must fail
# ---------------------------------------------------------------------------


def test_mutation_unsupported_version_rejection_depends_on_the_check() -> None:
    registry = ReferencePackRegistry()
    # A version-2 record must be rejected; without the version check it would
    # register, so this negative control guards the rejection rule.
    with pytest.raises(ReferencePackError, match="unsupported"):
        registry.register(_pack(schema_version=2))
    assert registry.ids() == ()


def test_mutation_duplicate_rejection_depends_on_the_check() -> None:
    registry = ReferencePackRegistry()
    registry.register(_pack())
    # The second identical registration must fail; without the duplicate check
    # the registry would silently overwrite and the count would stay one.
    with pytest.raises(ReferencePackError, match="duplicate"):
        registry.register(_pack())
    assert registry.ids() == ("disco_refpack.demo@1",)
