"""Progressive-disclosure selection, guided authoring and owned-byte ejection.

This module owns the *workflow* around installed library material, which the
record seams deliberately do not:

* **Progressive disclosure.**  Browsing yields summaries — identity, scope,
  trust and digest — never content.  Content requires an explicit, direct
  reference to one identity.  There is no API here that hands the whole
  installed set to a prompt, because "installed packs do not enter prompts
  ambiently" is only true if no such call exists to be made by accident.
* **Guided authoring.**  Authoring accepts a *raw* user-supplied document and
  classifies it under the frozen ``R-LATENT/v1`` lexicon **before** it is
  coerced into a typed record.  This is where ``R4`` (a structured payload
  overloaded into a field whose declared shape is scalar) is essential: the
  typed record cannot even represent that case, so a schema-only defence would
  never exercise the rule the frozen corpus's ``POS-006`` names.
* **Owned-byte ejection.**  Ejection returns the owned bytes and leaves **no**
  live pointer: not in the selection index, not in the disclosure index, not in
  the digest index.  Disabling is the separate, weaker operation — a disabled
  entry keeps a resolvable descriptor so already-persisted references retain a
  compatibility and ejection path until they migrate.

Nothing here executes or renders an input, and nothing here reaches a provider.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from .contracts import ComponentId, FrozenModel
from .latent_effects import ValueSurface, classify_document
from .library_composition import InstalledSource, content_digest


class LibraryCatalogError(ValueError):
    """A catalog operation failed closed."""


class AuthoringRejected(LibraryCatalogError):
    """A user-supplied document carried a latent effect and was refused."""


class SourceSummary(FrozenModel):
    """Disclosure level 1 — enough to choose, never enough to inject.

    A summary deliberately carries no value surface.  It is what a selection UI
    may list; obtaining content requires a direct reference to one identity.
    """

    id: ComponentId
    category: str
    mount: str
    trust: str
    provides: tuple[str, ...] = ()
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    enabled: bool = True


class PersistedReferenceResolution(FrozenModel):
    """What a persisted reference still resolves to after an entry is disabled."""

    source: ComponentId
    enabled: bool
    ejectable: bool
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    detail: str = ""


class EjectedBytes(FrozenModel):
    """The owned bytes handed back at ejection, with their exact digest."""

    source: ComponentId
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)
    payload: str


def author_document(
    document: Mapping[str, object],
    surfaces: Mapping[str, ValueSurface],
) -> dict[str, object]:
    """Classify a raw user-supplied document, then hand back the accepted values.

    Guided authoring runs *before* typed coercion on purpose.  A document that
    overloads a scalar-declared field with an array or object is refused here
    with the lexicon's named rule; by the time the same values reach a typed
    record that shape is unrepresentable, so the record path alone could never
    demonstrate the rule was enforced rather than merely unreachable.
    """
    verdict = classify_document(document, surfaces)
    if not verdict.clean:
        raise AuthoringRejected(
            "authored document rejected by "
            f"{verdict.lexicon_version}: {verdict.classification.value} "
            f"({verdict.rule}) in field '{verdict.field}'"
        )
    return dict(document)


class InstalledLibraryCatalog:
    """Owns installed library material, its disclosure, selection and ejection.

    The catalog holds the *owned bytes* of each installed source so ejection can
    hand them back and prove nothing was retained.  It is not a second registry
    of record schemas: the three seams still own their records, and a source
    reaches this catalog only after its own registry admitted it.
    """

    def __init__(self) -> None:
        self._sources: dict[str, InstalledSource] = {}
        self._payloads: dict[str, str] = {}
        self._enabled: dict[str, bool] = {}

    # -- installation -------------------------------------------------------

    def install(self, source: InstalledSource, payload: str) -> None:
        """Take ownership of one already-validated source and its bytes."""
        key = source.id.canonical
        if key in self._sources:
            raise LibraryCatalogError(f"library source already installed: {key}")
        digest = content_digest(payload)
        if digest != source.content_digest:
            raise LibraryCatalogError(
                f"installed payload digest {digest} does not match declared {source.content_digest}"
            )
        self._sources[key] = source
        self._payloads[key] = payload
        self._enabled[key] = True

    # -- progressive disclosure --------------------------------------------

    def disclose(self) -> tuple[SourceSummary, ...]:
        """Level 1 — summaries only, deterministically ordered, never content."""
        return tuple(
            SourceSummary(
                id=source.id,
                category=source.category.value,
                mount=source.mount.value,
                trust=source.trust.value,
                provides=source.provides,
                content_digest=source.content_digest,
                enabled=self._enabled[key],
            )
            for key, source in sorted(self._sources.items())
        )

    def select(self, requested: tuple[ComponentId, ...]) -> tuple[InstalledSource, ...]:
        """Level 2 — resolve only the identities the user asked for, by name.

        An empty request yields an empty selection.  There is deliberately no
        "select everything installed" path: ambient inclusion is prevented by
        the absence of the capability, not by a caller remembering not to.
        """
        selected: list[InstalledSource] = []
        for component in requested:
            key = component.canonical
            source = self._sources.get(key)
            if source is None:
                raise LibraryCatalogError(f"library source {key} is not installed")
            if not self._enabled[key]:
                raise LibraryCatalogError(
                    f"library source {key} is disabled and cannot be newly selected"
                )
            selected.append(source)
        return tuple(sorted(selected, key=lambda item: item.id.canonical))

    def payload_of(self, component: ComponentId) -> str:
        """Level 3 — the owned bytes of one directly referenced identity."""
        key = component.canonical
        if key not in self._payloads:
            raise LibraryCatalogError(f"library source {key} is not installed")
        return self._payloads[key]

    # -- disable / persisted-reference compatibility ------------------------

    def disable(self, component: ComponentId) -> None:
        """Disable a registry entry without orphaning persisted references."""
        key = component.canonical
        if key not in self._sources:
            raise LibraryCatalogError(f"library source {key} is not installed")
        self._enabled[key] = False

    def resolve_persisted(self, component: ComponentId) -> PersistedReferenceResolution:
        """Resolve an already-persisted reference, disabled or not.

        A disabled entry is refused for *new* selection but still resolves here,
        so a build that persisted the reference earlier retains a compatibility
        descriptor and an ejection path until it migrates.  Ejection removes the
        entry outright, and then this fails closed.
        """
        key = component.canonical
        source = self._sources.get(key)
        if source is None:
            raise LibraryCatalogError(
                f"persisted reference {key} has no descriptor: it was ejected, "
                "so no compatibility path remains"
            )
        enabled = self._enabled[key]
        return PersistedReferenceResolution(
            source=source.id,
            enabled=enabled,
            ejectable=True,
            content_digest=source.content_digest,
            detail=(
                "live registry entry"
                if enabled
                else "disabled entry retained for persisted-reference compatibility"
            ),
        )

    # -- owned-byte ejection ------------------------------------------------

    def eject(self, component: ComponentId) -> EjectedBytes:
        """Eject a source's owned bytes and leave no live pointer behind."""
        key = component.canonical
        source = self._sources.get(key)
        if source is None:
            raise LibraryCatalogError(f"library source {key} is not installed")
        payload = self._payloads[key]
        del self._sources[key]
        del self._payloads[key]
        del self._enabled[key]
        return EjectedBytes(
            source=source.id,
            content_digest=source.content_digest,
            byte_count=len(payload.encode("utf-8")),
            payload=payload,
        )

    def live_pointers(self, component: ComponentId) -> tuple[str, ...]:
        """Every index that still names this identity — empty after ejection.

        This exists so "no live pointer remains" is a property a test can
        falsify, rather than a claim resting on reading the ejection code.
        """
        key = component.canonical
        indexes: list[str] = []
        if key in self._sources:
            indexes.append("sources")
        if key in self._payloads:
            indexes.append("payloads")
        if key in self._enabled:
            indexes.append("enabled")
        return tuple(indexes)

    def installed_ids(self) -> tuple[str, ...]:
        """Deterministic installed identities, sorted by canonical ID."""
        return tuple(sorted(self._sources))
