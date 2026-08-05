"""Frozen, versioned Reference Pack scope/schema and its bounded registry entry.

A Reference Pack is a *distinct* input concept from a Starter Recipe, a Library
Recipe, an AppKit composition, a Freeform composition, a target, or a persisted
profile.  This module freezes only the Reference Pack record and a bounded,
deterministic registry for it.  It does not add user selection, prompt
injection, direct-reference workflow, persistence/ejection, provider, or
runtime-effect channels; those are later slices.

The record carries its engine, profile, target, and required-capability scope
explicitly — no scope is implicit in a label.  Registration and exact
resolution are deterministic and fail closed for unsupported schema versions,
duplicate identities, and engine/profile/target/capability scope mismatches.
"""

from __future__ import annotations

from typing import Literal

from .builtin_profiles import (
    APPKIT_ENGINE_ID,
    APPKIT_PROFILE_ID,
    BUILTIN_CAPABILITIES,
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    WEB_TARGET_ID,
)
from .contracts import ComponentId, FrozenModel

SUPPORTED_REFERENCE_PACK_SCHEMA_VERSION: Literal[1] = 1

_REFERENCE_PACK_NAMESPACE = "disco_refpack"

# The canonical construction engines and the target-owned built-in target.  A
# Reference Pack declares exactly one of each explicitly; Next.js targets use
# Freeform as their engine, but their identity stays target-owned and is not
# folded into this seam.
_KNOWN_ENGINES = frozenset({APPKIT_ENGINE_ID.canonical, FREEFORM_ENGINE_ID.canonical})
_KNOWN_TARGETS = frozenset({WEB_TARGET_ID.canonical})

# A known build profile's exact construction engine, so an engine/profile scope
# mismatch is detected deterministically rather than read from a label.
_ENGINE_FOR_PROFILE = {
    APPKIT_PROFILE_ID.canonical: APPKIT_ENGINE_ID.canonical,
    FREEFORM_PROFILE_ID.canonical: FREEFORM_ENGINE_ID.canonical,
}


class ReferencePackError(ValueError):
    """A Reference Pack registration or resolution failed closed."""


class ReferencePack(FrozenModel):
    """A versioned Reference Pack record with explicit composition scope.

    ``id`` is the pack's own distinct typed identity.  ``engine``, ``profile``,
    and ``target`` are the exact construction engine, build profile, and
    target-owned target this pack is scoped to, and ``required_capabilities``
    is its explicit capability requirement.  Every scope is explicit here; none
    is inferred from a label.
    """

    id: ComponentId
    schema_version: int = SUPPORTED_REFERENCE_PACK_SCHEMA_VERSION
    engine: ComponentId
    profile: ComponentId
    target: ComponentId
    required_capabilities: frozenset[str] = frozenset()


class ReferencePackRegistry:
    """Sole owner of frozen, host-scoped Reference Pack records.

    Registration is deterministic and fail-closed.  A record is admitted only
    when its schema version is supported, its identity is a distinct Reference
    Pack ID (never an AppKit, Freeform, target, or persisted-profile ID), its
    engine/profile/target scope is coherent and known, and its required
    capability scope does not widen the host ceiling.  Duplicate identities and
    any scope mismatch raise with a typed reason; exact resolution returns the
    record or fails closed.
    """

    def __init__(self) -> None:
        self._packs: dict[str, ReferencePack] = {}

    def register(self, pack: ReferencePack) -> None:
        if type(pack) is not ReferencePack:
            raise ReferencePackError("reference pack registration requires exact frozen data")
        if pack.schema_version != SUPPORTED_REFERENCE_PACK_SCHEMA_VERSION:
            raise ReferencePackError(
                f"unsupported reference pack schema version: {pack.schema_version}"
            )
        if pack.id.namespace != _REFERENCE_PACK_NAMESPACE:
            raise ReferencePackError(
                "reference pack id must use the distinct disco_refpack namespace"
            )
        # The namespace guard above already forbids every AppKit/Freeform/
        # target/persisted-profile identity (they live under disco or
        # model_role, never disco_refpack), so a Reference Pack can never reuse
        # one of those IDs as its own.
        key = pack.id.canonical
        if key in self._packs:
            raise ReferencePackError(f"duplicate reference pack id: {key}")
        self._validate_scope(pack)
        self._packs[key] = pack

    @staticmethod
    def _validate_scope(pack: ReferencePack) -> None:
        if pack.engine.canonical not in _KNOWN_ENGINES:
            raise ReferencePackError(
                f"reference pack engine {pack.engine.canonical} is not a known construction engine"
            )
        if pack.profile.canonical not in _ENGINE_FOR_PROFILE:
            raise ReferencePackError(
                f"reference pack profile {pack.profile.canonical} is not a known build profile"
            )
        if _ENGINE_FOR_PROFILE[pack.profile.canonical] != pack.engine.canonical:
            raise ReferencePackError(
                "reference pack engine/profile scope mismatch: "
                f"{pack.profile.canonical} requires "
                f"{_ENGINE_FOR_PROFILE[pack.profile.canonical]}, "
                f"got {pack.engine.canonical}"
            )
        if pack.target.canonical not in _KNOWN_TARGETS:
            raise ReferencePackError(
                f"reference pack target {pack.target.canonical} is not a known target"
            )
        widening = sorted(pack.required_capabilities - BUILTIN_CAPABILITIES)
        if widening:
            raise ReferencePackError(
                "reference pack capability widening denied: " + ", ".join(widening)
            )

    def resolve(self, pack_id: ComponentId) -> ReferencePack:
        pack = self._packs.get(pack_id.canonical)
        if pack is None:
            raise ReferencePackError(f"reference pack {pack_id.canonical} is not registered")
        return pack

    def ids(self) -> tuple[str, ...]:
        """Deterministic registered identities, sorted by canonical ID."""
        return tuple(sorted(self._packs))
