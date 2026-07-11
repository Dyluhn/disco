"""Epic 6.1 - the `analytics` AppKit primitive.

Covers the core contract: strict fillable spec, fold into AppSpec.analytics plus
an owner dashboard page/section, lead-gen lowering for `_hits` schema + Worker
routes + SPA beacon + dashboard component, and the real WO-A3 verify hook.
"""

from __future__ import annotations

import sqlite3

import pytest
from disco.core.appkit.analytics_primitive import (
    AnalyticsSpec,
    analytics_verify,
    apply_analytics_spec,
)
from disco.core.appkit.generator import generate
from disco.core.appkit.primitives import (
    ANALYTICS_PRIMITIVE_ID,
    DIRECTORY_PRIMITIVE_ID,
    get_primitive,
)
from disco.core.appkit.recipes import SiteRecipe, get_recipe
from disco.core.appkit.spec import AnalyticsMeta, AppSpec
from pydantic import ValidationError


def _recipe() -> SiteRecipe:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _lead_gen_app() -> AppSpec:
    prim = get_primitive("lead_gen")
    assert prim is not None
    return prim.prepare_app_spec(prim.default_app_spec("Acme Studio", _recipe()))


def _design():
    return _recipe().to_design_spec()


def _spec(**overrides: object) -> AnalyticsSpec:
    payload: dict[str, object] = {}
    payload.update(overrides)
    return AnalyticsSpec.model_validate(payload)


def _folded() -> AppSpec:
    return apply_analytics_spec(_lead_gen_app(), _spec(dashboard_page_title="Traffic"))


def _dashboard_component(tree: dict[str, str]) -> str:
    for path, source in tree.items():
        if path.startswith("src/components/") and path.endswith(".tsx"):
            if 'id="analytics-dashboard"' in source:
                return source
    raise AssertionError("analytics dashboard component not emitted")


def test_analytics_spec_validation() -> None:
    assert _spec().dashboard_page_title == "Analytics"
    assert _spec(dashboard_page_title=" Traffic ").dashboard_page_title == "Traffic"
    with pytest.raises(ValidationError, match="Extra inputs"):
        AnalyticsSpec.model_validate({"provider": "external"})
    with pytest.raises(ValidationError):
        _spec(dashboard_page_title=" ")
    with pytest.raises(ValidationError):
        _spec(dashboard_page_title="x" * 121)


def test_analytics_meta_is_additive_and_hidden_when_unset() -> None:
    app = _lead_gen_app()
    assert app.analytics is None
    assert "analytics" not in app.model_dump(mode="json")
    assert AnalyticsMeta(dashboard_page_title=" Traffic ").dashboard_page_title == "Traffic"

    tree = generate(app, _design())
    assert "_hits" not in tree["schema.sql"]
    assert "/api/_hits" not in tree["worker/index.ts"]
    assert "installAnalyticsBeacon" not in tree["src/main.tsx"]


def test_apply_analytics_spec_adds_dashboard_page_and_reapply_updates() -> None:
    folded = _folded()
    assert folded.analytics is not None
    assert folded.analytics.dashboard_page_title == "Traffic"
    page = next(page for page in folded.pages if page.id == "analytics")
    assert page.route == "/analytics"
    assert page.title == "Traffic"
    section = page.sections[0]
    assert section.id == "analytics-dashboard"
    assert section.kind == "custom"
    assert section.content_ref == "_hits"
    assert section.content is not None
    assert section.content.heading == "Traffic"

    assert apply_analytics_spec(folded, _spec(dashboard_page_title="Traffic")).model_dump(
        mode="json"
    ) == folded.model_dump(mode="json")
    changed = apply_analytics_spec(folded, _spec(dashboard_page_title="Page analytics"))
    assert changed.analytics is not None
    assert changed.analytics.dashboard_page_title == "Page analytics"
    assert next(page for page in changed.pages if page.id == "analytics").title == (
        "Page analytics"
    )


def test_apply_analytics_spec_refuses_non_lead_gen_hosts() -> None:
    directory = get_primitive(DIRECTORY_PRIMITIVE_ID)
    assert directory is not None
    app = directory.default_app_spec("Directory", _recipe())
    with pytest.raises(ValueError, match="lead_gen-shaped"):
        apply_analytics_spec(app, _spec())


def test_analytics_lowering_emits_schema_worker_dashboard_and_beacon() -> None:
    tree = generate(_folded(), _design())
    schema = tree["schema.sql"]
    assert 'CREATE TABLE IF NOT EXISTS "_hits"' in schema
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(schema)
        cols = {row[1] for row in con.execute('PRAGMA table_info("_hits")')}
        assert {"path", "ts", "referrer", "ua_class"} <= cols
    finally:
        con.close()

    worker = tree["worker/index.ts"]
    assert 'url.pathname === "/api/_hits" && request.method === "POST"' in worker
    assert 'url.pathname === "/api/_hits/summary" && request.method === "GET"' in worker
    assert "hitRateLimited()" in worker
    assert "isAuthorized(request, env)" in worker
    assert 'INSERT INTO "_hits"' in worker

    main = tree["src/main.tsx"]
    assert "installAnalyticsBeacon" in main
    assert "sendBeacon" in main
    assert "pushState" in main
    assert "popstate" in main

    app_shell = tree["src/App.tsx"]
    assert '"/analytics": Page' in app_shell
    dashboard = _dashboard_component(tree)
    assert 'const SUMMARY_PATH = "/api/_hits/summary";' in dashboard
    assert "Authorization" in dashboard
    assert "Bearer" in dashboard


def test_analytics_verify_hook_passes_and_fails_on_missing_beacon() -> None:
    app = _folded()
    tree = generate(app, _design())
    result = analytics_verify(app, _design(), tree)
    assert result.ok is True
    assert {check.name for check in result.checks} == {
        "analytics_schema",
        "analytics_worker_routes",
        "analytics_dashboard_section",
        "analytics_spa_beacon",
    }

    broken = dict(tree)
    broken["src/main.tsx"] = broken["src/main.tsx"].replace("sendBeacon", "sendBecon")
    failed = analytics_verify(app, _design(), broken)
    checks = {check.name: check for check in failed.checks}
    assert failed.ok is False
    assert checks["analytics_spa_beacon"].passed is False


def test_analytics_primitive_registered_addable_with_verify() -> None:
    prim = get_primitive(ANALYTICS_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "fillable"
    assert prim.host_contract == ()
    assert prim.spec_schema is AnalyticsSpec
    assert prim.apply_spec is apply_analytics_spec
    assert prim.verify is analytics_verify
    with pytest.raises(ValueError, match="ADD-ON"):
        prim.default_app_spec("Analytics", _recipe())
    with pytest.raises(ValueError, match="does not generate"):
        prim.generate(_lead_gen_app(), _design())
