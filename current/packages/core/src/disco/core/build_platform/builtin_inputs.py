"""Typed, read-only ownership map for the current built-in Build inputs.

This module does not ingest user content or create a second runtime registry.
It projects the existing host-owned registries into one deterministic catalog so
composition records can prove that each built-in has exactly one category,
mount, owner, and precedence path.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, model_validator

from .contracts import ComponentId, FrozenModel, TrustLevel


class BuiltinInputCategory(str, Enum):
    STARTER_RECIPE = "starter_recipe"
    REFERENCE_PACK = "reference_pack"
    PROMPT_MODULE = "prompt_module"
    LIBRARY_RECIPE = "library_recipe"
    GOVERNED_CAPABILITY = "governed_capability"


class BuiltinInputMount(str, Enum):
    PROMPT = "prompt"
    CONTEXT = "context"
    SCAFFOLD = "scaffold"
    CONSTRUCTION = "construction"
    ENGINE_INTERNAL = "engine_internal"


_MOUNT_ORDER = {
    BuiltinInputMount.PROMPT: 10,
    BuiltinInputMount.CONTEXT: 20,
    BuiltinInputMount.SCAFFOLD: 30,
    BuiltinInputMount.CONSTRUCTION: 40,
    BuiltinInputMount.ENGINE_INTERNAL: 50,
}
STARTER_SELECTION_PRECEDENCE = ("explicit_request", "profile_default")


class BuiltinInputOwner(FrozenModel):
    component: ComponentId
    source_key: str = Field(min_length=3, max_length=160, pattern=r"^[a-z][a-z0-9_.:@/-]+$")
    category: BuiltinInputCategory
    mount: BuiltinInputMount
    owner: str = Field(min_length=3, max_length=240)
    trust: TrustLevel
    selection_precedence: tuple[str, ...] = ()
    engine: ComponentId | None = None
    shared_origin: str | None = Field(
        default=None,
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_.-]+$",
    )

    @model_validator(mode="after")
    def _category_has_one_exact_mount(self) -> BuiltinInputOwner:
        expected_mount = {
            BuiltinInputCategory.STARTER_RECIPE: BuiltinInputMount.SCAFFOLD,
            BuiltinInputCategory.REFERENCE_PACK: BuiltinInputMount.CONTEXT,
            BuiltinInputCategory.PROMPT_MODULE: BuiltinInputMount.PROMPT,
            BuiltinInputCategory.LIBRARY_RECIPE: BuiltinInputMount.CONSTRUCTION,
            BuiltinInputCategory.GOVERNED_CAPABILITY: BuiltinInputMount.ENGINE_INTERNAL,
        }[self.category]
        if self.mount is not expected_mount:
            raise ValueError(f"{self.category.value} must mount at {expected_mount.value}")
        if self.category is BuiltinInputCategory.STARTER_RECIPE:
            if self.selection_precedence != STARTER_SELECTION_PRECEDENCE:
                raise ValueError("starter precedence must be explicit request then profile default")
        elif self.selection_precedence:
            raise ValueError("only starter recipes declare scaffold selection precedence")
        if self.category is BuiltinInputCategory.GOVERNED_CAPABILITY:
            if self.engine is None or self.trust is not TrustLevel.HOST_POLICY:
                raise ValueError("governed capabilities require a host-policy engine owner")
        elif self.engine is not None:
            raise ValueError("only governed capabilities may be engine-internal")
        return self


class BuiltinInputCatalog(FrozenModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    items: tuple[BuiltinInputOwner, ...]

    @model_validator(mode="after")
    def _unique_and_deterministic(self) -> BuiltinInputCatalog:
        source_keys = [item.source_key for item in self.items]
        components = [item.component.canonical for item in self.items]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("a built-in source has more than one ownership path")
        if len(components) != len(set(components)):
            raise ValueError("a normalized built-in component identity is duplicated")
        shared_origins: dict[str, int] = {}
        for item in self.items:
            if item.shared_origin is not None:
                shared_origins[item.shared_origin] = shared_origins.get(item.shared_origin, 0) + 1
        if any(count < 2 for count in shared_origins.values()):
            raise ValueError("a shared built-in origin must name every intentional access path")
        ordered = tuple(
            sorted(
                self.items,
                key=lambda item: (_MOUNT_ORDER[item.mount], item.component.canonical),
            )
        )
        if self.items != ordered:
            raise ValueError("built-in inputs must use deterministic mount/component order")
        return self

    def owner_for(self, source_key: str) -> BuiltinInputOwner | None:
        return next((item for item in self.items if item.source_key == source_key), None)

    def mounted_at(self, mount: BuiltinInputMount) -> tuple[BuiltinInputOwner, ...]:
        return tuple(item for item in self.items if item.mount is mount)


def _component(name: str, *, version: str = "1") -> ComponentId:
    return ComponentId(namespace="disco_builtin", name=name, version=version)


@lru_cache(maxsize=1)
def builtin_input_catalog() -> BuiltinInputCatalog:
    """Project the shipped registries without adding a second input path."""

    from disco.core.appkit import primitive_ids, recipe_ids
    from disco.core.design.directions import DIRECTIONS
    from disco.core.kits import StarterKitRegistry
    from disco.core.trusted_components.registry import TrustedComponentRegistry
    from disco.core.workflows import PromptPackRegistry

    from .builtin_profiles import APPKIT_ENGINE_ID

    items: list[BuiltinInputOwner] = []
    for starter_id in StarterKitRegistry.default().ids():
        items.append(
            BuiltinInputOwner(
                component=_component(f"starter_{starter_id}"),
                source_key=f"starter:{starter_id}",
                category=BuiltinInputCategory.STARTER_RECIPE,
                mount=BuiltinInputMount.SCAFFOLD,
                owner="disco.core.kits.StarterKitRegistry",
                trust=TrustLevel.TRUSTED_LOCAL,
                selection_precedence=STARTER_SELECTION_PRECEDENCE,
                shared_origin=("appkit.lead_gen_scaffold" if starter_id == "lead_form" else None),
            )
        )
    for direction in DIRECTIONS:
        items.append(
            BuiltinInputOwner(
                component=_component(f"reference_direction_{direction.id}"),
                source_key=f"design_direction:{direction.id}",
                category=BuiltinInputCategory.REFERENCE_PACK,
                mount=BuiltinInputMount.CONTEXT,
                owner="disco.core.design.DIRECTIONS",
                trust=TrustLevel.TRUSTED_LOCAL,
            )
        )
    for recipe_id in recipe_ids():
        items.append(
            BuiltinInputOwner(
                component=_component(f"reference_site_recipe_{recipe_id}"),
                source_key=f"site_recipe:{recipe_id}",
                category=BuiltinInputCategory.REFERENCE_PACK,
                mount=BuiltinInputMount.CONTEXT,
                owner="disco.core.appkit.RECIPES",
                trust=TrustLevel.TRUSTED_LOCAL,
            )
        )
    for pack_id in PromptPackRegistry().ids():
        items.append(
            BuiltinInputOwner(
                component=_component(f"prompt_{pack_id}"),
                source_key=f"prompt_pack:{pack_id}",
                category=BuiltinInputCategory.PROMPT_MODULE,
                mount=BuiltinInputMount.PROMPT,
                owner="disco.core.workflows.PromptPackRegistry",
                trust=TrustLevel.TRUSTED_LOCAL,
            )
        )
    components = TrustedComponentRegistry.default()
    for name in components.names():
        for version in components.versions(name):
            items.append(
                BuiltinInputOwner(
                    component=_component(f"library_{name}", version=version),
                    source_key=f"trusted_component:{name}@{version}",
                    category=BuiltinInputCategory.LIBRARY_RECIPE,
                    mount=BuiltinInputMount.CONSTRUCTION,
                    owner="disco.core.trusted_components.registry.TrustedComponentRegistry",
                    trust=TrustLevel.HOST_POLICY,
                )
            )
    for primitive_id in primitive_ids():
        items.append(
            BuiltinInputOwner(
                component=_component(f"appkit_capability_{primitive_id}"),
                source_key=f"appkit_primitive:{primitive_id}",
                category=BuiltinInputCategory.GOVERNED_CAPABILITY,
                mount=BuiltinInputMount.ENGINE_INTERNAL,
                owner="disco.core.appkit.PrimitiveDefinition",
                trust=TrustLevel.HOST_POLICY,
                engine=APPKIT_ENGINE_ID,
                shared_origin=("appkit.lead_gen_scaffold" if primitive_id == "lead_gen" else None),
            )
        )
    ordered = tuple(
        sorted(items, key=lambda item: (_MOUNT_ORDER[item.mount], item.component.canonical))
    )
    return BuiltinInputCatalog(items=ordered)
