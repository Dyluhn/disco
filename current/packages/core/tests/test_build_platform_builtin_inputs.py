from __future__ import annotations

import pytest
from disco.core.appkit import primitive_ids, recipe_ids
from disco.core.build_platform import (
    APPKIT_ENGINE_ID,
    STARTER_SELECTION_PRECEDENCE,
    BuiltinInputCatalog,
    BuiltinInputCategory,
    BuiltinInputMount,
    BuiltinInputOwner,
    ComponentId,
    ToolDescriptor,
    TrustLevel,
    builtin_input_catalog,
    resolve_builtin_composition,
)
from disco.core.design.directions import DIRECTIONS
from disco.core.kits import StarterKitRegistry
from disco.core.trusted_components.registry import TrustedComponentRegistry
from disco.core.workflows import PromptPackRegistry
from pydantic import ValidationError


def _expected_source_keys() -> set[str]:
    components = TrustedComponentRegistry.default()
    return {
        *(f"starter:{item}" for item in StarterKitRegistry.default().ids()),
        *(f"design_direction:{item.id}" for item in DIRECTIONS),
        *(f"site_recipe:{item}" for item in recipe_ids()),
        *(f"prompt_pack:{item}" for item in PromptPackRegistry().ids()),
        *(
            f"trusted_component:{name}@{version}"
            for name in components.names()
            for version in components.versions(name)
        ),
        *(f"appkit_primitive:{item}" for item in primitive_ids()),
    }


def test_every_current_builtin_has_one_typed_owner_and_mount() -> None:
    catalog = builtin_input_catalog()
    assert {item.source_key for item in catalog.items} == _expected_source_keys()
    assert len(catalog.items) == len(_expected_source_keys())
    owner_by_prefix = {
        "starter:": "disco.core.kits.StarterKitRegistry",
        "design_direction:": "disco.core.design.DIRECTIONS",
        "site_recipe:": "disco.core.appkit.RECIPES",
        "prompt_pack:": "disco.core.workflows.PromptPackRegistry",
        "trusted_component:": ("disco.core.trusted_components.registry.TrustedComponentRegistry"),
        "appkit_primitive:": "disco.core.appkit.PrimitiveDefinition",
    }

    for item in catalog.items:
        assert item.owner == next(
            owner for prefix, owner in owner_by_prefix.items() if item.source_key.startswith(prefix)
        )
        if item.category is BuiltinInputCategory.STARTER_RECIPE:
            assert item.mount is BuiltinInputMount.SCAFFOLD
            assert item.selection_precedence == STARTER_SELECTION_PRECEDENCE
        elif item.category is BuiltinInputCategory.REFERENCE_PACK:
            assert item.mount is BuiltinInputMount.CONTEXT
        elif item.category is BuiltinInputCategory.PROMPT_MODULE:
            assert item.mount is BuiltinInputMount.PROMPT
            assert item.trust is TrustLevel.TRUSTED_LOCAL
        elif item.category is BuiltinInputCategory.LIBRARY_RECIPE:
            assert item.mount is BuiltinInputMount.CONSTRUCTION
        else:
            assert item.category is BuiltinInputCategory.GOVERNED_CAPABILITY
            assert item.mount is BuiltinInputMount.ENGINE_INTERNAL
            assert item.engine == APPKIT_ENGINE_ID


def test_catalog_order_and_serialization_are_deterministic() -> None:
    first = builtin_input_catalog()
    second = builtin_input_catalog()
    assert first is second
    assert first.model_dump_json() == second.model_dump_json()
    assert tuple(item.source_key for item in first.items) == tuple(
        item.source_key for item in second.items
    )


def test_lead_form_shared_scaffold_origin_is_explicit_not_hidden() -> None:
    catalog = builtin_input_catalog()
    starter = catalog.owner_for("starter:lead_form")
    capability = catalog.owner_for("appkit_primitive:lead_gen")
    assert starter is not None and capability is not None
    assert starter.shared_origin == capability.shared_origin == "appkit.lead_gen_scaffold"
    assert starter.mount is BuiltinInputMount.SCAFFOLD
    assert capability.mount is BuiltinInputMount.ENGINE_INTERNAL


def test_duplicate_or_cross_mounted_source_fails_closed() -> None:
    item = builtin_input_catalog().items[0]
    with pytest.raises(ValidationError, match="more than one ownership path"):
        BuiltinInputCatalog(items=(item, item))
    with pytest.raises(ValidationError, match="must mount"):
        BuiltinInputOwner(
            component=ComponentId(namespace="test", name="wrong_mount", version="1"),
            source_key="starter:wrong_mount",
            category=BuiltinInputCategory.STARTER_RECIPE,
            mount=BuiltinInputMount.PROMPT,
            owner="test.owner",
            trust=TrustLevel.TRUSTED_LOCAL,
            selection_precedence=STARTER_SELECTION_PRECEDENCE,
        )


def test_builtin_composition_digest_carries_the_complete_owner_map() -> None:
    composition = resolve_builtin_composition(
        appkit=True,
        goal="compose governed inputs",
        tool_catalog=(ToolDescriptor(name="file_read", description="read"),),
        visible_tools=frozenset({"file_read"}),
    )
    assert composition.builtin_input_owners == builtin_input_catalog().items
    assert composition.digest.digest.startswith("sha256:")
