"""Frozen, versioned Reference Pack scope/schema and its bounded registry entry.

A Reference Pack is a *distinct* input concept from a Starter Recipe (scaffold),
a Library Recipe (construction), an AppKit composition, a Freeform composition,
a target, or a persisted profile.  Reference Packs supply *context*: they are
bound to the host ``CONTEXT`` mount.  This module freezes only the Reference Pack
record, its bounded typed value surface, and a deterministic registry for it.  It
adds no user selection, prompt injection, direct-reference workflow,
persistence/ejection, provider or runtime-effect channel of its own: selection,
guided authoring and ejection are owned by :mod:`.library_catalog`, and trust
evaluation, capability intersection, precedence and provenance/BOM by
:mod:`.library_composition`.

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
from .latent_effects import (
    R_LATENT_LEXICON_VERSION,
    StructuredEntry,
    ValueSurface,
    classify_document,
    structured_document,
)

SUPPORTED_REFERENCE_PACK_SCHEMA_VERSION: Literal[1] = 1

#: The frozen lexicon version this schema's value surface is validated against.
SUPPORTED_R_LATENT_VERSION: Literal["R-LATENT/v1"] = R_LATENT_LEXICON_VERSION

#: The declared purpose of every Reference Pack value field.  ``docs`` and
#: ``summary`` are annotation surfaces (``LC4``/``LC2``), so a documentation
#: link is data there; ``callback_url`` is plain data, so an endpoint declared
#: in it is ``R9`` egress.  The contrast between those two is the point: the
#: same bytes are legitimate in one declared surface and latent in another.
REFERENCE_PACK_VALUE_SURFACES: dict[str, ValueSurface] = {
    "label": ValueSurface.DATA,
    "version": ValueSurface.DATA,
    "src": ValueSurface.PROJECT_PATH,
    "callback_url": ValueSurface.DATA,
    "summary": ValueSurface.DOCUMENTATION,
    "docs": ValueSurface.DOCUMENTATION,
    "nested": ValueSurface.STRUCTURED,
    "tiers": ValueSurface.STRUCTURED,
}

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


class ReferencePackValues(FrozenModel):
    """The bounded, typed value surface a Reference Pack may carry.

    ``nested`` and ``tiers`` are tuples of :class:`StructuredEntry` rather than
    ``dict[str, Any]``: a structured value is a tree of typed, immutable entries
    that cannot carry a callable or an arbitrary object, so a structured field
    never becomes an execution channel.
    """

    label: str | None = None
    version: str | None = None
    src: str | None = None
    callback_url: str | None = None
    summary: str | None = None
    docs: str | None = None
    nested: tuple[StructuredEntry, ...] = ()
    tiers: tuple[StructuredEntry, ...] = ()

    def as_document(self) -> dict[str, object]:
        """Project the declared values to the plain document the classifier walks."""
        document: dict[str, object] = {
            "label": self.label,
            "version": self.version,
            "src": self.src,
            "callback_url": self.callback_url,
            "summary": self.summary,
            "docs": self.docs,
        }
        if self.nested:
            document["nested"] = structured_document(self.nested)
        if self.tiers:
            document["tiers"] = structured_document(self.tiers)
        return document


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
    r_latent_version: str = SUPPORTED_R_LATENT_VERSION
    values: ReferencePackValues = ReferencePackValues()


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
        self._validate_values(pack)
        self._packs[key] = pack

    @staticmethod
    def _validate_values(pack: ReferencePack) -> None:
        """Apply the frozen ``R-LATENT`` classification to the value surface."""
        if pack.r_latent_version != SUPPORTED_R_LATENT_VERSION:
            raise ReferencePackError(
                f"unsupported reference pack lexicon version: {pack.r_latent_version}"
            )
        verdict = classify_document(pack.values.as_document(), REFERENCE_PACK_VALUE_SURFACES)
        if not verdict.clean:
            raise ReferencePackError(
                "reference pack value surface rejected by "
                f"{verdict.lexicon_version}: {verdict.classification.value} ({verdict.rule})"
            )

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
