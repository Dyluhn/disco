"""Epic F5.3-lite — `seo` through the real `app_add_primitive` tool interface.

On a lead_gen app created via AppCreateTool (recipe editorial-ledger):
  * happy path — the folded seo regenerates the tree: the meta/OG/JSON-LD head
    block lands in the WRITTEN index.html and public/robots.txt +
    public/sitemap.xml appear in the written tree;
  * provenance — the applied spec is persisted at .disco/primitives/seo.json;
  * an invalid spec (bad base_url / unknown key) is refused with the expected
    schema carried in the refusal (self-recovering);
  * an identical re-apply is a loud no-op refusal (free from app_add_primitive).
"""

from __future__ import annotations

import json

from disco.core.appkit import APPSPEC_RELPATH
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_SEO_SPEC = {
    "site_description": "Independent studio publishing careful work.",
    "base_url": "https://acme.example",
    "social_image_url": "https://acme.example/og.png",
}


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-f53-seo",
    )


async def _create_lead_gen_app(sbx: FakeSandboxInstance):
    return await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="lead_gen", brief="Acme Studio"),
        _ctx(sbx),
    )


async def _add_seo(sbx: FakeSandboxInstance, spec: dict):
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="seo", spec=spec), _ctx(sbx)
    )


async def test_add_seo_happy_path_writes_files_and_provenance():
    sbx = FakeSandboxInstance()
    created = await _create_lead_gen_app(sbx)
    assert created.success is True, created.content
    before = sbx._fs["index.html"].decode("utf-8")
    assert "og:title" not in before
    assert "public/robots.txt" not in sbx._fs

    out = await _add_seo(sbx, _SEO_SPEC)
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["tier"] == "fillable"
    assert "public/robots.txt" in out.structured["files_written"]
    assert "public/sitemap.xml" in out.structured["files_written"]

    # the folded seo is VISIBLE in the regenerated, WRITTEN tree
    index = sbx._fs["index.html"].decode("utf-8")
    assert '<meta name="description" content="Independent studio' in index
    assert '<meta property="og:title" content="Acme Studio" />' in index
    assert '<meta property="og:url" content="https://acme.example/" />' in index
    assert '<meta property="og:image" content="https://acme.example/og.png" />' in index
    assert '<script type="application/ld+json">' in index
    robots = sbx._fs["public/robots.txt"].decode("utf-8")
    assert "Sitemap: https://acme.example/sitemap.xml" in robots
    sitemap = sbx._fs["public/sitemap.xml"].decode("utf-8")
    assert "<loc>https://acme.example/</loc>" in sitemap

    # the AppSpec was re-saved with the folded seo
    app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
    assert app_data["seo"]["base_url"] == "https://acme.example"

    # provenance record persisted
    record = json.loads(sbx._fs[".disco/primitives/seo.json"])
    assert record["primitive_id"] == "seo"
    assert record["tier"] == "fillable"
    assert record["spec"]["site_description"] == _SEO_SPEC["site_description"]
    assert record["spec"]["base_url"] == _SEO_SPEC["base_url"]


async def test_invalid_seo_spec_refused_with_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    # bad base_url (no scheme/host)
    out = await _add_seo(sbx, {**_SEO_SPEC, "base_url": "acme.example"})
    assert out.success is False
    assert "invalid 'seo' spec" in out.content
    assert "Expected schema" in out.content
    assert "base_url" in out.content
    # unknown key (extra="forbid")
    out2 = await _add_seo(sbx, {**_SEO_SPEC, "keywords": "spam"})
    assert out2.success is False
    assert "Expected schema" in out2.content
    # nothing was written on refusal
    assert "public/robots.txt" not in sbx._fs


async def test_identical_seo_reapply_is_loud_noop():
    sbx = FakeSandboxInstance()
    assert (await _create_lead_gen_app(sbx)).success is True
    first = await _add_seo(sbx, _SEO_SPEC)
    assert first.success is True, first.content
    again = await _add_seo(sbx, _SEO_SPEC)
    assert again.success is False
    assert "no-op" in again.content
    # a DIFFERENT spec goes through
    changed = await _add_seo(sbx, {**_SEO_SPEC, "site_name": "Acme Studio Co"})
    assert changed.success is True, changed.content
    assert '<meta property="og:title" content="Acme Studio Co" />' in sbx._fs["index.html"].decode(
        "utf-8"
    )
