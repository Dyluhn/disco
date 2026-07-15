"""Epic 5.3-lite — `blog` through the real `app_add_primitive` tool interface."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from disco.core.appkit import APPSPEC_RELPATH
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_BLOG_SPEC = {
    "index_page_title": "Field Notes",
    "posts": [
        {
            "slug": "launch-note",
            "title": "Launch Note",
            "date": "2026-07-07",
            "summary": "What changed.",
            "body_md": "## Launch\n\nA short **update**.",
        },
        {
            "slug": "archive-note",
            "title": "Archive Note",
            "date": "2026-06-01",
            "body_md": "Older context.",
        },
    ],
}


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-f53-blog",
    )


async def _create_lead_gen_app(sbx: FakeSandboxInstance):
    return await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="lead_gen", brief="Acme Studio"),
        _ctx(sbx),
    )


async def _add_blog(sbx: FakeSandboxInstance, spec: dict):
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="blog", spec=spec), _ctx(sbx)
    )


async def test_add_blog_happy_path_writes_files_and_provenance():
    sbx = FakeSandboxInstance()
    created = await _create_lead_gen_app(sbx)
    assert created.success is True, created.content
    assert "public/rss.xml" not in sbx._fs

    out = await _add_blog(sbx, _BLOG_SPEC)
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["tier"] == "fillable"
    assert "public/rss.xml" in out.structured["files_written"]
    assert "src/generated/blog.ts" in out.structured["files_written"]
    assert "src/blog/BlogIndexPage.tsx" in out.structured["files_written"]

    app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
    assert app_data["blog"]["index_page_title"] == "Field Notes"
    assert [post["slug"] for post in app_data["blog"]["posts"]] == [
        "launch-note",
        "archive-note",
    ]

    app_tsx = sbx._fs["src/App.tsx"].decode("utf-8")
    assert '"/blog": BlogIndexPage' in app_tsx
    assert '"/blog/launch-note"' in app_tsx
    rss = ET.fromstring(sbx._fs["public/rss.xml"].decode("utf-8"))
    assert [node.text for node in rss.findall("./channel/item/title")] == [
        "Launch Note",
        "Archive Note",
    ]

    record = json.loads(sbx._fs[".disco/primitives/blog.json"])
    assert record["primitive_id"] == "blog"
    assert record["tier"] == "fillable"
    assert record["spec"]["posts"][0]["slug"] == "launch-note"


async def test_invalid_blog_spec_refused_with_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    out = await _add_blog(
        sbx,
        {
            "posts": [
                {
                    "slug": "Bad Slug",
                    "title": "Bad",
                    "date": "2026-07-07",
                    "body_md": "x",
                }
            ]
        },
    )
    assert out.success is False
    assert "invalid 'blog' spec" in out.content
    assert "Expected schema" in out.content
    assert "slug" in out.content
    assert "public/rss.xml" not in sbx._fs


async def test_identical_blog_reapply_is_loud_noop_and_changed_spec_updates():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    first = await _add_blog(sbx, _BLOG_SPEC)
    assert first.success is True, first.content
    again = await _add_blog(sbx, _BLOG_SPEC)
    assert again.success is False
    assert "no-op" in again.content

    changed = await _add_blog(sbx, {**_BLOG_SPEC, "index_page_title": "Journal"})
    assert changed.success is True, changed.content
    blog_data = sbx._fs["src/generated/blog.ts"].decode("utf-8")
    assert 'export const BLOG_INDEX_TITLE = "Journal";' in blog_data
