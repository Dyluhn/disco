"""Epic 6.2 - the `feature_flags` primitive.

Covers:
  * FeatureFlagsSpec validation: extra keys, slug keys, count bounds, duplicate
    keys, blank descriptions;
  * lead_gen-only fold semantics: inserts one admin marker section before a
    trailing footer, re-apply updates in place, non-lead-gen hosts are refused;
  * lead-gen lowering: `_flags` schema + seed rows, public enabled-only route,
    owner-gated toggle route, admin section, and `useFlag` / `flags.isEnabled`;
  * real WO-A3-style verify hook wired through the registry.
"""

from __future__ import annotations

import sqlite3

import pytest
from disco.core.appkit import (
    DIRECTORY_PRIMITIVE_ID,
    FEATURE_FLAGS_PRIMITIVE_ID,
    default_directory_app_spec,
    default_lead_gen_app_spec,
    generate,
    get_primitive,
    get_recipe,
)
from disco.core.appkit.feature_flags_primitive import (
    FeatureFlag,
    FeatureFlagsSpec,
    apply_feature_flags_spec,
    feature_flags_for,
    feature_flags_verify,
)
from disco.core.appkit.spec import AppSpec, Section
from pydantic import ValidationError


def _recipe():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design():
    return _recipe().to_design_spec()


def _app():
    return default_lead_gen_app_spec("Flag Shop", _recipe())


def _spec(**overrides: object) -> FeatureFlagsSpec:
    payload: dict[str, object] = {
        "flags": [
            {
                "key": "new-checkout",
                "description": "Show the redesigned checkout flow",
                "enabled": True,
            },
            {"key": "beta-pricing", "description": "Expose beta pricing", "enabled": False},
        ]
    }
    payload.update(overrides)
    return FeatureFlagsSpec.model_validate(payload)


def _folded_app() -> AppSpec:
    return apply_feature_flags_spec(_app(), _spec())


def _flag_section(app: AppSpec) -> Section:
    return next(
        section
        for page in app.pages
        for section in page.sections
        if section.id == "feature_flags"
    )


# ---- spec validation --------------------------------------------------------------


def test_unknown_key_refused() -> None:
    with pytest.raises(ValidationError, match="rollout"):
        _spec(rollout="oops")


def test_flag_key_must_be_slug_and_safe() -> None:
    for bad in ("NewCheckout", "1checkout", "new_checkout", "new checkout", "new!", ""):
        with pytest.raises(ValidationError):
            _spec(flags=[{"key": bad, "enabled": True}])
    with pytest.raises(ValidationError, match="reserved"):
        _spec(flags=[{"key": "constructor", "enabled": True}])
    assert _spec(flags=[{"key": "checkout-v2", "enabled": False}]).flags[0].key == "checkout-v2"


def test_flags_bounds_and_duplicate_keys() -> None:
    with pytest.raises(ValidationError):
        _spec(flags=[])
    with pytest.raises(ValidationError):
        _spec(flags=[{"key": f"flag-{i}", "enabled": False} for i in range(21)])
    with pytest.raises(ValidationError, match="duplicate feature flag key"):
        _spec(flags=[{"key": "alpha", "enabled": True}, {"key": "alpha", "enabled": False}])
    with pytest.raises(ValidationError):
        _spec(flags=[{"key": "alpha", "description": "  ", "enabled": True}])


def test_flag_model_roundtrip() -> None:
    flag = FeatureFlag(key="alpha", description=None, enabled=True)
    assert flag.model_dump() == {"key": "alpha", "description": None, "enabled": True}


# ---- fold ------------------------------------------------------------------------


def test_fold_inserts_admin_marker_before_footer() -> None:
    app = _folded_app()
    home = app.pages[0]
    assert [s.id for s in home.sections][-2:] == ["feature_flags", "footer"]
    section = _flag_section(app)
    assert section.kind == "custom"
    assert section.content_ref == "feature_flags"
    assert section.content is not None
    assert section.content.heading == "Feature flags"
    flags = feature_flags_for(app)
    assert [(f.key, f.enabled) for f in flags] == [
        ("new-checkout", True),
        ("beta-pricing", False),
    ]


def test_reapply_updates_in_place() -> None:
    app = _folded_app()
    updated = apply_feature_flags_spec(
        app,
        _spec(flags=[{"key": "new-checkout", "description": "Updated", "enabled": False}]),
    )
    sections = [s.id for s in updated.pages[0].sections]
    assert sections.count("feature_flags") == 1
    flags = feature_flags_for(updated)
    assert [(f.key, f.description, f.enabled) for f in flags] == [
        ("new-checkout", "Updated", False)
    ]


