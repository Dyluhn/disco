"""AppKit EPIC E2/E3 — the validated-patch app tools (fake sandbox/workspace).

Covers, through the real tool interface against an in-memory sandbox:
  * app_create writes the generated tree + BOTH .disco specs; refuses to overwrite
    without `overwrite`; rejects an unknown recipe and an invalid app_spec;
  * app_add_section inserts a validated section and touches ONLY the affected files
    (new component, App.tsx, content.ts, manifest, appspec) — never the worker/CSS;
    an invalid variant (wrong kind) is rejected;
  * app_update_content patches the spec content and touches ONLY appspec.json +
    content.ts + manifest (the component is NOT rewritten — it reads CONTENT by id);
  * app_set_design swaps the DesignSpec and touches ONLY the design files
    (designspec, styles.css, index.html, manifest) — never the worker/schema/spec;
    a raw DesignSpec that would generate slop is REJECTED by the lint gate.
"""

from __future__ import annotations

import json

import pytest
from disco.core.appkit import APPSPEC_RELPATH, DESIGNSPEC_RELPATH
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddSectionArgs,
    AppAddSectionTool,
    AppCreateArgs,
    AppCreateTool,
    AppSetDesignArgs,
    AppSetDesignTool,
    AppSnapshotVersionArgs,
    AppSnapshotVersionTool,
    AppUpdateContentArgs,
    AppUpdateContentTool,
)
from tool_fakes import FakeSandboxInstance


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-appkit",
    )


async def _create(sbx: FakeSandboxInstance, *, recipe="editorial-ledger", **kw):
    return await AppCreateTool().run(
        AppCreateArgs(recipe_id=recipe, brief="Acme Studio", **kw), _ctx(sbx)
    )


def _files(sbx: FakeSandboxInstance) -> set[str]:
    return set(sbx._fs)


def _schema_allows_string_array(schema: dict) -> bool:
    branches = schema.get("anyOf") or [schema]
    return any(
        branch.get("type") == "array" and branch.get("items", {}).get("type") == "string"
        for branch in branches
    )


def _non_null_branch(schema: dict) -> dict:
    branches = schema.get("anyOf") or [schema]
    for branch in branches:
        if branch.get("type") != "null":
            return branch
    raise AssertionError(f"no non-null schema branch found in {schema!r}")


# ---- app_create ---------------------------------------------------------------


async def test_app_create_writes_tree_and_both_specs():
    sbx = FakeSandboxInstance()
    out = await _create(sbx)
    assert out.success, out.content
    files = _files(sbx)
    # both specs persisted
    assert APPSPEC_RELPATH in files
    assert DESIGNSPEC_RELPATH in files
    # the generated tree landed
    for path in (
        "index.html",
        "src/main.tsx",
        "src/App.tsx",
        "src/styles.css",
        "src/generated/content.ts",
        "src/generated/manifest.ts",
        "worker/index.ts",
        "schema.sql",
        "wrangler.toml",
    ):
        assert path in files, path
    # the persisted appspec is valid JSON and declares the lead entity
    spec = json.loads(sbx._fs[APPSPEC_RELPATH].decode("utf-8"))
    assert any(e["id"] == "lead" for e in spec["entities"])


async def test_app_create_refuses_overwrite_then_allows_with_flag():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    blocked = await _create(sbx)
    assert not blocked.success
    assert blocked.error == "app_create_refused"
    assert (await _create(sbx, overwrite=True)).success


async def test_app_create_rejects_unknown_recipe():
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(AppCreateArgs(recipe_id="no-such-recipe"), _ctx(sbx))
    assert not out.success
    assert "unknown recipe" in out.content.lower()


async def test_app_create_rejects_invalid_app_spec():
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            app_spec={
                "schema_version": 1,
                "app_kind": "x",
                "name": "n",
                "pages": [{"id": "p", "route": "no-slash", "title": "t"}],
            },
        ),
        _ctx(sbx),
    )
    assert not out.success
    assert "invalid app_spec" in out.content.lower()


