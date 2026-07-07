"""WO-A3 — the per-primitive verify hooks (pure ports) + the VerifyCheck shape.

Covers, pure (no sandbox, no browser):
  * `lead_gen_verify` over a REAL generated lead-gen tree — happy path (all six
    checks pass, in order) and missing-file failures with the tool's original
    evidence strings byte-asserted;
  * `directory_verify` over a REAL generated directory tree — happy + failures;
  * `form_verify`, `seo_verify`, and `collection_verify` over folded generated
    trees — happy + stale-output failures;
  * `hello_verify` — pass, wrong-headline fail, missing index.html fail, and the
    folded-HelloSpec subtitle contract;
  * `VerifyCheck` / `PrimitiveVerifyResult.checks` defaults (backward compatible);
  * the registry wiring: lead_gen/directory/hello carry their verify hooks,
    records stays verify=None ON PURPOSE (the lead-gen fallback preserves its
    pre-dispatch verdicts).
"""

from __future__ import annotations

from disco.core.appkit import (
    default_directory_app_spec,
    default_lead_gen_app_spec,
    ensure_lead_entity,
    generate,
    get_primitive,
    get_recipe,
)
from disco.core.appkit.blog_primitive import blog_verify
from disco.core.appkit.collection_primitive import CollectionSpec, apply_collection_spec
from disco.core.appkit.form_primitive import FormSpec, apply_form_spec, form_verify
from disco.core.appkit.hello_primitive import (
    HelloSpec,
    apply_hello_spec,
    default_hello_app_spec,
    generate_hello,
    hello_verify,
)
from disco.core.appkit.primitive_verify import (
    collection_verify,
    directory_verify,
    lead_gen_verify,
    seo_verify,
)
from disco.core.appkit.primitives import PrimitiveVerifyResult, VerifyCheck
from disco.core.appkit.seo_primitive import SeoSpec, apply_seo_spec

_RECIPE = get_recipe("editorial-ledger")


def _lead_gen_fixture():
    app = ensure_lead_entity(default_lead_gen_app_spec("Acme Leads", _RECIPE))
    design = _RECIPE.to_design_spec()
    return app, design, generate(app, design)


def _directory_fixture():
    app = default_directory_app_spec("Town Directory", _RECIPE)
    design = _RECIPE.to_design_spec()
    return app, design, generate(app, design)


def _form_fixture():
    app = ensure_lead_entity(default_lead_gen_app_spec("Acme Leads", _RECIPE))
    spec = FormSpec.model_validate(
        {
            "form_id": "quote_request",
            "title": "Request a quote",
            "fields": [{"name": "email", "label": "Email", "kind": "email", "required": True}],
            "success_message": "Thanks, we will respond shortly.",
        }
    )
    folded = apply_form_spec(app, spec)
    design = _RECIPE.to_design_spec()
    return folded, design, generate(folded, design)


def _seo_fixture():
    app = ensure_lead_entity(default_lead_gen_app_spec("Acme Leads", _RECIPE))
    spec = SeoSpec(
        site_description="Careful lead capture for growing teams.",
        base_url="https://example.com",
        site_name="Acme Leads",
    )
    folded = apply_seo_spec(app, spec)
    design = _RECIPE.to_design_spec()
    return folded, design, generate(folded, design)


def _collection_fixture():
    app = ensure_lead_entity(default_lead_gen_app_spec("Acme Leads", _RECIPE))
    spec = CollectionSpec.model_validate(
        {
            "collection_id": "team",
            "title": "Team",
            "items": [
                {"label": "Ana Ferreira", "detail": "Founder"},
                {"label": "Bo Kim"},
            ],
        }
    )
    folded = apply_collection_spec(app, spec)
    design = _RECIPE.to_design_spec()
    return folded, design, generate(folded, design)


def _by_name(res: PrimitiveVerifyResult) -> dict[str, VerifyCheck]:
    return {c.name: c for c in res.checks}


