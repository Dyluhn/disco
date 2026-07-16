"""H283 — ``local_list`` selection through the real app_create boundary."""

from __future__ import annotations

import json

from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    get_primitive,
    primitive_ids,
)
from disco.core.appkit.spec import AppSpec, Entity, EntityField, Page, Section, SectionContent
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_ADDABLE_SPECS: dict[str, dict[str, object]] = {
    "analytics": {},
    "blog": {
        "posts": [
            {
                "slug": "first-note",
                "title": "First note",
                "date": "2026-07-15",
                "body_md": "A deliberately small first note.",
            }
        ]
    },
    "collection": {
        "collection_id": "favorites",
        "title": "Favorites",
        "items": [{"label": "One"}],
    },
    "feature_flags": {"flags": [{"key": "beta", "enabled": True}]},
    "form": {
        "form_id": "entry",
        "title": "Entry",
        "fields": [{"name": "name", "label": "Name", "kind": "text", "required": True}],
    },
    "hello": {"headline": "A better list"},
    "seo": {
        "site_description": "A small persistent browser-local notes list.",
        "base_url": "https://notes.example",
    },
    "stripe": {
        "plan_name": "Pro",
        "price_display": "$9/mo",
        "entitlement_flag": "pro_member",
        "success_message": "Welcome to Pro.",
    },
    "webhook": {
        "endpoint_id": "events",
        "direction": "inbound",
        "event_types": ["item.created"],
    },
}


def _ctx(sandbox: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-h283",
    )


async def test_app_create_selects_local_list_and_writes_owned_vite_tree():
    sandbox = FakeSandboxInstance()
    outcome = await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            primitive_id="local_list",
            brief="Pocket Notes",
        ),
        _ctx(sandbox),
    )
    assert outcome.success, outcome.content
    assert outcome.structured["primitive_id"] == "local_list"
    assert outcome.structured["app_kind"] == "local_list"
    assert APPSPEC_RELPATH in sandbox._fs
    assert DESIGNSPEC_RELPATH in sandbox._fs
    assert not any(path.startswith(".disco/primitives/") for path in sandbox._fs)
    spec = json.loads(sandbox._fs[APPSPEC_RELPATH])
    assert spec["app_kind"] == "local_list"
    assert spec["entities"] == []
    assert "package-lock.json" in sandbox._fs
    assert ".dev.vars.example" not in sandbox._fs
    component = next(
        body.decode("utf-8")
        for path, body in sandbox._fs.items()
        if path.startswith("src/components/")
    )
    assert "window.localStorage.getItem(STORAGE_KEY)" in component
    assert "onClick={() => deleteItem(index)}" in component


async def test_app_create_local_list_refuses_explicit_server_widening_without_crashing():
    sandbox = FakeSandboxInstance()
    app = AppSpec(
        schema_version=1,
        app_kind="local_list",
        name="Not Local",
        pages=(
            Page(
                id="home",
                route="/",
                title="Home",
                sections=(
                    Section(
                        id="local_list",
                        kind="list",
                        content=SectionContent(heading="Not Local"),
                    ),
                ),
            ),
        ),
        entities=(
            Entity(
                id="note",
                name="Note",
                fields=(EntityField(name="text", type="str", required=True),),
            ),
        ),
    )
    outcome = await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            app_spec=app.model_dump(mode="json"),
        ),
        _ctx(sandbox),
    )
    assert not outcome.success
    assert outcome.error == "app_create_refused"
    assert "browser-local" in outcome.content
    assert APPSPEC_RELPATH not in sandbox._fs


def test_app_create_schema_advertises_local_list_before_first_call():
    schema = AppCreateArgs.model_json_schema()
    primitive = schema["properties"]["primitive_id"]
    assert "local_list" in primitive["description"]
    app_spec = schema["properties"]["app_spec"]
    assert "local_list" in app_spec["description"]


async def test_local_list_refuses_unsupported_seo_add_on_without_writes():
    sandbox = FakeSandboxInstance()
    created = await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            primitive_id="local_list",
            brief="Pocket Notes",
        ),
        _ctx(sandbox),
    )
    assert created.success, created.content
    before = dict(sandbox._fs)

    outcome = await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(
            primitive_id="seo",
            spec={
                "site_description": "A small persistent browser-local notes list.",
                "base_url": "https://notes.example",
            },
        ),
        _ctx(sandbox),
    )

    assert not outcome.success
    assert outcome.error == "app_add_primitive_refused"
    assert "local_list" in outcome.content and "does not support" in outcome.content
    assert "seo" in outcome.content
    assert sandbox._fs == before
    assert ".disco/primitives/seo.json" not in sandbox._fs


async def test_local_list_refuses_stripe_as_typed_outcome_without_writes():
    sandbox = FakeSandboxInstance()
    created = await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            primitive_id="local_list",
            brief="Pocket Notes",
        ),
        _ctx(sandbox),
    )
    assert created.success, created.content
    before = dict(sandbox._fs)

    outcome = await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="stripe", spec=_ADDABLE_SPECS["stripe"]),
        _ctx(sandbox),
    )

    assert not outcome.success
    assert outcome.error == "app_add_primitive_refused"
    assert "stripe" in outcome.content and "local_list" in outcome.content
    assert sandbox._fs == before
    assert ".disco/primitives/stripe.json" not in sandbox._fs


async def test_every_registered_add_on_has_explicit_local_list_compatibility():
    registered_addable = {
        primitive_id
        for primitive_id in primitive_ids()
        if (primitive := get_primitive(primitive_id)) is not None
        # Other tool-test modules register synthetic definitions into the shared
        # process registry at collection time. Inventory the built-in definitions,
        # whose scaffold owners live in the production appkit package, so this
        # exhaustive compatibility proof is independent of pytest collection order.
        and primitive.default_app_spec.__module__.startswith("disco.core.appkit.")
        and primitive.spec_schema is not None
        and primitive.apply_spec is not None
    }
    assert set(_ADDABLE_SPECS) == registered_addable

    for primitive_id in sorted(registered_addable):
        cases = (
            ("valid", _ADDABLE_SPECS[primitive_id]),
            # Every built-in schema forbids this unknown field. Seeing the SAME
            # host refusal proves compatibility is decided before spec validation.
            ("invalid", {"definitely_not_a_primitive_field": True}),
        )
        for case, spec in cases:
            sandbox = FakeSandboxInstance()
            created = await AppCreateTool().run(
                AppCreateArgs(
                    recipe_id="editorial-ledger",
                    primitive_id="local_list",
                    brief="Pocket Notes",
                ),
                _ctx(sandbox),
            )
            assert created.success, (primitive_id, case, created.content)
            before = dict(sandbox._fs)
            before_generated = {
                path: body for path, body in before.items() if not path.startswith(".disco/")
            }

            outcome = await AppAddPrimitiveTool().run(
                AppAddPrimitiveArgs(primitive_id=primitive_id, spec=spec),
                _ctx(sandbox),
            )

            assert not outcome.success, (primitive_id, case)
            assert outcome.error == "app_add_primitive_refused", (primitive_id, case)
            assert "local_list" in outcome.content and primitive_id in outcome.content
            assert "invalid" not in outcome.content, (primitive_id, case, outcome.content)
            assert sandbox._fs == before, (primitive_id, case)
            assert {
                path: body for path, body in sandbox._fs.items() if not path.startswith(".disco/")
            } == before_generated
            assert f".disco/primitives/{primitive_id}.json" not in sandbox._fs