def test_app_create_advertises_typed_app_spec_schema():
    schema = AppCreateTool.definition.to_spec().parameters_schema
    app_spec_schema = _non_null_branch(schema["properties"]["app_spec"])

    assert app_spec_schema["additionalProperties"] is False
    assert {"schema_version", "app_kind", "name", "pages"}.issubset(app_spec_schema["properties"])
    page_schema = app_spec_schema["properties"]["pages"]["items"]
    section_schema = page_schema["properties"]["sections"]["items"]
    assert {"id", "kind", "variant_id", "content"}.issubset(section_schema["properties"])


# ---- app_add_section ----------------------------------------------------------


async def test_app_add_section_touches_only_affected_files():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    out = await AppAddSectionTool().run(
        AppAddSectionArgs(
            page_id="home",
            after_section_id="features",
            section={
                "id": "extra",
                "kind": "cta",
                "variant_id": "cta.banner-inline",
                "content": {"heading": "Ready?", "cta_label": "Start now"},
            },
        ),
        _ctx(sbx),
    )
    assert out.success, out.content
    touched = set(out.artifacts)
    assert touched == {
        APPSPEC_RELPATH,
        "src/App.tsx",
        "src/components/HomeExtraSection.tsx",
        "src/generated/content.ts",
        "src/generated/manifest.ts",
    }
    # design + backend files are untouched
    assert "src/styles.css" not in touched
    assert "worker/index.ts" not in touched
    assert "schema.sql" not in touched


async def test_app_add_section_rejects_wrong_kind_variant():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    out = await AppAddSectionTool().run(
        AppAddSectionArgs(
            page_id="home",
            section={"id": "x", "kind": "cta", "variant_id": "hero.full-bleed-image"},
        ),
        _ctx(sbx),
    )
    assert not out.success
    assert "layout" in out.content.lower()


async def test_app_add_section_rejects_duplicate_id():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    out = await AppAddSectionTool().run(
        AppAddSectionArgs(page_id="home", section={"id": "hero", "kind": "cta"}),
        _ctx(sbx),
    )
    assert not out.success
    assert "invalid" in out.content.lower() or "duplicate" in out.content.lower()


def test_app_add_section_advertises_typed_section_schema():
    schema = AppAddSectionTool.definition.to_spec().parameters_schema
    section_schema = schema["properties"]["section"]

    assert section_schema["additionalProperties"] is False
    assert section_schema["properties"]["kind"]["enum"] == [
        "hero",
        "features",
        "cta",
        "list",
        "form",
        "table",
        "gallery",
        "testimonials",
        "pricing",
        "faq",
        "footer",
        "custom",
    ]
    content_schema = _non_null_branch(section_schema["properties"]["content"])
    assert _schema_allows_string_array(content_schema["properties"]["items"])


# ---- app_update_content -------------------------------------------------------


def test_app_update_content_advertises_typed_items_array_schema():
    schema = AppUpdateContentTool.definition.args_model.model_json_schema()
    ref = schema["properties"]["updates"]["$ref"]
    update_schema = schema["$defs"][ref.rsplit("/", 1)[-1]]

    assert update_schema["additionalProperties"] is False
    assert _schema_allows_string_array(update_schema["properties"]["items"])
    assert "success_message" in update_schema["properties"]


async def test_app_update_content_touches_only_content_and_spec():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    before = dict(sbx._fs)
    out = await AppUpdateContentTool().run(
        AppUpdateContentArgs(
            page_id="home",
            section_id="hero",
            updates={"heading": "A brand-new headline"},
        ),
        _ctx(sbx),
    )
    assert out.success, out.content
    touched = set(out.artifacts)
    assert touched == {
        APPSPEC_RELPATH,
        "src/generated/content.ts",
        "src/generated/manifest.ts",
    }
    # the hero COMPONENT is byte-identical (it reads CONTENT by id, not embedded copy)
    assert (
        sbx._fs["src/components/HomeHeroSection.tsx"]
        == before["src/components/HomeHeroSection.tsx"]
    )
    # the new copy is in content.ts
    assert "A brand-new headline" in sbx._fs["src/generated/content.ts"].decode("utf-8")


