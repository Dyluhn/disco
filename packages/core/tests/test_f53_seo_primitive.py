"""Epic F5.3-lite — the `seo` AppKit primitive.

Covers:
  * `SeoSpec` validation — bad/host-less/non-http URLs refused, oversize +
    empty description refused, unknown keys refused (extra="forbid");
  * `SeoMeta` on the AppSpec mirrors the same URL checks, and the field is
    STRICTLY additive: a None `seo` never appears in dumps, so existing spec
    serialization is unaffected (and round-trips preserve a set `seo`);
  * fold correctness — `apply_seo_spec` populates `app.seo`; different values
    yield a CHANGED app; a wrong spec type raises;
  * emitters (SHARED path, all three verticals): meta description / OG tags /
    JSON-LD present and properly ESCAPED; JSON-LD parses via `json.loads` and
    round-trips the raw values; og:image only when set; `public/sitemap.xml`
    lists every page as an absolute URL with no double-slash joins;
    `public/robots.txt` allows all + points at the sitemap;
  * the HARD constraint — when `AppSpec.seo` is None, all three default trees
    (plus hello) are BYTE-IDENTICAL to the pre-F5.3 generator output, pinned by
    tree-level sha256 hashes captured at base commit a619d8f8 BEFORE the emitter
    edit;
  * hello compatibility — seo folded onto a hello app changes only the spec
    (hello's generate ignores it, tree byte-identical);
  * the standalone `seo` primitive registration stub (registered, addable,
    fillable, verify=None, minimal generate that ignores `app.seo`).
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from disco.core.appkit import get_recipe
from disco.core.appkit.primitives import (
    DIRECTORY_PRIMITIVE_ID,
    HELLO_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    RECORDS_PRIMITIVE_ID,
    SEO_PRIMITIVE_ID,
    get_primitive,
    resolve_primitive,
)
from disco.core.appkit.recipes import SiteRecipe
from disco.core.appkit.seo_primitive import SeoSpec, apply_seo_spec, generate_seo
from disco.core.appkit.spec import (
    AppSpec,
    DesignSpec,
    SeoMeta,
    load_app_spec_from_bytes,
    serialize_app_spec,
)
from pydantic import ValidationError


def _recipe() -> SiteRecipe:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _design() -> DesignSpec:
    return _recipe().to_design_spec()


def _default_app(prim_id: str, name: str = "Acme Studio") -> AppSpec:
    prim = resolve_primitive(prim_id)
    assert prim.id == prim_id
    return prim.default_app_spec(name, _recipe())


def _seo_spec(**overrides: object) -> SeoSpec:
    base: dict[str, object] = {
        "site_description": "Independent studio publishing careful work.",
        "base_url": "https://example.com",
    }
    base.update(overrides)
    return SeoSpec.model_validate(base)


def _with_seo(app: AppSpec, **overrides: object) -> AppSpec:
    return apply_seo_spec(app, _seo_spec(**overrides))


_VERTICALS = [LEAD_GEN_PRIMITIVE_ID, DIRECTORY_PRIMITIVE_ID, RECORDS_PRIMITIVE_ID]


# ---- 1. SeoSpec validation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "example.com",  # no scheme
        "ftp://example.com",  # wrong scheme
        "https://",  # no host
        "http://",  # no host
        "javascript:alert(1)",  # hostile scheme
        "//example.com",  # protocol-relative
        "",
    ],
)
def test_seospec_bad_base_url_refused(bad_url: str) -> None:
    with pytest.raises(ValidationError):
        _seo_spec(base_url=bad_url)


@pytest.mark.parametrize("bad_url", ["og.png", "ftp://x.com/a.png", "https://"])
def test_seospec_bad_social_image_url_refused(bad_url: str) -> None:
    with pytest.raises(ValidationError):
        _seo_spec(social_image_url=bad_url)


def test_seospec_none_optionals_ok() -> None:
    spec = _seo_spec()
    assert spec.social_image_url is None
    assert spec.site_name is None


def test_seospec_oversize_and_empty_refused() -> None:
    with pytest.raises(ValidationError):
        _seo_spec(site_description="x" * 301)
    with pytest.raises(ValidationError):
        _seo_spec(site_description="")
    with pytest.raises(ValidationError):
        _seo_spec(base_url="https://example.com/" + "a" * 2050)
    with pytest.raises(ValidationError):
        _seo_spec(site_name="")


def test_seospec_unknown_key_refused() -> None:
    with pytest.raises(ValidationError):
        _seo_spec(keywords="spam, spam")  # extra="forbid"


# ---- 2. SeoMeta / AppSpec: strictly additive ---------------------------------------


def test_seometa_mirrors_url_checks() -> None:
    with pytest.raises(ValidationError):
        SeoMeta(site_description="d", base_url="example.com")
    with pytest.raises(ValidationError):
        SeoMeta(site_description="d", base_url="https://x.com", social_image_url="nope")
    meta = SeoMeta(site_description="d", base_url="https://x.com")
    assert meta.site_name is None


def test_none_seo_absent_from_dump_and_serialization() -> None:
    app = _default_app(LEAD_GEN_PRIMITIVE_ID)
    assert app.seo is None
    assert "seo" not in app.model_dump(mode="json")
    assert '"seo"' not in serialize_app_spec(app)


def test_seo_survives_serialize_roundtrip() -> None:
    app = _with_seo(
        _default_app(DIRECTORY_PRIMITIVE_ID),
        social_image_url="https://example.com/og.png",
        site_name="Acme",
    )
    loaded = load_app_spec_from_bytes(serialize_app_spec(app))
    assert loaded.seo is not None
    assert loaded.seo.base_url == "https://example.com"
    assert loaded.seo.social_image_url == "https://example.com/og.png"
    assert loaded.seo.site_name == "Acme"
    assert loaded.model_dump(mode="json") == app.model_dump(mode="json")


# ---- 3. fold correctness ------------------------------------------------------------


def test_apply_seo_spec_folds_and_changes_on_different_values() -> None:
    app = _default_app(LEAD_GEN_PRIMITIVE_ID)
    folded = _with_seo(app)
    assert folded.seo is not None
    assert folded.seo.site_description == "Independent studio publishing careful work."
    assert folded.model_dump(mode="json") != app.model_dump(mode="json")
    # re-apply with DIFFERENT values = a changed app (the no-op refusal for an
    # IDENTICAL re-apply is app_add_primitive's dump-equality check, not ours)
    refolded = apply_seo_spec(folded, _seo_spec(base_url="https://other.example"))
    assert refolded.seo is not None
    assert refolded.seo.base_url == "https://other.example"
    assert refolded.model_dump(mode="json") != folded.model_dump(mode="json")


def test_apply_seo_spec_rejects_wrong_spec_type() -> None:
    from disco.core.appkit.hello_primitive import HelloSpec

    with pytest.raises(TypeError):
        apply_seo_spec(_default_app(LEAD_GEN_PRIMITIVE_ID), HelloSpec(headline="x"))


# ---- 4. emitted head block (shared index.html path) --------------------------------


@pytest.mark.parametrize("prim_id", _VERTICALS)
def test_head_block_present_for_all_verticals(prim_id: str) -> None:
    app = _with_seo(
        _default_app(prim_id),
        social_image_url="https://example.com/og.png",
        site_name="Acme Studio Co",
    )
    index = resolve_primitive(prim_id).generate(app, _design())["index.html"]
    assert '<meta name="description" content="Independent studio' in index
    assert '<meta property="og:title" content="Acme Studio Co" />' in index
    assert '<meta property="og:description" content="Independent studio' in index
    assert '<meta property="og:type" content="website" />' in index
    assert '<meta property="og:url" content="https://example.com/" />' in index
    assert '<meta property="og:image" content="https://example.com/og.png" />' in index
    assert '<script type="application/ld+json">' in index


def test_og_image_absent_when_unset_and_site_name_defaults_to_app_name() -> None:
    app = _with_seo(_default_app(LEAD_GEN_PRIMITIVE_ID))
    index = resolve_primitive(LEAD_GEN_PRIMITIVE_ID).generate(app, _design())["index.html"]
    assert "og:image" not in index
    assert '<meta property="og:title" content="Acme Studio" />' in index


def _extract_ld(index: str) -> str:
    match = re.search(r'<script type="application/ld\+json">(.*)</script>', index)
    assert match is not None, "JSON-LD block missing"
    return match.group(1)


def test_hostile_values_escaped_and_jsonld_parses() -> None:
    hostile = 'He said "hi" & </script><script>alert(1)</script>'
    app = _with_seo(
        _default_app(LEAD_GEN_PRIMITIVE_ID, name='Acme "&" <Sons>'),
        site_description=hostile,
    )
    index = resolve_primitive(LEAD_GEN_PRIMITIVE_ID).generate(app, _design())["index.html"]
    # no raw payload anywhere in the emitted HTML
    assert "<script>alert(1)</script>" not in index
    # the JSON-LD payload can never close its own script tag or open markup
    payload = _extract_ld(index)
    assert "</" not in payload
    assert "<" not in payload
    # ... yet parses via json.loads and round-trips the RAW values
    ld = json.loads(payload)
    assert ld["@type"] == "WebSite"
    assert ld["name"] == 'Acme "&" <Sons>'
    assert ld["description"] == hostile
    assert ld["url"] == "https://example.com/"


# ---- 5. sitemap.xml + robots.txt ----------------------------------------------------


@pytest.mark.parametrize("prim_id", _VERTICALS)
def test_sitemap_lists_every_page_absolute(prim_id: str) -> None:
    app = _with_seo(_default_app(prim_id))
    tree = resolve_primitive(prim_id).generate(app, _design())
    sitemap = tree["public/sitemap.xml"]
    assert sitemap.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    assert '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' in sitemap
    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    assert locs == [f"https://example.com{p.route}" for p in app.pages]
    assert len(locs) == len(app.pages) >= 1


@pytest.mark.parametrize("base", ["https://example.com", "https://example.com/"])
def test_no_double_slash_from_trailing_slash_base(base: str) -> None:
    app = _with_seo(_default_app(DIRECTORY_PRIMITIVE_ID), base_url=base)
    tree = resolve_primitive(DIRECTORY_PRIMITIVE_ID).generate(app, _design())
    assert "example.com//" not in tree["public/sitemap.xml"]
    assert "example.com//" not in tree["public/robots.txt"]
    assert "example.com//" not in tree["index.html"]
    assert "<loc>https://example.com/directory</loc>" in tree["public/sitemap.xml"]


@pytest.mark.parametrize("prim_id", _VERTICALS)
def test_robots_allows_all_and_points_at_sitemap(prim_id: str) -> None:
    app = _with_seo(_default_app(prim_id))
    robots = resolve_primitive(prim_id).generate(app, _design())["public/robots.txt"]
    assert "User-agent: *\n" in robots
    assert "Allow: /\n" in robots
    assert "Sitemap: https://example.com/sitemap.xml\n" in robots


# ---- 6. the HARD constraint: seo=None trees are byte-identical to pre-F5.3 ---------

# Tree-level sha256 fingerprints of the DEFAULT (seo=None) generated trees,
# captured at base commit a619d8f8 BEFORE the F5.3 emitter edit. The F5.3
# guarantee is that a None `AppSpec.seo` changes NOTHING — not one byte — so
# these must keep matching. If an UNRELATED, deliberate emitter change lands,
# re-capture with the same recipe/name and update (the fingerprint is
# json.dumps of the per-file sha256 map, sort_keys=True, then sha256'd).
_BASELINE_TREE_SHA256 = {
    LEAD_GEN_PRIMITIVE_ID: "407b5177d29abebdb411256d3360b84a62b1923d3f7f8c6bbd82ee19a394854a",
    DIRECTORY_PRIMITIVE_ID: "1c1d3d177ea1c0eb1f51c87a50259eb16d9e64b601d3ff18106e00287eae5c93",
    RECORDS_PRIMITIVE_ID: "12062f7176b486dc8fdde22c39dea5d090b85de8375578b110140b53a4999fed",
    HELLO_PRIMITIVE_ID: "e8f75cbf9483e4f3b7b1af8e3b963d621e22394ac0a72a20f4428c633e0699db",
}


def _tree_fingerprint(tree: dict[str, str]) -> str:
    per_file = {
        path: hashlib.sha256(contents.encode("utf-8")).hexdigest()
        for path, contents in sorted(tree.items())
    }
    return hashlib.sha256(json.dumps(per_file, sort_keys=True).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("prim_id", sorted(_BASELINE_TREE_SHA256))
def test_none_seo_tree_is_byte_identical_to_pre_f53_output(prim_id: str) -> None:
    app = _default_app(prim_id)
    assert app.seo is None
    tree = resolve_primitive(prim_id).generate(app, _design())
    assert _tree_fingerprint(tree) == _BASELINE_TREE_SHA256[prim_id]


@pytest.mark.parametrize("prim_id", _VERTICALS)
def test_none_seo_tree_carries_no_seo_artifacts(prim_id: str) -> None:
    tree = resolve_primitive(prim_id).generate(_default_app(prim_id), _design())
    assert "public/robots.txt" not in tree
    assert "public/sitemap.xml" not in tree
    index = tree["index.html"]
    assert "og:" not in index
    assert "application/ld+json" not in index
    assert '<meta name="description"' not in index


# ---- 7. hello compatibility + the standalone stub -----------------------------------


def test_seo_on_hello_changes_spec_only() -> None:
    hello = resolve_primitive(HELLO_PRIMITIVE_ID)
    app = hello.default_app_spec("Acme Studio", _recipe())
    before = hello.generate(app, _design())
    folded = _with_seo(app)
    assert folded.seo is not None  # the fold reached the spec ...
    after = hello.generate(folded, _design())
    assert after == before  # ... and hello's generate deliberately ignores it


def test_seo_primitive_registered_addable_and_minimal() -> None:
    prim = get_primitive(SEO_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "fillable"
    assert prim.host_contract == ()
    assert prim.spec_schema is SeoSpec
    assert prim.verify is None
    assert prim.apply_spec is apply_seo_spec

    app = prim.default_app_spec("Meta Proof", _recipe())
    assert app.app_kind == SEO_PRIMITIVE_ID
    assert prim.prepare_app_spec(app) is app
    tree = generate_seo(app, _design())
    assert set(tree) == {"index.html"}
    assert "<h1>Meta Proof</h1>" in tree["index.html"]
    # the standalone stub also ignores a folded seo (documented behavior)
    assert generate_seo(_with_seo(app), _design()) == tree
