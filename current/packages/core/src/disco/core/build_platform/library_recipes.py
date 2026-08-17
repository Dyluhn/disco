"""Frozen, versioned Library Recipe scope/schema and its bounded registry entry.

A Library Recipe is a *distinct* input concept from a Starter Recipe (scaffold),
a Reference Pack (context), an AppKit composition, a Freeform composition, a
target, or a persisted profile.  Library Recipes *construct*: they are bound to
the host ``CONSTRUCTION`` mount, which is what separates them from the Starter
Recipe's ``SCAFFOLD`` mount and the Reference Pack's ``CONTEXT`` mount.  The
host's own ``BuiltinInputCategory`` already binds ``LIBRARY_RECIPE`` to
``CONSTRUCTION``; this module does not invent a second mount vocabulary.

Like the sibling seams, this module freezes a record, a bounded typed value
surface, and a deterministic fail-closed registry.  It adds no selection, prompt
injection, persistence/ejection, provider, or runtime-effect channel — selection,
authoring and ejection are owned by :mod:`.library_catalog`, and trust,
capability intersection, precedence and provenance by
:mod:`.library_composition`.

The value surface is classified under the frozen ``R-LATENT/v1`` lexicon owned by
:mod:`.latent_effects`, so a Library Recipe carrying a latent executable effect
is refused at registration with a named verdict and rule.
"""

from __future__ import annotations

from typing import Literal

from .builtin_inputs import BuiltinInputMount
from .builtin_profiles import (
    APPKIT_ENGINE_ID,
    APPKIT_PROFILE_ID,
    BUILTIN_CAPABILITIES,
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    WEB_TARGET_ID,
)
from .contracts import ComponentId, FrozenModel, TrustLevel
from .latent_effects import (
    R_LATENT_LEXICON_VERSION,
    StructuredEntry,
    ValueSurface,
    classify_document,
    structured_document,
)

SUPPORTED_LIBRARY_RECIPE_SCHEMA_VERSION: Literal[1] = 1

#: The frozen lexicon version this schema's value surface is validated against.
SUPPORTED_R_LATENT_VERSION: Literal["R-LATENT/v1"] = R_LATENT_LEXICON_VERSION

_LIBRARY_RECIPE_NAMESPACE = "disco_libraryrecipe"

# The canonical construction engines and the target-owned built-in target, held
# identically to the sibling seams so a Library Recipe's scope is checked the
# same deterministic way rather than read from a label.
_KNOWN_ENGINES = frozenset({APPKIT_ENGINE_ID.canonical, FREEFORM_ENGINE_ID.canonical})
_KNOWN_TARGETS = frozenset({WEB_TARGET_ID.canonical})

_ENGINE_FOR_PROFILE = {
    APPKIT_PROFILE_ID.canonical: APPKIT_ENGINE_ID.canonical,
    FREEFORM_PROFILE_ID.canonical: FREEFORM_ENGINE_ID.canonical,
}

#: The declared purpose of every Library Recipe value field.  ``env`` is an
#: environment-variable NAME map (``LC3``): it may reference a name, never carry
#: a secret value.  ``output`` is a project path, so an escape out of the
#: declared root is ``R7`` rather than ordinary data.
LIBRARY_RECIPE_VALUE_SURFACES: dict[str, ValueSurface] = {
    "description": ValueSurface.DOCUMENTATION,
    "hook": ValueSurface.DATA,
    "template": ValueSurface.DATA,
    "value": ValueSurface.DATA,
    "output": ValueSurface.PROJECT_PATH,
    "enabled": ValueSurface.DATA,
    "env": ValueSurface.ENV_NAME_MAP,
    "features": ValueSurface.STRUCTURED,
}


class LibraryRecipeError(ValueError):
    """A Library Recipe registration or resolution failed closed."""


class LibraryRecipeValues(FrozenModel):
    """The bounded, typed value surface a Library Recipe may carry.

    ``env`` is a tuple of :class:`StructuredEntry` rather than a
    ``dict[str, Any]``: an environment map is a tree of typed, immutable entries
    that cannot carry a callable or an arbitrary object, so the construction
    surface never becomes an execution channel.
    """

    description: str | None = None
    hook: str | None = None
    template: str | None = None
    value: str | None = None
    output: str | None = None
    enabled: bool | None = None
    env: tuple[StructuredEntry, ...] = ()
    features: tuple[str, ...] = ()

    def as_document(self) -> dict[str, object]:
        """Project the declared values to the plain document the classifier walks."""
        document: dict[str, object] = {
            "description": self.description,
            "hook": self.hook,
            "template": self.template,
            "value": self.value,
            "output": self.output,
            "enabled": self.enabled,
        }
        if self.env:
            document["env"] = structured_document(self.env)
        if self.features:
            document["features"] = list(self.features)
        return document