# ---- lead_gen_verify --------------------------------------------------------------


def test_lead_gen_verify_passes_on_generated_tree():
    app, design, tree = _lead_gen_fixture()
    res = lead_gen_verify(app, design, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    assert res.detail == "6 passed / 0 failed"
    assert [c.name for c in res.checks] == [
        "schema_sql_valid",
        "drizzle_schema_valid",
        "worker_contract",
        "lead_form_posts",
        "local_api_roundtrip",
        "cloudflare_export_ready",
    ]
    assert all(c.passed for c in res.checks)


def test_lead_gen_verify_empty_tree_and_no_app_fails_with_app_create_evidence():
    # The verifier's fallback input when app_create never ran: the exact
    # pre-dispatch evidence strings.
    res = lead_gen_verify(None, None, {})
    assert not res.ok
    checks = _by_name(res)
    assert (
        checks["schema_sql_valid"].evidence
        == "missing .disco/appspec.json or schema.sql — run app_create first."
    )
    assert checks["worker_contract"].evidence == "no worker/index.ts in the workspace."
    assert (
        checks["local_api_roundtrip"].evidence
        == "cannot model the lead flow without a valid schema.sql + worker contract."
    )
    assert res.detail == "0 passed / 5 failed"
    # no lead entity resolved → no lead_form_posts check, same as the old tool branch
    assert "lead_form_posts" not in checks


def test_lead_gen_verify_missing_worker_fails_worker_and_roundtrip_only():
    app, design, tree = _lead_gen_fixture()
    tree = {k: v for k, v in tree.items() if k != "worker/index.ts"}
    res = lead_gen_verify(app, design, tree)
    assert not res.ok
    checks = _by_name(res)
    assert checks["worker_contract"].passed is False
    assert checks["worker_contract"].evidence == "no worker/index.ts in the workspace."
    assert checks["local_api_roundtrip"].passed is False
    assert (
        checks["local_api_roundtrip"].evidence
        == "cannot model the lead flow without a valid schema.sql + worker contract."
    )
    # worker/index.ts is ALSO a Cloudflare export deliverable → that check fails too
    assert checks["cloudflare_export_ready"].passed is False
    assert "worker/index.ts" in checks["cloudflare_export_ready"].evidence
    # the rest stay green
    assert checks["schema_sql_valid"].passed is True
    assert checks["drizzle_schema_valid"].passed is True
    assert checks["lead_form_posts"].passed is True
    assert res.detail == "3 passed / 3 failed"


def test_lead_gen_verify_missing_form_component_fails_lead_form():
    app, design, tree = _lead_gen_fixture()
    tree = {
        k: v
        for k, v in tree.items()
        if not (k.startswith("src/components/") and k.endswith(".tsx"))
    }
    res = lead_gen_verify(app, design, tree)
    checks = _by_name(res)
    assert checks["lead_form_posts"].passed is False
    assert (
        checks["lead_form_posts"].evidence
        == 'no form component calls useSubmit("/api/leads") — the lead form is missing or broken.'
    )


def test_lead_gen_verify_real_dev_vars_fails_export_check():
    app, design, tree = _lead_gen_fixture()
    tree = dict(tree)
    tree[".dev.vars"] = "ADMIN_TOKEN=super-secret-real-value\n"
    res = lead_gen_verify(app, design, tree)
    checks = _by_name(res)
    assert checks["cloudflare_export_ready"].passed is False
    assert ".dev.vars" in checks["cloudflare_export_ready"].evidence


def test_lead_gen_verify_design_is_accepted_but_unused():
    app, _design, tree = _lead_gen_fixture()
    assert lead_gen_verify(app, None, tree).ok  # None design — same verdict


# ---- directory_verify --------------------------------------------------------------


def test_directory_verify_passes_on_generated_tree():
    app, design, tree = _directory_fixture()
    res = directory_verify(app, design, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    assert res.detail == "3 passed / 0 failed"
    assert [c.name for c in res.checks] == [
        "directory_listing",
        "static_worker_contract",
        "cloudflare_export_ready",
    ]


def test_directory_verify_no_app_fails_listing_with_app_create_evidence():
    _app, design, tree = _directory_fixture()
    res = directory_verify(None, design, tree)
    checks = _by_name(res)
    assert checks["directory_listing"].passed is False
    assert checks["directory_listing"].evidence == "no .disco/appspec.json — run app_create first."


def test_directory_verify_missing_worker_fails_static_contract():
    app, design, tree = _directory_fixture()
    tree = {k: v for k, v in tree.items() if k != "worker/index.ts"}
    res = directory_verify(app, design, tree)
    checks = _by_name(res)
    assert checks["static_worker_contract"].passed is False
    assert checks["static_worker_contract"].evidence == "no worker/index.ts in the workspace."
    assert checks["directory_listing"].passed is True


def test_directory_verify_worker_grown_lead_api_fails():
    app, design, tree = _directory_fixture()
    tree = dict(tree)
    tree["worker/index.ts"] = tree["worker/index.ts"].replace(
        "return env.ASSETS.fetch(request);",
        'if (new URL(request.url).pathname === "/api/leads") {}\n'
        "    return env.ASSETS.fetch(request);",
    )
    res = directory_verify(app, design, tree)
    checks = _by_name(res)
    assert checks["static_worker_contract"].passed is False
    assert "/api/leads" in checks["static_worker_contract"].evidence


# ---- form_verify --------------------------------------------------------------------


def test_form_verify_passes_on_folded_generated_tree():
    app, design, tree = _form_fixture()
    res = form_verify(app, design, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    assert res.detail == "4 passed / 0 failed"
    assert [c.name for c in res.checks] == [
        "form_schema_table",
        "form_worker_validate_route",
        "form_section_component",
        "form_success_message",
    ]


def test_form_verify_fails_when_worker_route_removed():
    app, design, tree = _form_fixture()
    tree = dict(tree)
    tree["worker/index.ts"] = tree["worker/index.ts"].replace(
        '"/api/quote_requests"', '"/api/missing"'
    )
    res = form_verify(app, design, tree)
    checks = _by_name(res)
    assert checks["form_worker_validate_route"].passed is False
    assert "/api/quote_requests" in checks["form_worker_validate_route"].evidence


# ---- seo_verify ---------------------------------------------------------------------


def test_seo_verify_passes_on_folded_generated_tree():
    app, design, tree = _seo_fixture()
    res = seo_verify(app, design, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    assert res.detail == "3 passed / 0 failed"
    assert [c.name for c in res.checks] == [
        "seo_head_metadata",
        "seo_robots_txt",
        "seo_sitemap_xml",
    ]


def test_seo_verify_fails_when_sitemap_loses_route():
    app, design, tree = _seo_fixture()
    tree = dict(tree)
    tree["public/sitemap.xml"] = tree["public/sitemap.xml"].replace(
        "<loc>https://example.com/</loc>", ""
    )
    res = seo_verify(app, design, tree)
    checks = _by_name(res)
    assert checks["seo_sitemap_xml"].passed is False
    assert "expected" in checks["seo_sitemap_xml"].evidence


# ---- collection_verify --------------------------------------------------------------


def test_collection_verify_passes_on_folded_generated_tree():
    app, design, tree = _collection_fixture()
    res = collection_verify(app, design, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    assert res.detail == "2 passed / 0 failed"
    assert [c.name for c in res.checks] == [
        "collection_sections_render",
        "collection_items_present",
    ]


def test_collection_verify_fails_when_content_item_missing():
    app, design, tree = _collection_fixture()
    tree = dict(tree)
    tree["src/generated/content.ts"] = tree["src/generated/content.ts"].replace(
        "Ana Ferreira — Founder", "Missing Person"
    )
    res = collection_verify(app, design, tree)
    checks = _by_name(res)
    assert checks["collection_items_present"].passed is False
    assert "Ana Ferreira" in checks["collection_items_present"].evidence


# ---- hello_verify -------------------------------------------------------------------


def _hello_fixture():
    app = default_hello_app_spec("Acme Studio", _RECIPE)
    return app, generate_hello(app, _RECIPE.to_design_spec())


def test_hello_verify_passes_on_generated_tree():
    app, tree = _hello_fixture()
    res = hello_verify(app, None, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    assert [c.name for c in res.checks] == [
        "hello_index_present",
        "hello_headline",
        "hello_subtitle",
    ]
    assert res.detail == "3 passed / 0 failed"


def test_hello_verify_fails_on_wrong_headline():
    app, tree = _hello_fixture()
    tree = dict(tree)
    tree["index.html"] = tree["index.html"].replace("Acme Studio", "Wrong Name")
    res = hello_verify(app, None, tree)
    assert not res.ok
    checks = _by_name(res)
    assert checks["hello_index_present"].passed is True
    assert checks["hello_headline"].passed is False
    assert "Acme Studio" in checks["hello_headline"].evidence


def test_hello_verify_fails_when_index_missing():
    app, _tree = _hello_fixture()
    res = hello_verify(app, None, {})
    assert not res.ok
    checks = _by_name(res)
    assert checks["hello_index_present"].passed is False
    assert "run app_create first" in checks["hello_index_present"].evidence
    assert checks["hello_headline"].passed is False
    assert checks["hello_subtitle"].passed is False


def test_hello_verify_subtitle_tracks_folded_hello_spec():
    # The WO-A1 fold made VISIBLE: the applied headline becomes the first page's
    # title, rendered as the <p> subtitle — verify sees it end to end.
    app, _ = _hello_fixture()
    folded = apply_hello_spec(app, HelloSpec(headline="Handcrafted since 1994"))
    tree = generate_hello(folded, _RECIPE.to_design_spec())
    res = hello_verify(folded, None, tree)
    assert res.ok, [c for c in res.checks if not c.passed]
    # ...and a stale tree (pre-fold) FAILS the subtitle check against the folded spec
    stale = generate_hello(app, _RECIPE.to_design_spec())
    res_stale = hello_verify(folded, None, stale)
    checks = _by_name(res_stale)
    assert checks["hello_subtitle"].passed is False
    assert "Handcrafted since 1994" in checks["hello_subtitle"].evidence


# ---- the VerifyCheck / checks-tuple shape -------------------------------------------


def test_primitive_verify_result_checks_default_is_empty_tuple():
    res = PrimitiveVerifyResult(ok=True)
    assert res.checks == ()
    assert res.detail == ""


def test_verify_check_is_frozen_and_holds_fields():
    check = VerifyCheck(name="x", passed=False, evidence="why")
    assert (check.name, check.passed, check.evidence) == ("x", False, "why")
    try:
        check.passed = True  # type: ignore[misc]
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("VerifyCheck must be frozen")


# ---- registry wiring ----------------------------------------------------------------


def test_registry_verify_hooks_wired_and_records_stays_none():
    lead = get_primitive("lead_gen")
    directory = get_primitive("directory")
    records = get_primitive("records")
    hello = get_primitive("hello")
    blog = get_primitive("blog")
    form = get_primitive("form")
    seo = get_primitive("seo")
    collection = get_primitive("collection")
    assert lead is not None and lead.verify is lead_gen_verify
    assert directory is not None and directory.verify is directory_verify
    assert hello is not None and hello.verify is hello_verify
    assert blog is not None and blog.verify is blog_verify
    assert form is not None and form.verify is form_verify
    assert seo is not None and seo.verify is seo_verify
    assert collection is not None and collection.verify is collection_verify
    # records stays verify=None ON PURPOSE: its apps fall through to the lead-gen
    # bundle exactly as they did before the dispatch existed.
    assert records is not None and records.verify is None
