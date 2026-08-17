"""Trust evaluation, capability intersection, precedence and provenance/BOM.

This module is the sole owner of what happens when *several* installed library
inputs meet one build.  The three record seams — :mod:`.reference_packs`
(``CONTEXT``), :mod:`.starter_recipes` (``SCAFFOLD``) and :mod:`.library_recipes`
(``CONSTRUCTION``) — each own one record kind and validate it in isolation.
None of them can answer a cross-source question, and this module answers exactly
four of them:

* **Trust-tier evaluation.**  A record's ``trust`` is not decoration: each tier
  carries its own capability ceiling, so installed material can never widen
  host/profile/target policy.  A record whose declared capabilities sit inside
  the host ceiling can still be refused because its *tier* does not reach them.
* **Capability intersection.**  The effective grant is the intersection of the
  host layer, the profile layer and every admitted source's tier ceiling.  The
  intersection is monotone by construction: adding a source can only narrow the
  result, never widen it.
* **Deterministic precedence.**  When several sources supply the same named
  slot, exactly one wins under a total, documented order — never "last one in".
* **Provenance / BOM.**  The resulting artifact records its exact inputs: every
  source that was *considered* appears in the bill of materials, included or
  superseded, with a content digest and a named reason.

Nothing here executes, renders, or injects an input.  Selection, authoring and
ejection are owned by :mod:`.library_catalog`.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import Field

from .builtin_inputs import BuiltinInputCategory, BuiltinInputMount
from .builtin_profiles import BUILTIN_CAPABILITIES
from .contracts import (
    CapabilityDenial,
    CapabilityLayer,
    ComponentId,
    EffectiveCapabilityPolicy,
    FrozenModel,
    TrustLevel,
)

#: The host mount each library input category is bound to.  This mirrors the
#: host's own ``BuiltinInputOwner`` validator rather than introducing a second
#: mount vocabulary, so the three concepts stay distinct with explicit owners.
MOUNT_FOR_CATEGORY: dict[BuiltinInputCategory, BuiltinInputMount] = {
    BuiltinInputCategory.REFERENCE_PACK: BuiltinInputMount.CONTEXT,
    BuiltinInputCategory.STARTER_RECIPE: BuiltinInputMount.SCAFFOLD,
    BuiltinInputCategory.LIBRARY_RECIPE: BuiltinInputMount.CONSTRUCTION,
}

#: The capability ceiling each trust tier may reach, most authoritative first.
#:
#: This is the evaluation that makes ``trust`` load bearing.  ``HOST_POLICY`` is
#: host-owned material and reaches the full built-in ceiling.  ``TRUSTED_LOCAL``
#: is owner-installed local material: it may read and write the workspace and
#: drive interactive display, but not the two capabilities that carry an effect
#: *outside* the workspace.  ``UNTRUSTED_PORTABLE`` is portable third-party
#: material and is inert — read-only.
#:
#: The ladder is deliberately monotone (each tier's ceiling is a subset of the
#: tier above it), which is what makes the intersection below well behaved.
TRUST_CEILINGS: dict[TrustLevel, frozenset[str]] = {
    TrustLevel.HOST_POLICY: frozenset(BUILTIN_CAPABILITIES),
    TrustLevel.TRUSTED_LOCAL: frozenset(
        {"workspace.read", "workspace.write", "display.interactive"}
    ),
    TrustLevel.UNTRUSTED_PORTABLE: frozenset({"workspace.read"}),
}

#: Precedence rank of each trust tier; lower wins.
_TRUST_RANK: dict[TrustLevel, int] = {
    TrustLevel.HOST_POLICY: 0,
    TrustLevel.TRUSTED_LOCAL: 1,
    TrustLevel.UNTRUSTED_PORTABLE: 2,
}

#: Precedence rank of each mount; lower wins.  Ordered the same way the host
#: orders its own built-in inputs.
_MOUNT_RANK: dict[BuiltinInputMount, int] = {
    BuiltinInputMount.PROMPT: 10,
    BuiltinInputMount.CONTEXT: 20,
    BuiltinInputMount.SCAFFOLD: 30,
    BuiltinInputMount.CONSTRUCTION: 40,
    BuiltinInputMount.ENGINE_INTERNAL: 50,
}


class LibraryCompositionError(ValueError):
    """A library composition failed closed."""


class TrustDenialReason(str, Enum):
    """Why a source was refused, named rather than boolean."""

    CAPABILITY_ABOVE_HOST_CEILING = "capability_above_host_ceiling"
    CAPABILITY_ABOVE_TRUST_CEILING = "capability_above_trust_ceiling"
    MOUNT_DOES_NOT_MATCH_CATEGORY = "mount_does_not_match_category"


class InstalledSource(FrozenModel):
    """One installed library input, normalized across the three record kinds.

    The three seams own their own records; this is the single normalized shape
    the cross-source questions are answered over.  ``provides`` names the slots
    this source supplies, which is what precedence resolves; ``content_digest``
    is what the bill of materials records so an artifact's inputs are exact.
    """

    id: ComponentId
    category: BuiltinInputCategory
    mount: BuiltinInputMount
    trust: TrustLevel
    required_capabilities: frozenset[str] = frozenset()
    provides: tuple[str, ...] = ()
    provenance: str = Field(min_length=1, max_length=240)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def precedence_key(self) -> tuple[int, int, str]:
        """The total, documented order used to resolve a contested slot.

        Trust first (a more authoritative tier always wins), then mount order,
        then the canonical identity as a total tiebreak.  Registration order is
        deliberately *not* part of the key: "last one in wins" is exactly the
        non-determinism this resolves.
        """
        return (_TRUST_RANK[self.trust], _MOUNT_RANK[self.mount], self.id.canonical)


def content_digest(values: object) -> str:
    """A deterministic ``sha256:`` digest of a declared value surface."""
    encoded = json.dumps(values, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class TrustDenial(FrozenModel):
    """One named refusal of an installed source."""

    source: ComponentId
    reason: TrustDenialReason
    capabilities: tuple[str, ...] = ()
    detail: str = ""


class TrustEvaluation(FrozenModel):
    """The result of evaluating installed material against the host ceiling."""

    admitted: tuple[InstalledSource, ...] = ()
    denials: tuple[TrustDenial, ...] = ()

    @property
    def admitted_ids(self) -> tuple[str, ...]:
        return tuple(source.id.canonical for source in self.admitted)


def evaluate_trust(
    sources: tuple[InstalledSource, ...],
    *,
    host_ceiling: frozenset[str] = frozenset(BUILTIN_CAPABILITIES),
) -> TrustEvaluation:
    """Evaluate each source's declared capabilities against its trust tier.

    Two distinct refusals are reported separately because they mean different
    things: a capability outside the *host* ceiling is not a capability at all,
    while a capability inside the host ceiling but above the source's *trust*
    ceiling is the tier doing its job.  Conflating them is what made ``trust``
    look enforced when it was only stored.
    """
    admitted: list[InstalledSource] = []
    denials: list[TrustDenial] = []

    for source in sorted(sources, key=lambda item: item.id.canonical):
        expected_mount = MOUNT_FOR_CATEGORY[source.category]
        if source.mount is not expected_mount:
            denials.append(
                TrustDenial(
                    source=source.id,
                    reason=TrustDenialReason.MOUNT_DOES_NOT_MATCH_CATEGORY,
                    detail=f"{source.category.value} must mount at {expected_mount.value}",
                )
            )
            continue

        above_host = tuple(sorted(source.required_capabilities - host_ceiling))
        if above_host:
            denials.append(
                TrustDenial(
                    source=source.id,
                    reason=TrustDenialReason.CAPABILITY_ABOVE_HOST_CEILING,
                    capabilities=above_host,
                    detail="installed material may not widen the host capability ceiling",
                )
            )
            continue

        ceiling = TRUST_CEILINGS[source.trust]
        above_trust = tuple(sorted(source.required_capabilities - ceiling))
        if above_trust:
            denials.append(
                TrustDenial(
                    source=source.id,
                    reason=TrustDenialReason.CAPABILITY_ABOVE_TRUST_CEILING,
                    capabilities=above_trust,
                    detail=f"trust tier {source.trust.value} does not reach these capabilities",
                )
            )
            continue

        admitted.append(source)

    return TrustEvaluation(admitted=tuple(admitted), denials=tuple(denials))


def intersect_capabilities(
    sources: tuple[InstalledSource, ...],
    *,
    host: CapabilityLayer,
    profile: CapabilityLayer,
) -> EffectiveCapabilityPolicy:
    """Intersect the host layer, the profile layer and every source's tier ceiling.

    The result is deterministic and monotone: each source contributes its trust
    ceiling as one more layer, and an intersection can only ever narrow.  A
    source therefore cannot widen host or profile policy no matter what it
    declares — the property is structural, not a check that could be forgotten.
    """
    layers: list[CapabilityLayer] = [host, profile]
    layers.extend(
        CapabilityLayer(
            source=f"{source.category.value}:{source.id.canonical}",
            allowed=TRUST_CEILINGS[source.trust],
        )
        for source in sorted(sources, key=lambda item: item.id.canonical)
    )

    allowed = set(layers[0].allowed)
    for layer in layers[1:]:
        allowed &= set(layer.allowed)

    candidates: set[str] = set()
    for layer in layers:
        candidates |= set(layer.allowed)

    denials = tuple(
        CapabilityDenial(
            capability=capability,
            denied_by=tuple(
                sorted(layer.source for layer in layers if capability not in layer.allowed)
            ),
            reason="not present in every capability layer of the intersection",
        )
        for capability in sorted(candidates - allowed)
    )
    return EffectiveCapabilityPolicy(allowed=frozenset(allowed), denied=denials)


class BillOfMaterialsEntry(FrozenModel):
    """One considered input, included or superseded, with an exact digest."""

    ordinal: int = Field(ge=0)
    source: ComponentId
    category: BuiltinInputCategory
    mount: BuiltinInputMount
    trust: TrustLevel
    provenance: str
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provides: tuple[str, ...] = ()
    included: bool
    superseded_by: ComponentId | None = None
    reason: str = ""


class LibraryBillOfMaterials(FrozenModel):
    """The exact inputs of one composed artifact.

    Every *considered* source appears, not only the winners: a bill of materials
    that silently omitted the superseded candidates would not let a reader
    reconstruct why the artifact contains what it contains.
    """

    schema_version: int = Field(default=1, ge=1, le=1)
    entries: tuple[BillOfMaterialsEntry, ...] = ()

    @property
    def included(self) -> tuple[BillOfMaterialsEntry, ...]:
        return tuple(entry for entry in self.entries if entry.included)

    def entry_for(self, source: ComponentId) -> BillOfMaterialsEntry | None:
        return next(
            (entry for entry in self.entries if entry.source.canonical == source.canonical), None
        )

    @property
    def digest(self) -> str:
        """A digest over the whole bill, so an artifact can pin its exact inputs."""
        return content_digest(
            [
                [entry.source.canonical, entry.content_digest, entry.included]
                for entry in self.entries
            ]
        )


def resolve_precedence(sources: tuple[InstalledSource, ...]) -> LibraryBillOfMaterials:
    """Resolve contested slots deterministically and record the full provenance.

    A slot is contested when more than one source ``provides`` it.  The winner
    is the minimum of :attr:`InstalledSource.precedence_key`, which is a total
    order, so the outcome does not depend on registration order, dict ordering,
    or which source was installed most recently.  A source is included when it
    wins at least one of the slots it provides; a source that provides nothing
    is included as an unconditional contributor.
    """
    ordered = sorted(sources, key=lambda item: item.precedence_key)

    winner_for_slot: dict[str, InstalledSource] = {}
    for source in ordered:
        for slot in source.provides:
            if slot not in winner_for_slot:
                winner_for_slot[slot] = source

    entries: list[BillOfMaterialsEntry] = []
    for ordinal, source in enumerate(ordered):
        won = tuple(slot for slot in source.provides if winner_for_slot[slot] is source)
        lost = tuple(slot for slot in source.provides if winner_for_slot[slot] is not source)
        included = bool(won) or not source.provides

        superseded_by: ComponentId | None = None
        reason = ""
        if included and lost:
            reason = (
                "wins " + ", ".join(sorted(won)) + "; superseded for " + ", ".join(sorted(lost))
            )
        elif included and source.provides:
            reason = "wins " + ", ".join(sorted(won))
        elif included:
            reason = "contributes no contested slot"
        else:
            superseded_by = winner_for_slot[source.provides[0]].id
            reason = "superseded for " + ", ".join(sorted(lost))

        entries.append(
            BillOfMaterialsEntry(
                ordinal=ordinal,
                source=source.id,
                category=source.category,
                mount=source.mount,
                trust=source.trust,
                provenance=source.provenance,
                content_digest=source.content_digest,
                provides=source.provides,
                included=included,
                superseded_by=superseded_by,
                reason=reason,
            )
        )

    return LibraryBillOfMaterials(entries=tuple(entries))
