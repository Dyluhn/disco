"""Epic 5.3-lite — the `blog` AppKit primitive.

Covers spec validation, AppSpec folding, shared generator lowering, RSS/sitemap
interplay with SEO, safe Markdown rendering, registration, and the WO-A3 verify hook.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest
from disco.core.appkit import default_directory_app_spec, default_lead_gen_app_spec, get_recipe
from disco.core.appkit.blog_primitive import (
    BLOG_PRIMITIVE_ID,
    BlogSpec,
    apply_blog_spec,
    blog_routes_for,
    blog_verify,
    generate_blog,
)
from disco.core.appkit.hello_primitive import default_hello_app_spec
from disco.core.appkit.primitives import get_primitive
from disco.core.appkit.recipes import SiteRecipe
from disco.core.appkit.seo_primitive import SeoSpec, apply_seo_spec
from disco.core.appkit.spec import AppSpec, BlogMeta, DesignSpec, Page, Section
from pydantic import ValidationError


def _recipe() -> SiteRecipe:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design() -> DesignSpec:
    return _recipe().to_design_spec()


def _app() -> AppSpec:
    return default_lead_gen_app_spec("Acme Studio", _recipe())


def _spec(**overrides: object) -> BlogSpec:
    payload: dict[str, object] = {
        "index_page_title": "Field Notes",
        "posts": [
            {
                "slug": "older-note",
                "title": "Older Note",
                "date": "2026-06-01",
                "summary": "A first note.",
                "body_md": "Hello **there**.\n\n- One\n- Two",
            },
            {
                "slug": "newer-note",
                "title": "Newer Note",
                "date": "2026-07-07",
                "body_md": "## Update\n\nRead [more](/about).",
            },
        ],
    }
    payload.update(overrides)
    return BlogSpec.model_validate(payload)


# ---- spec validation ------------------------------------------------------------


def test_blog_spec_validation_refuses_bad_shape() -> None:
    with pytest.raises(ValidationError):
        _spec(posts=[])
    with pytest.raises(ValidationError):
        _spec(posts=[{"slug": "Bad Slug", "title": "x", "date": "2026-07-07", "body_md": "x"}])
    with pytest.raises(ValidationError):
        _spec(posts=[{"slug": "bad-date", "title": "x", "date": "07/07/2026", "body_md": "x"}])
    with pytest.raises(ValidationError):
        _spec(posts=[{"slug": "blank", "title": "x", "date": "2026-07-07", "body_md": "  "}])
    with pytest.raises(ValidationError):
        _spec(unknown_key=True)
    with pytest.raises(ValidationError, match="duplicate blog post slug"):
        _spec(
            posts=[
                {"slug": "same", "title": "One", "date": "2026-07-07", "body_md": "x"},
                {"slug": "same", "title": "Two", "date": "2026-07-08", "body_md": "y"},
            ]
        )


def test_blogmeta_roundtrips_and_rejects_duplicate_slugs() -> None:
    meta = BlogMeta.model_validate(_spec().model_dump(mode="json"))
    assert meta.posts[0].slug == "older-note"
    with pytest.raises(ValidationError):
        BlogMeta.model_validate(
            {
                "posts": [
                    {"slug": "same", "title": "One", "date": "2026-07-07", "body_md": "x"},
                    {"slug": "same", "title": "Two", "date": "2026-07-08", "body_md": "y"},
                ]
            }
        )


# ---- fold semantics -------------------------------------------------------------


def test_apply_blog_spec_folds_posts_newest_first() -> None:
    folded = apply_blog_spec(_app(), _spec())
    assert folded.blog is not None
    assert [post.slug for post in folded.blog.posts] == ["newer-note", "older-note"]
    assert folded.blog.index_page_title == "Field Notes"
    assert blog_routes_for(folded) == ("/blog", "/blog/newer-note", "/blog/older-note")


def test_apply_blog_spec_rejects_route_collision() -> None:
    app = AppSpec(
        schema_version=1,
        app_kind="lead_gen",
        name="Acme",
        pages=(
            Page(id="home", route="/", title="Home"),
            Page(id="blog", route="/blog", title="Blog"),
        ),
    )
    with pytest.raises(ValueError, match="/blog"):
        apply_blog_spec(app, _spec())


def test_apply_blog_spec_rejects_unsupported_stub_hosts() -> None:
    with pytest.raises(ValueError, match="shared React AppKit hosts"):
        apply_blog_spec(default_hello_app_spec("Hello", _recipe()), _spec())


def test_apply_blog_spec_rejects_wrong_spec_type() -> None:
    with pytest.raises(TypeError, match="needs a BlogSpec"):
        apply_blog_spec(_app(), Section(id="not_blog", kind="custom"))


def test_identical_reapply_returns_equal_app() -> None:
    once = apply_blog_spec(_app(), _spec())
    twice = apply_blog_spec(once, _spec())
    assert twice.model_dump(mode="json") == once.model_dump(mode="json")


# ---- generated files ------------------------------------------------------------


def test_blog_generates_routes_components_rss_and_safe_markdown() -> None:
    spec = _spec(
        posts=[
            {
                "slug": "safe-markdown",
                "title": "Safe Markdown",
                "date": "2026-07-07",
                "summary": "Escaped safely.",
                "body_md": (
                    "# Heading\n\n"
                    "A <script>alert(1)</script> tag, **bold**, `code`, "
                    "[safe](/about), and [blocked](javascript:evil).\n\n"
                    "1. First\n2. Second"
                ),
            }
        ]
    )
    app = apply_blog_spec(_app(), spec)
    tree = get_primitive("lead_gen").generate(app, _design())  # type: ignore[union-attr]

    assert "src/blog/BlogIndexPage.tsx" in tree
    assert "src/blog/BlogSafeMarkdownPostPage.tsx" in tree
    assert "src/generated/blog.ts" in tree
    assert '"/blog/safe-markdown"' in tree["src/App.tsx"]
    assert "dangerouslySetInnerHTML" not in tree["src/blog/BlogRichText.tsx"]
    blog_data = tree["src/generated/blog.ts"]
    assert "Safe Markdown" in blog_data
    assert "javascript:evil" not in blog_data
    assert '"kind": "strong"' in blog_data
    assert '"kind": "ol"' in blog_data

    rss = ET.fromstring(tree["public/rss.xml"])
    assert rss.findtext("./channel/item/title") == "Safe Markdown"
    assert rss.findtext("./channel/item/link") == "/blog/safe-markdown"


def test_blog_rss_and_sitemap_use_seo_base_url_when_present() -> None:
    app = apply_blog_spec(_app(), _spec())
    app = apply_seo_spec(
        app,
        SeoSpec(
            site_description="Independent studio publishing careful work.",
            base_url="https://example.com/",
        ),
    )
    tree = get_primitive("lead_gen").generate(app, _design())  # type: ignore[union-attr]
    rss = ET.fromstring(tree["public/rss.xml"])
    links = [node.text for node in rss.findall("./channel/item/link")]
    assert links == [
        "https://example.com/blog/newer-note",
        "https://example.com/blog/older-note",
    ]

    locs = re.findall(r"<loc>([^<]+)</loc>", tree["public/sitemap.xml"])
    assert "https://example.com/" in locs
    assert "https://example.com/blog" in locs
    assert "https://example.com/blog/newer-note" in locs
    assert "https://example.com/blog/older-note" in locs
    assert not any("example.com//" in loc for loc in locs)


def test_blog_routes_work_on_directory_host() -> None:
    app = apply_blog_spec(default_directory_app_spec("Town Directory", _recipe()), _spec())
    tree = get_primitive("directory").generate(app, _design())  # type: ignore[union-attr]
    assert '"/directory": Page1' in tree["src/App.tsx"]
    assert '"/blog/newer-note"' in tree["src/App.tsx"]
    assert "public/rss.xml" in tree


# ---- verify hook + registration -------------------------------------------------


def test_blog_verify_passes_and_fails_structurally() -> None:
    app = apply_blog_spec(_app(), _spec())
    tree = get_primitive("lead_gen").generate(app, _design())  # type: ignore[union-attr]
    res = blog_verify(app, _design(), tree)
    assert res.ok, [check for check in res.checks if not check.passed]
    assert [check.name for check in res.checks] == [
        "blog_spec_present",
        "blog_index_lists_posts",
        "blog_post_routes",
        "blog_rss_xml",
    ]

    broken = {k: v for k, v in tree.items() if k != "public/rss.xml"}
    failed = blog_verify(app, _design(), broken)
    assert not failed.ok
    assert any(check.name == "blog_rss_xml" and not check.passed for check in failed.checks)


def test_blog_primitive_registered_addable_and_verified() -> None:
    prim = get_primitive(BLOG_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "fillable"
    assert prim.host_contract == ()
    assert prim.spec_schema is BlogSpec
    assert prim.verify is blog_verify
    assert prim.apply_spec is apply_blog_spec
    with pytest.raises(ValueError, match="ADD-ON"):
        prim.default_app_spec("Blog", _recipe())
    with pytest.raises(ValueError, match="does not generate"):
        generate_blog(_app(), _design())