class LibraryRecipe(FrozenModel):
    """A versioned Library Recipe record with explicit construction scope.

    ``id`` is the recipe's own distinct typed identity.  ``engine``, ``profile``
    and ``target`` are the exact construction engine, build profile and
    target-owned target this recipe is scoped to; ``required_capabilities`` is
    its explicit capability requirement and ``trust`` its explicit tier.  Every
    scope is explicit here; none is inferred from a label.
    """

    id: ComponentId
    schema_version: int = SUPPORTED_LIBRARY_RECIPE_SCHEMA_VERSION
    engine: ComponentId
    profile: ComponentId
    target: ComponentId
    trust: TrustLevel
    required_capabilities: frozenset[str] = frozenset()
    construction_mount: BuiltinInputMount = BuiltinInputMount.CONSTRUCTION
    r_latent_version: str = SUPPORTED_R_LATENT_VERSION
    values: LibraryRecipeValues = LibraryRecipeValues()


class LibraryRecipeRegistry:
    """Sole owner of frozen, host-scoped Library Recipe records.

    Registration is deterministic and fail-closed.  A record is admitted only
    when its schema version is supported, its identity is a distinct Library
    Recipe ID (never a Starter Recipe, Reference Pack, AppKit, Freeform, target,
    persisted-profile or host-catalog identity), its engine/profile/target scope
    is coherent and known, its trust is an explicit tier, its required capability
    scope does not widen the host ceiling, it is bound to the frozen
    ``R-LATENT/v1`` vocabulary and the host ``CONSTRUCTION`` mount, and its typed
    value surface classifies clean.  Duplicate identities and any scope or
    classification violation raise with a typed, named reason.
    """

    def __init__(self) -> None:
        self._recipes: dict[str, LibraryRecipe] = {}

    def register(self, recipe: LibraryRecipe) -> None:
        if type(recipe) is not LibraryRecipe:
            raise LibraryRecipeError("library recipe registration requires exact frozen data")
        if recipe.schema_version != SUPPORTED_LIBRARY_RECIPE_SCHEMA_VERSION:
            raise LibraryRecipeError(
                f"unsupported library recipe schema version: {recipe.schema_version}"
            )
        if recipe.r_latent_version != SUPPORTED_R_LATENT_VERSION:
            raise LibraryRecipeError(
                "unsupported R-LATENT binding: "
                f"{recipe.r_latent_version} is not {SUPPORTED_R_LATENT_VERSION}"
            )
        if recipe.construction_mount is not BuiltinInputMount.CONSTRUCTION:
            raise LibraryRecipeError(
                "a library recipe constructs and must mount at the host construction axis"
            )
        if recipe.id.namespace != _LIBRARY_RECIPE_NAMESPACE:
            raise LibraryRecipeError(
                "library recipe id must use the distinct disco_libraryrecipe namespace"
            )
        # The namespace guard above already forbids every Starter Recipe
        # (disco_starter), Reference Pack (disco_refpack), host-catalog
        # (disco_builtin) and AppKit/Freeform/target/persisted-profile (disco or
        # model_role) identity, so a Library Recipe can never reuse one as its own.
        key = recipe.id.canonical
        if key in self._recipes:
            raise LibraryRecipeError(f"duplicate library recipe id: {key}")
        self._validate_scope(recipe)
        self._validate_values(recipe)
        self._recipes[key] = recipe

    @staticmethod
    def _validate_scope(recipe: LibraryRecipe) -> None:
        if recipe.engine.canonical not in _KNOWN_ENGINES:
            raise LibraryRecipeError(
                f"library recipe engine {recipe.engine.canonical} "
                "is not a known construction engine"
            )
        if recipe.profile.canonical not in _ENGINE_FOR_PROFILE:
            raise LibraryRecipeError(
                f"library recipe profile {recipe.profile.canonical} is not a known build profile"
            )
        if _ENGINE_FOR_PROFILE[recipe.profile.canonical] != recipe.engine.canonical:
            raise LibraryRecipeError(
                "library recipe engine/profile scope mismatch: "
                f"{recipe.profile.canonical} requires "
                f"{_ENGINE_FOR_PROFILE[recipe.profile.canonical]}, "
                f"got {recipe.engine.canonical}"
            )
        if recipe.target.canonical not in _KNOWN_TARGETS:
            raise LibraryRecipeError(
                f"library recipe target {recipe.target.canonical} is not a known target"
            )
        widening = sorted(recipe.required_capabilities - BUILTIN_CAPABILITIES)
        if widening:
            raise LibraryRecipeError(
                "library recipe capability widening denied: " + ", ".join(widening)
            )

    @staticmethod
    def _validate_values(recipe: LibraryRecipe) -> None:
        """Apply the frozen ``R-LATENT`` classification to the value surface."""
        verdict = classify_document(recipe.values.as_document(), LIBRARY_RECIPE_VALUE_SURFACES)
        if not verdict.clean:
            raise LibraryRecipeError(
                "library recipe value surface rejected by "
                f"{verdict.lexicon_version}: {verdict.classification.value} ({verdict.rule})"
            )

    def resolve(self, recipe_id: ComponentId) -> LibraryRecipe:
        recipe = self._recipes.get(recipe_id.canonical)
        if recipe is None:
            raise LibraryRecipeError(f"library recipe {recipe_id.canonical} is not registered")
        return recipe

    def ids(self) -> tuple[str, ...]:
        """Deterministic registered identities, sorted by canonical ID."""
        return tuple(sorted(self._recipes))