async def test_app_update_content_unwraps_minimax_items_wrapper():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    top_level = AppUpdateContentArgs(
        page_id="home",
        section_id="features",
        updates={"item": ["Plan clearly", "Ship calmly"]},
    )

    assert top_level.updates.model_dump(mode="json", exclude_unset=True) == {
        "items": ["Plan clearly", "Ship calmly"]
    }

    out = await AppUpdateContentTool().run(
        AppUpdateContentArgs(
            page_id="home",
            section_id="features",
            updates={"items": {"item": ["Plan clearly", "Ship calmly"]}},
        ),
        _ctx(sbx),
    )

    assert out.success, out.content
    spec = json.loads(sbx._fs[APPSPEC_RELPATH].decode("utf-8"))
    features = spec["pages"][0]["sections"][1]
    assert features["content"]["items"] == ["Plan clearly", "Ship calmly"]


async def test_app_add_section_unwraps_content_items_wrapper():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success

    out = await AppAddSectionTool().run(
        AppAddSectionArgs(
            page_id="home",
            section={
                "id": "extra_features",
                "kind": "features",
                "variant_id": "features.icon-list-2col",
                "content": {"heading": "Services", "items": {"items": ["Audit", "Launch"]}},
            },
        ),
        _ctx(sbx),
    )

    assert out.success, out.content
    spec = json.loads(sbx._fs[APPSPEC_RELPATH].decode("utf-8"))
    extra = spec["pages"][0]["sections"][-1]
    assert extra["content"]["items"] == ["Audit", "Launch"]


async def test_app_update_content_refuses_empty_updates():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success

    out = await AppUpdateContentTool().run(
        AppUpdateContentArgs(page_id="home", section_id="hero", updates={}),
        _ctx(sbx),
    )

    assert not out.success
    assert out.error == "app_update_content_refused"
    assert (
        out.content == "no updates provided — set at least one of "
        "heading/subheading/body/cta_label/items/success_message"
    )


async def test_app_create_refuses_essay_brief_with_guidance():
    """Live-caught 2026-07-03: the whole build request passed as `brief` poisons
    the app name + seeded hero copy. Long briefs refuse with guidance."""
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", brief="B" * 80),
        _ctx(sbx),
    )
    assert not out.success
    assert "SHORT brand name" in out.content


async def test_app_update_content_refuses_semantic_noop_with_ground_truth():
    """RC-M, live-caught 2026-07-03: echoing the EXISTING values back must refuse
    (with the current content in the message), never report success with
    "0 file(s)" — MiniMax looped an identical no-op edit into the stuck breaker."""
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    first = await AppUpdateContentTool().run(
        AppUpdateContentArgs(
            page_id="home", section_id="hero", updates={"heading": "Same headline"}
        ),
        _ctx(sbx),
    )
    assert first.success, first.content
    repeat = await AppUpdateContentTool().run(
        AppUpdateContentArgs(
            page_id="home", section_id="hero", updates={"heading": "Same headline"}
        ),
        _ctx(sbx),
    )
    assert not repeat.success
    assert repeat.error == "app_update_content_refused"
    assert "no-op" in repeat.content
    # self-recovering: the refusal carries the CURRENT values as ground truth
    assert "Same headline" in repeat.content


async def test_app_set_design_refuses_identical_design_noop():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx, recipe="editorial-ledger")).success
    repeat = await AppSetDesignTool().run(
        AppSetDesignArgs(recipe_id="editorial-ledger"),
        _ctx(sbx),
    )
    assert not repeat.success
    assert "no-op" in repeat.content


def test_app_update_content_rejects_unknown_slot():
    with pytest.raises(ValueError, match="bogus"):
        AppUpdateContentArgs(page_id="home", section_id="hero", updates={"bogus": "x"})