def test_refuses_non_lead_gen_hosts() -> None:
    directory = default_directory_app_spec("Directory", _recipe())
    assert directory.app_kind == DIRECTORY_PRIMITIVE_ID
    with pytest.raises(ValueError, match="lead_gen-shaped"):
        apply_feature_flags_spec(directory, _spec())


def test_wrong_spec_type_is_type_error() -> None:
    with pytest.raises(TypeError, match="FeatureFlagsSpec"):
        apply_feature_flags_spec(_app(), Section(id="x", kind="custom"))


# ---- generated output ------------------------------------------------------------


def test_generated_schema_seeds_flags_table() -> None:
    tree = generate(_folded_app(), _design())
    schema = tree["schema.sql"]
    assert 'CREATE TABLE IF NOT EXISTS "_flags"' in schema
    assert 'INSERT INTO "_flags" ("key", "description", "enabled") VALUES' in schema
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(schema)
        rows = con.execute(
            'SELECT "key", "description", "enabled" FROM "_flags" ORDER BY "key"'
        ).fetchall()
    finally:
        con.close()
    assert rows == [
        ("beta-pricing", "Expose beta pricing", 0),
        ("new-checkout", "Show the redesigned checkout flow", 1),
    ]


def test_generated_worker_routes_and_client_helpers() -> None:
    tree = generate(_folded_app(), _design())
    worker = tree["worker/index.ts"]
    assert 'url.pathname === "/api/_flags" && request.method === "GET"' in worker
    assert 'WHERE \\"enabled\\" = 1' in worker
    assert "return json({ flags: enabled });" in worker
    toggle_pos = worker.index('url.pathname === "/api/_flags/toggle"')
    auth_pos = worker.index("if (!isAuthorized(request, env))", toggle_pos)
    body_pos = worker.index("body = await request.json();", toggle_pos)
    assert toggle_pos < auth_pos < body_pos
    assert "const expected = env.ADMIN_TOKEN;" in worker

    hook = tree["src/hooks/useFlag.ts"]
    assert "export const flags = {" in hook
    assert "isEnabled(key: string): boolean" in hook
    assert "export function useFlag(key: string): boolean" in hook
    assert 'const FLAGS_PATH = "/api/_flags";' in hook


def test_generated_admin_section_emitted() -> None:
    tree = generate(_folded_app(), _design())
    component = next(src for path, src in tree.items() if path.endswith("FeatureFlagsSection.tsx"))
    assert 'className="section kind-custom feature-flags-admin"' in component
    assert 'const TOGGLE_PATH = "/api/_flags/toggle";' in component
    assert "Authorization: `Bearer ${token.trim()}`" in component
    assert "new-checkout" in component
    assert "beta-pricing" in component


def test_no_flags_tree_has_no_flag_artifacts() -> None:
    tree = generate(_app(), _design())
    blob = "\n".join(tree.values())
    assert '"_flags"' not in blob
    assert "/api/_flags" not in blob
    assert "src/hooks/useFlag.ts" not in tree


# ---- verify + registration -------------------------------------------------------


def test_feature_flags_verify_passes_on_generated_tree() -> None:
    app = _folded_app()
    design = _design()
    tree = generate(app, design)
    res = feature_flags_verify(app, design, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    assert res.detail == "4 passed / 0 failed"
    assert [c.name for c in res.checks] == [
        "feature_flags_seeded",
        "feature_flags_public_route",
        "feature_flags_toggle_owner_gated",
        "feature_flags_admin_section",
    ]


def test_feature_flags_verify_fails_when_toggle_guard_is_removed() -> None:
    app = _folded_app()
    tree = generate(app, _design())
    tree = dict(tree)
    worker = tree["worker/index.ts"]
    toggle_pos = worker.index('url.pathname === "/api/_flags/toggle"')
    tree["worker/index.ts"] = worker[:toggle_pos] + worker[toggle_pos:].replace(
        "      if (!isAuthorized(request, env)) {\n",
        "      if (false) {\n",
        1,
    )
    res = feature_flags_verify(app, _design(), tree)
    checks = {c.name: c for c in res.checks}
    assert checks["feature_flags_toggle_owner_gated"].passed is False


def test_registered_as_fillable_addable_and_verified() -> None:
    prim = get_primitive(FEATURE_FLAGS_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "fillable"
    assert prim.host_contract == ()
    assert prim.spec_schema is FeatureFlagsSpec
    assert prim.apply_spec is apply_feature_flags_spec
    assert prim.verify is feature_flags_verify
