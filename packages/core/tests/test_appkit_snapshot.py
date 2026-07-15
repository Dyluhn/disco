"""AppKit EPIC H — snapshot/summary helpers (`disco.core.appkit.snapshot`).

Covers the PURE versioning + summary functions shared by the manifest writer (H1),
the project routes (H2), and the `app_snapshot_version` tool (H3):

  * `spec_digest` is stable + sensitive (equal specs hash equal; any change moves it);
  * `summarize_specs` projects the validated specs to the compact manifest shape;
  * `tree_digest` / `tree_file_hashes` are deterministic + collision-resistant.
"""

from __future__ import annotations

from disco.core.appkit import (
    default_lead_gen_app_spec,
    generate,
    get_recipe,
    spec_digest,
    summarize_specs,
    tree_digest,
    tree_file_hashes,
)


def _specs():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    app = default_lead_gen_app_spec("Acme Studio", recipe)
    return app, recipe.to_design_spec()


# ---- spec_digest --------------------------------------------------------------


def test_spec_digest_is_stable_and_namespaced():
    app, design = _specs()
    d1 = spec_digest(app, design)
    d2 = spec_digest(app, design)
    assert d1 == d2
    assert d1.startswith("sha256:")


def test_spec_digest_changes_when_app_changes():
    app, design = _specs()
    before = spec_digest(app, design)
    renamed = app.model_copy(update={"name": "Different Brand"})
    assert spec_digest(renamed, design) != before


def test_spec_digest_changes_when_design_changes():
    app, design = _specs()
    before = spec_digest(app, design)
    other = get_recipe("civic-service") or get_recipe("editorial-ledger")
    assert other is not None
    other_design = other.to_design_spec()
    # Only assert the sensitivity when the two recipes actually differ.
    if other_design != design:
        assert spec_digest(app, other_design) != before


# ---- summarize_specs ----------------------------------------------------------


def test_summarize_specs_projects_the_manifest_shape():
    app, design = _specs()
    summary = summarize_specs(app, design)
    assert summary["valid"] is True
    assert summary["name"] == app.name
    assert summary["app_kind"] == app.app_kind
    assert summary["page_count"] == len(app.pages)
    assert summary["entity_count"] == len(app.entities)
    expected_sections = sum(len(p.sections) for p in app.pages)
    assert summary["section_count"] == expected_sections
    # section_kinds is the sorted, de-duplicated set of kinds.
    assert summary["section_kinds"] == sorted({s.kind for p in app.pages for s in p.sections})
    # design tokens are carried through verbatim.
    assert summary["design"]["palette"]["primary"] == design.palette.primary
    assert summary["design"]["typography"]["heading_font"] == design.typography.heading_font
    assert summary["design"]["layout_family"] == design.layout_family
    assert summary["spec_digest"] == spec_digest(app, design)


def test_summarize_specs_is_json_safe():
    import json

    app, design = _specs()
    # Round-trips through JSON unchanged (the manifest persists it as JSON).
    summary = summarize_specs(app, design)
    assert json.loads(json.dumps(summary)) == summary


# ---- tree digests -------------------------------------------------------------


def test_tree_digest_deterministic_and_order_independent():
    app, design = _specs()
    tree = generate(app, design)
    d1 = tree_digest(tree)
    # A reordered dict with identical content hashes identically.
    reordered = dict(reversed(list(tree.items())))
    assert tree_digest(reordered) == d1
    assert d1.startswith("sha256:")


def test_tree_digest_is_collision_resistant_on_boundaries():
    # The length-delimiter guards against the classic "{ab:c} vs {a:bc}" collision.
    assert tree_digest({"ab": "c"}) != tree_digest({"a": "bc"})


def test_tree_file_hashes_one_entry_per_file():
    app, design = _specs()
    tree = generate(app, design)
    hashes = tree_file_hashes(tree)
    assert set(hashes) == set(tree)
    assert all(v.startswith("sha256:") for v in hashes.values())
    # changing one file's bytes changes only that file's hash.
    path = next(iter(tree))
    mutated = dict(tree)
    mutated[path] = tree[path] + "\n/* touched */"
    new_hashes = tree_file_hashes(mutated)
    assert new_hashes[path] != hashes[path]
    for other in tree:
        if other != path:
            assert new_hashes[other] == hashes[other]
