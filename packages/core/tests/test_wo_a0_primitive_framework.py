"""WO-A0 — the extended primitive framework, proven backward-compatible.

Covers:
  * BACKWARD-COMPAT — lead_gen/directory/records still resolve and generate a
    non-empty tree, and all three carry the WO-A0 defaults (tier="fillable",
    empty host_contract, no spec_schema, no verify) — i.e. nothing changed;
  * the new `PrimitiveDefinition` fields exist and default correctly on a fresh
    construction that omits them;
  * `HostService` / `PrimitiveVerifyResult` construct and are frozen;
  * the `hello` primitive registers, and its generate() emits a mountable
    index.html rendering the app name.
"""

from __future__ import annotations

import dataclasses

import pytest
from disco.core.appkit import get_recipe
from disco.core.appkit.primitives import (
    DIRECTORY_PRIMITIVE_ID,
    HELLO_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    RECORDS_PRIMITIVE_ID,
    HostService,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    get_primitive,
    resolve_primitive,
)
from disco.core.appkit.recipes import SiteRecipe
from disco.core.appkit.spec import DesignSpec


def _recipe() -> SiteRecipe:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design() -> DesignSpec:
    return _recipe().to_design_spec()


# ---- 1. backward-compat: the three pre-WO-A0 primitives are untouched ----------


@pytest.mark.parametrize(
    "prim_id",
    [LEAD_GEN_PRIMITIVE_ID, DIRECTORY_PRIMITIVE_ID, RECORDS_PRIMITIVE_ID],
)
def test_existing_primitive_still_resolves_and_generates(prim_id: str) -> None:
    prim = resolve_primitive(prim_id)
    assert prim.id == prim_id
    app = prim.default_app_spec("Acme Studio", _recipe())
    tree = prim.generate(app, _design())
    assert tree, f"{prim_id} generated an empty tree"
    assert all(
        isinstance(path, str) and isinstance(contents, str)
        for path, contents in tree.items()
    )


@pytest.mark.parametrize(
    "prim_id",
    [LEAD_GEN_PRIMITIVE_ID, DIRECTORY_PRIMITIVE_ID, RECORDS_PRIMITIVE_ID],
)
def test_existing_primitive_carries_wo_a0_defaults(prim_id: str) -> None:
    prim = resolve_primitive(prim_id)
    assert prim.tier == "fillable"
    assert prim.host_contract == ()
    assert prim.spec_schema is None
    assert prim.verify is None
    assert prim.apply_spec is None  # WO-A1 default: base scaffolds are not addable


# ---- 2. the new fields exist and default on a fresh construction ---------------


def test_new_fields_default_on_fresh_definition() -> None:
    defn = PrimitiveDefinition(
        id="wo_a0_fresh_test",
        default_app_spec=lambda name, recipe: (_ for _ in ()).throw(NotImplementedError),
        prepare_app_spec=lambda app: app,
        generate=lambda app, design: {},
    )
    # NOT registered — a pure construction proof.
    assert defn.aliases == ()
    assert defn.tier == "fillable"
    assert defn.host_contract == ()
    assert defn.spec_schema is None
    assert defn.verify is None
    assert defn.apply_spec is None


def test_new_fields_accept_explicit_values() -> None:
    def _verify(app: object, design: object, tree: object) -> PrimitiveVerifyResult:
        return PrimitiveVerifyResult(ok=True)

    defn = PrimitiveDefinition(
        id="wo_a0_explicit_test",
        default_app_spec=lambda name, recipe: (_ for _ in ()).throw(NotImplementedError),
        prepare_app_spec=lambda app: app,
        generate=lambda app, design: {},
        tier="template_only",
        host_contract=(HostService("email.send"),),
        verify=_verify,  # type: ignore[arg-type] — shape proof, not a live hook
    )
    assert defn.tier == "template_only"
    assert defn.host_contract == (HostService("email.send"),)
    assert defn.verify is _verify


# ---- 3. HostService / PrimitiveVerifyResult construct + are frozen -------------


def test_host_service_constructs_and_is_frozen() -> None:
    svc = HostService("email.send")
    assert svc.name == "email.send"
    with pytest.raises(dataclasses.FrozenInstanceError):
        svc.name = "ai.chat"  # type: ignore[misc]


def test_primitive_verify_result_constructs_and_is_frozen() -> None:
    result = PrimitiveVerifyResult(ok=True)
    assert result.ok is True
    assert result.detail == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]
    detailed = PrimitiveVerifyResult(ok=False, detail="missing heading")
    assert detailed.detail == "missing heading"


# ---- 4. the hello primitive: registered + generates a mountable tree -----------


def test_hello_primitive_registered() -> None:
    from disco.core.appkit.hello_primitive import HelloSpec, apply_hello_spec

    prim = get_primitive(HELLO_PRIMITIVE_ID)
    assert prim is not None
    assert prim.id == HELLO_PRIMITIVE_ID
    assert prim.tier == "fillable"
    assert prim.host_contract == ()
    # WO-A1 made hello ADDABLE: it declares the fill-and-validate pair.
    assert prim.spec_schema is HelloSpec
    assert prim.apply_spec is apply_hello_spec
    assert prim.verify is None


def test_hello_primitive_generates_index_html_with_app_name() -> None:
    prim = get_primitive("hello")
    assert prim is not None
    app = prim.default_app_spec("Hi", _recipe())
    assert app.app_kind == HELLO_PRIMITIVE_ID
    assert prim.prepare_app_spec(app) is app  # identity prepare
    tree = prim.generate(app, _design())
    assert "index.html" in tree
    page = tree["index.html"]
    assert "Hi" in page
    assert "Hello from the hello primitive" in page
    assert "<h1>" in page and "</h1>" in page
    assert page.lstrip().lower().startswith("<!doctype html>")