# ---- app_set_design -----------------------------------------------------------


async def test_app_set_design_touches_only_design_files():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx, recipe="editorial-ledger")).success
    out = await AppSetDesignTool().run(
        AppSetDesignArgs(recipe_id="atelier-commerce", variant_policy="preserve"),
        _ctx(sbx),
    )
    assert out.success, out.content
    touched = set(out.artifacts)
    assert touched == {
        DESIGNSPEC_RELPATH,
        "src/styles.css",
        "index.html",
        "src/generated/manifest.ts",
    }
    # structure + backend untouched
    assert APPSPEC_RELPATH not in touched
    assert "worker/index.ts" not in touched
    assert "schema.sql" not in touched
    assert "src/App.tsx" not in touched


async def test_app_set_design_rejects_lint_dirty_raw_spec():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    before = dict(sbx._fs)
    # a schema-VALID DesignSpec whose generated CSS is design-slop (AI-purple primary,
    # UNjustified) — the lint gate must refuse it and leave the workspace unchanged.
    dirty = {
        "schema_version": 1,
        "typography": {"heading_font": "Fraunces", "body_font": "Newsreader"},
        "palette": {"primary": "#7c3aed", "surface": "#ffffff", "text": "#111111"},
        "layout_family": "centered",
        "component_style": "flat",
        "density": "comfortable",
        "justifications": [],
    }
    out = await AppSetDesignTool().run(
        AppSetDesignArgs(design_spec=dirty, variant_policy="preserve"), _ctx(sbx)
    )
    assert not out.success
    assert out.error == "app_set_design_refused"
    assert "slop" in out.content.lower()
    # nothing was written — the designspec on disk is the original
    assert sbx._fs[DESIGNSPEC_RELPATH] == before[DESIGNSPEC_RELPATH]
    assert sbx._fs["src/styles.css"] == before["src/styles.css"]


async def test_app_set_design_requires_exactly_one_source():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    out = await AppSetDesignTool().run(AppSetDesignArgs(), _ctx(sbx))
    assert not out.success
    assert "exactly one" in out.content.lower()


async def test_app_set_design_recipe_policy_reassigns_variants():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx, recipe="editorial-ledger")).success
    out = await AppSetDesignTool().run(
        AppSetDesignArgs(recipe_id="atelier-commerce", variant_policy="recipe"),
        _ctx(sbx),
    )
    assert out.success, out.content
    # reassigning variants mutates the appspec too
    assert APPSPEC_RELPATH in set(out.artifacts)
    spec = json.loads(sbx._fs[APPSPEC_RELPATH].decode("utf-8"))
    hero = spec["pages"][0]["sections"][0]
    # atelier-commerce prefers hero.full-bleed-image for the hero kind
    assert hero["variant_id"] == "hero.full-bleed-image"


def test_app_set_design_advertises_typed_design_spec_schema():
    schema = AppSetDesignTool.definition.to_spec().parameters_schema
    design_schema = _non_null_branch(schema["properties"]["design_spec"])

    assert design_schema["additionalProperties"] is False
    assert {"typography", "palette", "layout_family", "component_style", "density"}.issubset(
        design_schema["properties"]
    )
    assert {"heading_font", "body_font"}.issubset(
        design_schema["properties"]["typography"]["properties"]
    )
    assert {"primary", "surface", "text"}.issubset(
        design_schema["properties"]["palette"]["properties"]
    )


# ---- app_snapshot_version (EPIC H3) -------------------------------------------


async def _snapshot(sbx, **kw):
    return await AppSnapshotVersionTool().run(AppSnapshotVersionArgs(**kw), _ctx(sbx))


