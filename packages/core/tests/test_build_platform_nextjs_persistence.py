"""Focused persisted-reference compatibility and unregistration tests (15-P1).

These exercise the production trusted built-in registry and resolver path:
`build_builtin_registry()` plus `resolve_build_composition(...)`, alongside the
registry's typed persisted-reference descriptor/migration seam.  They observe
the real registered/resolved behavior — never a serialized fixture or prose-only
descriptor.

The corrective 15-P1 slice fixes two independently reproduced defects and proves
them:

1. A persisted mirror must preserve the registry's exact-ID keyspace.  A
   persisted ID that collides with any existing profile or component ID must
   fail closed with no overwrite and no partial descriptor/catalog state.
   Duplicate and unsupported descriptor validation remain atomic, and rollback
   must not reclaim or duplicate an already-claimed identity.

2. A real migration-to-composition proof: register a typed descriptor, resolve
   its persisted ID through the registry migration method, then pass the
   resolved live profile ID through `resolve_build_composition(...)` and assert
   the static and server target/exporter/self-host identities.  The optional
   Vercel connector remains separately selectable and never attaches through
   migration.  The proof includes rollback and negative controls.

No source, build command, token, secret, credential, deployment effect, or
authenticated provider result is introduced by this slice.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    CapabilityLayer,
    ComponentId,
    PolicyLayer,
    PromptContextInputs,
    ResolutionInputs,
    build_builtin_registry,
    resolve_build_composition,
)
from disco.core.build_platform.registry import (
    PersistedProfileDescriptor,
    PersistedReferenceError,
    RegistryError,
    UnregistrationError,
)
from disco.core.build_platform.resolver import ResolutionError
from disco.core.targets.nextjs_connectors import (
    NEXTJS_SELF_HOST_CONNECTOR_ID,
    NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID,
)
from disco.core.targets.nextjs_server import (
    NEXTJS_SERVER_EXPORTER_ID,
    NEXTJS_SERVER_PROFILE_ID,
    NEXTJS_SERVER_TARGET_ID,
)
from disco.core.targets.nextjs_static import (
    NEXTJS_STATIC_EXPORTER_ID,
    NEXTJS_STATIC_PROFILE_ID,
    NEXTJS_STATIC_TARGET_ID,
)

_STATIC = NEXTJS_STATIC_PROFILE_ID
_SERVER = NEXTJS_SERVER_PROFILE_ID
_STATIC_TARGET = NEXTJS_STATIC_TARGET_ID
_SERVER_TARGET = NEXTJS_SERVER_TARGET_ID
_STATIC_EXPORTER = NEXTJS_STATIC_EXPORTER_ID
_SERVER_EXPORTER = NEXTJS_SERVER_EXPORTER_ID
_SELF_HOST = NEXTJS_SELF_HOST_CONNECTOR_ID
_VERCEL = NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID

# Legacy persisted references that a stored consumer still holds, distinct from
# the live built-in profile IDs they resolve through the descriptor/migration
# path.
_PERSISTED_STATIC = ComponentId(namespace="disco", name="legacy_nextjs_static", version="1")
_PERSISTED_SERVER = ComponentId(namespace="disco", name="legacy_nextjs_server", version="1")

_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})


def _capabilities(source: str) -> CapabilityLayer:
    return CapabilityLayer(source=source, allowed=_CAPABILITIES)


def _inputs(profile_id: ComponentId, goal: str) -> ResolutionInputs:
    return ResolutionInputs(
        profile=profile_id,
        goal=goal,
        platform_capabilities=_capabilities("platform"),
        user_capabilities=_capabilities("user"),
        host_capabilities=_capabilities("host"),
        platform_policy=PolicyLayer(source="platform"),
        user_policy=PolicyLayer(source="user"),
        prompt_context=PromptContextInputs(),
    )


def _static_descriptor() -> PersistedProfileDescriptor:
    return PersistedProfileDescriptor(
        persisted_profile=_PERSISTED_STATIC,
        profile=_STATIC,
        target=_STATIC_TARGET,
        exporter=_STATIC_EXPORTER,
        connector=_SELF_HOST,
    )


def _server_descriptor() -> PersistedProfileDescriptor:
    return PersistedProfileDescriptor(
        persisted_profile=_PERSISTED_SERVER,
        profile=_SERVER,
        target=_SERVER_TARGET,
        exporter=_SERVER_EXPORTER,
        connector=_SELF_HOST,
    )


def _register_persisted(registry, descriptor: PersistedProfileDescriptor) -> None:
    registry.register_persisted_reference(descriptor)


def test_bare_unregister_of_live_profile_blocks_resolution_without_fallback() -> None:
    registry = build_builtin_registry()
    registry.unregister_profile(_STATIC)
    assert registry.profiles.get(_STATIC) is None
    with pytest.raises(ResolutionError, match="not registered"):
        resolve_build_composition(registry, _inputs(_STATIC, "probe"))


def test_bare_unregister_of_an_unregistered_profile_fails_closed() -> None:
    registry = build_builtin_registry()
    registry.unregister_profile(_STATIC)
    with pytest.raises(UnregistrationError, match="is not registered"):
        registry.unregister_profile(_STATIC)


def test_unregister_only_removes_the_named_live_profile() -> None:
    registry = build_builtin_registry()
    registry.unregister_profile(_STATIC)
    assert registry.profiles.get(_STATIC) is None
    assert registry.profiles.get(_SERVER) is not None


def test_persisted_id_resolves_through_descriptor_to_live_profile() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    resolved = registry.resolve_persisted_reference(_PERSISTED_STATIC)
    assert resolved.id == _STATIC
    assert resolved.target == _STATIC_TARGET
    assert resolved.exporter == _STATIC_EXPORTER
    assert resolved.connector == _SELF_HOST


def test_persisted_descriptor_preserves_all_original_identities() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    _register_persisted(registry, _server_descriptor())
    static = registry.resolve_persisted_reference(_PERSISTED_STATIC)
    server = registry.resolve_persisted_reference(_PERSISTED_SERVER)
    assert static.id == _STATIC
    assert server.id == _SERVER
    assert static.target == _STATIC_TARGET
    assert server.target == _SERVER_TARGET
    assert static.exporter == _STATIC_EXPORTER
    assert server.exporter == _SERVER_EXPORTER
    assert static.connector == _SELF_HOST
    assert server.connector == _SELF_HOST


def test_bare_unregister_is_rejected_after_persisted_reference() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    with pytest.raises(UnregistrationError, match="persisted"):
        registry.unregister_profile(_PERSISTED_STATIC)


def test_persisted_id_still_resolves_after_bare_unregister_is_rejected() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    with pytest.raises(UnregistrationError, match="persisted"):
        registry.unregister_profile(_PERSISTED_STATIC)
    assert registry.profiles.get(_PERSISTED_STATIC) is not None
    assert registry.resolve_persisted_reference(_PERSISTED_STATIC).id == _STATIC


def test_persisted_live_profile_still_resolves_normally_after_descriptor() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    composition = resolve_build_composition(registry, _inputs(_STATIC, "probe"))
    assert composition.profile.id == _STATIC
    assert composition.connector == _SELF_HOST
    assert composition.blocked_operations == ()


def test_persisted_mirror_resolves_as_a_normal_registered_profile() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    mirror = registry.profiles.get(_PERSISTED_STATIC)
    assert mirror is not None
    assert mirror.id == _PERSISTED_STATIC
    assert mirror.target == _STATIC_TARGET
    assert mirror.connector == _SELF_HOST
    assert registry.resolve_persisted_reference(_PERSISTED_STATIC).id == _STATIC


def test_unknown_persisted_version_fails_closed_atomically() -> None:
    registry = build_builtin_registry()
    descriptor = _static_descriptor()
    unknown_version = descriptor.model_copy(update={"descriptor_version": 2})
    with pytest.raises(PersistedReferenceError, match="unsupported persisted descriptor version"):
        registry.register_persisted_reference(unknown_version)
    assert not registry.persisted.has(_PERSISTED_STATIC)
    # No partial mirror and no keyspace claim survives a failed registration.
    assert registry.profiles.get(_PERSISTED_STATIC) is None


def test_removed_persisted_reference_fails_closed_without_fallback() -> None:
    registry = build_builtin_registry()
    with pytest.raises(PersistedReferenceError, match="no persisted reference"):
        registry.resolve_persisted_reference(_PERSISTED_STATIC)


def test_persisted_reference_to_removed_live_profile_fails_closed() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    registry.profiles._profiles.pop(_STATIC.canonical)
    with pytest.raises(PersistedReferenceError, match="missing"):
        registry.resolve_persisted_reference(_PERSISTED_STATIC)


def test_duplicate_persisted_reference_is_rejected() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    with pytest.raises(RegistryError, match="already exists"):
        registry.register_persisted_reference(_static_descriptor())


def test_wrong_identity_descriptor_is_rejected_atomically() -> None:
    registry = build_builtin_registry()
    wrong = _static_descriptor().model_copy(update={"target": _SERVER_TARGET})
    with pytest.raises(PersistedReferenceError, match="target"):
        registry.register_persisted_reference(wrong)
    # No partial descriptor, mirror, or keyspace claim survives the failure.
    assert not registry.persisted.has(_PERSISTED_STATIC)
    assert registry.profiles.get(_PERSISTED_STATIC) is None


def test_persisted_reference_must_differ_from_live_profile() -> None:
    registry = build_builtin_registry()
    same = _static_descriptor().model_copy(update={"persisted_profile": _STATIC})
    with pytest.raises(PersistedReferenceError, match="differ"):
        registry.register_persisted_reference(same)
    assert not registry.persisted.has(_PERSISTED_STATIC)


def test_persisted_profile_registers_only_when_live_profile_exists() -> None:
    registry = build_builtin_registry()
    missing = ComponentId(namespace="disco", name="nextjs_missing", version="1")
    descriptor = _static_descriptor().model_copy(update={"profile": missing})
    with pytest.raises(PersistedReferenceError, match="missing live profile"):
        registry.register_persisted_reference(descriptor)
    assert not registry.persisted.has(_PERSISTED_STATIC)
    assert registry.profiles.get(_PERSISTED_STATIC) is None


def test_persisted_id_colliding_with_existing_profile_fails_closed_no_overwrite() -> None:
    # The freeform profile is a pre-existing registered profile.  A persisted
    # descriptor whose persisted ID claims that exact-ID keyspace must fail
    # closed without overwriting the existing catalog entry.
    registry = build_builtin_registry()
    original = registry.profiles.get(FREEFORM_PROFILE_ID)
    assert original is not None
    collision = _static_descriptor().model_copy(
        update={"persisted_profile": FREEFORM_PROFILE_ID}
    )
    with pytest.raises(RegistryError, match="duplicate"):
        registry.register_persisted_reference(collision)
    # The pre-existing profile is byte-identical after the failed claim.
    assert registry.profiles.get(FREEFORM_PROFILE_ID) == original
    # No partial descriptor or mirror was recorded.
    assert not registry.persisted.has(FREEFORM_PROFILE_ID)
    # The freeform profile still resolves normally.
    composition = resolve_build_composition(registry, _inputs(FREEFORM_PROFILE_ID, "probe"))
    assert composition.profile.id == FREEFORM_PROFILE_ID


def test_persisted_id_colliding_with_existing_component_id_fails_closed() -> None:
    # The static target is a registered component.  A persisted ID claiming the
    # same exact-ID keyspace (profile/component share one namespace) must fail
    # closed rather than overwrite the component spec.
    registry = build_builtin_registry()
    original_spec = registry.components.spec(_STATIC_TARGET)
    assert original_spec is not None
    collision = _static_descriptor().model_copy(update={"persisted_profile": _STATIC_TARGET})
    with pytest.raises(RegistryError, match="duplicate"):
        registry.register_persisted_reference(collision)
    assert registry.components.spec(_STATIC_TARGET) == original_spec
    assert not registry.persisted.has(_STATIC_TARGET)
    assert registry.profiles.get(_STATIC_TARGET) is None


def test_rollback_restores_prior_descriptor_deterministically() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    registry.profiles._profiles.pop(_PERSISTED_STATIC.canonical)
    assert registry.profiles.get(_PERSISTED_STATIC) is None
    registry.rollback_persisted_reference(_PERSISTED_STATIC)
    restored = registry.resolve_persisted_reference(_PERSISTED_STATIC)
    assert restored.id == _STATIC
    assert restored.target == _STATIC_TARGET
    assert restored.exporter == _STATIC_EXPORTER
    assert restored.connector == _SELF_HOST
    assert registry.profiles.get(_PERSISTED_STATIC) is not None


def test_rollback_of_missing_descriptor_fails_closed() -> None:
    registry = build_builtin_registry()
    with pytest.raises(PersistedReferenceError, match="no persisted reference"):
        registry.rollback_persisted_reference(_PERSISTED_STATIC)


def test_rollback_does_not_silently_reinterpret_persisted_id() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    registry.profiles._profiles.pop(_PERSISTED_STATIC.canonical)
    registry.rollback_persisted_reference(_PERSISTED_STATIC)
    assert registry.persisted.get(_PERSISTED_STATIC) is not None
    resolved = registry.resolve_persisted_reference(_PERSISTED_STATIC)
    assert resolved.id == _STATIC
    assert resolved.target == _STATIC_TARGET
    mirror = registry.profiles.get(_PERSISTED_STATIC)
    assert mirror is not None and mirror.id == _PERSISTED_STATIC


def test_rollback_cannot_duplicate_an_already_claimed_identity() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    # If the mirror were already present, rollback must not duplicate/overwrite
    # the claimed identity: it fails closed rather than re-claiming the key.
    with pytest.raises(RegistryError, match="duplicate"):
        registry.rollback_persisted_reference(_PERSISTED_STATIC)
    # The mirror and descriptor remain intact.
    assert registry.profiles.get(_PERSISTED_STATIC) is not None
    assert registry.resolve_persisted_reference(_PERSISTED_STATIC).id == _STATIC


def test_migrated_static_id_composes_to_real_static_identities() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    # Resolve the persisted ID through the registry migration method, then pass
    # the resolved LIVE profile ID through the real resolver composition.
    live_profile_id = registry.resolve_persisted_reference(_PERSISTED_STATIC).id
    assert live_profile_id == _STATIC
    composition = resolve_build_composition(registry, _inputs(live_profile_id, "static"))
    assert composition.profile.id == _STATIC
    assert composition.profile.id.canonical == "disco.nextjs_static@1"
    assert composition.target == _STATIC_TARGET
    assert composition.target_plan.target == _STATIC_TARGET
    assert composition.exporter == _STATIC_EXPORTER
    assert composition.connector == _SELF_HOST
    assert composition.engine == FREEFORM_ENGINE_ID
    assert composition.blocked_operations == ()


def test_migrated_server_id_composes_to_real_server_identities() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _server_descriptor())
    live_profile_id = registry.resolve_persisted_reference(_PERSISTED_SERVER).id
    assert live_profile_id == _SERVER
    composition = resolve_build_composition(registry, _inputs(live_profile_id, "server"))
    assert composition.profile.id == _SERVER
    assert composition.profile.id.canonical == "disco.nextjs_server@1"
    assert composition.target == _SERVER_TARGET
    assert composition.target_plan.target == _SERVER_TARGET
    assert composition.exporter == _SERVER_EXPORTER
    assert composition.connector == _SELF_HOST
    assert composition.blocked_operations == ()


def test_migrated_static_and_server_keep_distinct_target_exporter_identities() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    _register_persisted(registry, _server_descriptor())
    static = resolve_build_composition(
        registry, _inputs(registry.resolve_persisted_reference(_PERSISTED_STATIC).id, "static")
    )
    server = resolve_build_composition(
        registry, _inputs(registry.resolve_persisted_reference(_PERSISTED_SERVER).id, "server")
    )
    assert static.target == _STATIC_TARGET
    assert server.target == _SERVER_TARGET
    assert static.target != server.target
    assert static.exporter == _STATIC_EXPORTER
    assert server.exporter == _SERVER_EXPORTER
    assert static.exporter != server.exporter
    assert static.connector == _SELF_HOST
    assert server.connector == _SELF_HOST


def test_vercel_connector_stays_separately_selectable_and_never_attaches() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    resolved = registry.resolve_persisted_reference(_PERSISTED_STATIC)
    assert resolved.connector == _SELF_HOST
    assert resolved.connector != _VERCEL
    assert registry.components.connector(_VERCEL) is not None
    assert registry.profiles.get(_STATIC).connector == _SELF_HOST
    assert registry.profiles.get(_STATIC).connector != _VERCEL
    # The migrated static composition carries only the self-host connector; the
    # optional Vercel connector is never attached through migration.
    composition = resolve_build_composition(
        registry, _inputs(registry.resolve_persisted_reference(_PERSISTED_STATIC).id, "probe")
    )
    assert composition.connector == _SELF_HOST
    assert composition.connector != _VERCEL


def test_persisted_reference_preserves_static_server_identity_parity() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    _register_persisted(registry, _server_descriptor())
    static = registry.resolve_persisted_reference(_PERSISTED_STATIC)
    server = registry.resolve_persisted_reference(_PERSISTED_SERVER)
    assert static.target != server.target
    assert static.id != server.id
    assert static.target == _STATIC_TARGET
    assert server.target == _SERVER_TARGET


def test_migrated_identity_drift_is_rejected() -> None:
    registry = build_builtin_registry()
    _register_persisted(registry, _static_descriptor())
    # Simulate a live-profile target drift after the descriptor was recorded;
    # resolution must fail closed rather than silently reinterpret.
    drifted = registry.profiles.get(_STATIC).model_copy(update={"target": _SERVER_TARGET})
    registry.profiles._profiles[_STATIC.canonical] = drifted
    with pytest.raises(PersistedReferenceError, match="identity drift"):
        registry.resolve_persisted_reference(_PERSISTED_STATIC)