async def test_app_snapshot_version_records_a_clean_in_sync_version():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    out = await _snapshot(sbx, label="checkpoint")
    assert out.success, out.content
    assert out.structured is not None
    # an immutable record landed under .disco/app_snapshots/
    rel = out.structured["path"]
    assert rel.startswith(".disco/app_snapshots/") and rel.endswith(".json")
    assert rel in set(sbx._fs)
    # fresh from app_create → the tree exactly matches the specs (no drift)
    drift = out.structured["drift"]
    assert drift["clean"] is True
    assert drift["modified"] == [] and drift["missing"] == []
    # the record is well-formed JSON carrying both digests + the summary
    record = json.loads(sbx._fs[rel].decode("utf-8"))
    assert record["spec_digest"] == out.structured["spec_digest"]
    assert record["tree_digest"] == out.structured["tree_digest"]
    assert record["summary"]["valid"] is True
    assert record["label"] == "checkpoint"
    assert record["file_count"] == len(record["files"])


async def test_app_snapshot_version_detects_modified_and_missing_drift():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    # hand-edit a generated file (the anti-pattern the snapshot is meant to catch)
    sbx._fs["src/styles.css"] = b"/* hand-edited, off-spec */\n"
    # and delete another generated file outright
    del sbx._fs["src/App.tsx"]
    out = await _snapshot(sbx)
    assert out.success, out.content
    drift = out.structured["drift"]
    assert drift["clean"] is False
    assert "src/styles.css" in drift["modified"]
    assert "src/App.tsx" in drift["missing"]


async def test_app_snapshot_version_refuses_without_an_app():
    sbx = FakeSandboxInstance()
    out = await _snapshot(sbx)
    assert not out.success
    assert out.error == "app_snapshot_version_refused"


async def test_app_snapshot_version_same_second_never_overwrites(monkeypatch):
    """Two checkpoints of UNCHANGED specs within the same second must each yield a
    distinct immutable record — the first is NEVER overwritten (P1 invariant)."""
    import disco.tools.builtin.app_kit as app_kit

    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success

    # Freeze the clock so both snapshots compute the SAME base id (same second, and
    # unchanged specs → identical digest). Only collision-disambiguation can separate
    # them into two files.
    from datetime import UTC, datetime

    fixed = datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC)

    class _FrozenClock:
        @staticmethod
        def now(tz=None):
            return fixed

    monkeypatch.setattr(app_kit, "datetime", _FrozenClock)

    first = await _snapshot(sbx, label="one")
    assert first.success, first.content
    first_rel = first.structured["path"]
    first_bytes = sbx._fs[first_rel]

    second = await _snapshot(sbx, label="two")
    assert second.success, second.content
    second_rel = second.structured["path"]

    # TWO DISTINCT files exist, both present, neither overwritten.
    assert first_rel != second_rel
    assert first_rel in sbx._fs and second_rel in sbx._fs
    # the FIRST record's bytes are unchanged (not clobbered) and still carry its label
    assert sbx._fs[first_rel] == first_bytes
    assert json.loads(sbx._fs[first_rel].decode("utf-8"))["label"] == "one"
    assert json.loads(sbx._fs[second_rel].decode("utf-8"))["label"] == "two"
    # the disambiguated id shares the base and carries a deterministic collision suffix
    assert (
        first.structured["version"]
        == "20260623T120000Z-" + (first.structured["spec_digest"].split(":", 1)[-1][:12])
    )
    assert second.structured["version"] == first.structured["version"] + "-2"

    # a THIRD same-second call disambiguates again (-3), still no overwrite
    third = await _snapshot(sbx, label="three")
    assert third.success
    assert third.structured["version"].endswith("-3")
    assert sbx._fs[first_rel] == first_bytes


async def test_app_snapshot_version_does_not_touch_specs_or_tree():
    sbx = FakeSandboxInstance()
    assert (await _create(sbx)).success
    before = dict(sbx._fs)
    out = await _snapshot(sbx)
    assert out.success
    # only the new version record was added; every pre-existing file is byte-identical
    new_keys = set(sbx._fs) - set(before)
    assert new_keys == {out.structured["path"]}
    for path, data in before.items():
        assert sbx._fs[path] == data
