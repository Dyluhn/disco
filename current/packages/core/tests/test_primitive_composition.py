"""Cross-primitive composition — the union nobody's single lane tested.

The addable primitives (seo/form/collection/feature_flags/blog/analytics) each
landed from an isolated lane wrapping the SAME shared emitters. This suite folds
them ALL onto one lead_gen app and asserts the composed lowering chain
(form → feature_flags → analytics, analytics over the blog-conditional App.tsx)
actually emits every primitive's contribution — a silent drop in any wrapper
would pass each lane's own tests and still ship a broken union.
"""

from __future__ import annotations

from disco.core.appkit.analytics_primitive import AnalyticsSpec, apply_analytics_spec
from disco.core.appkit.blog_primitive import BlogSpec, apply_blog_spec
from disco.core.appkit.feature_flags_primitive import (
    FeatureFlagsSpec,
    apply_feature_flags_spec,
)
from disco.core.appkit.form_primitive import FormSpec, apply_form_spec
from disco.core.appkit.generator import default_lead_gen_app_spec, generate
from disco.core.appkit.recipes import get_recipe
from disco.core.appkit.seo_primitive import SeoSpec, apply_seo_spec


def _all_primitives_app():
    app = default_lead_gen_app_spec("Composed Cafe", get_recipe("editorial-ledger"))
    app = apply_seo_spec(
        app,
        SeoSpec(
            site_description="A cafe site exercising every addable primitive.",
            base_url="https://composed.example.com",
        ),
    )
    app = apply_form_spec(
        app,
        FormSpec.model_validate(
            {
                "form_id": "catering",
                "title": "Catering inquiries",
                "fields": [
                    {"name": "email", "kind": "email", "label": "Email", "required": True},
                    {"name": "notes", "kind": "text", "label": "Notes", "required": False},
                ],
            }
        ),
    )
    app = apply_feature_flags_spec(
        app,
        FeatureFlagsSpec.model_validate(
            {"flags": [{"key": "dark-mode", "enabled": True, "description": "Dark UI"}]}
        ),
    )
    app = apply_blog_spec(
        app,
        BlogSpec.model_validate(
            {
                "posts": [
                    {
                        "slug": "opening-day",
                        "title": "Opening day",
                        "date": "2026-07-01",
                        "body_md": "We are **open**.",
                    }
                ]
            }
        ),
    )
    app = apply_analytics_spec(app, AnalyticsSpec.model_validate({}))
    return app


def test_all_addable_primitives_compose_into_one_tree() -> None:
    app = _all_primitives_app()
    tree = generate(app, get_recipe("editorial-ledger").to_design_spec())

    schema = tree["schema.sql"]
    worker = tree["worker/index.ts"]
    app_tsx = tree["src/App.tsx"]
    main_tsx = tree["src/main.tsx"]

    # form: submissions table + validate route
    assert "catering" in schema
    assert "/api/forms/catering" in worker or "catering" in worker
    # feature_flags: seeded table + public route
    assert "_flags" in schema
    assert "/api/_flags" in worker
    # analytics: hits table + beacon route + beacon helper + dashboard route
    assert "_hits" in schema
    assert "/api/_hits" in worker
    assert "_hits" in main_tsx or "beacon" in main_tsx.lower()
    # blog: routes + rss + index page
    assert "/blog" in app_tsx
    assert "public/rss.xml" in tree
    assert "opening-day" in tree["public/rss.xml"]
    # seo: head extras + sitemap listing blog route
    assert "composed.example.com" in tree["index.html"]
    assert "public/sitemap.xml" in tree
    assert "/blog/opening-day" in tree["public/sitemap.xml"]


def test_composition_without_optional_primitives_is_unaffected() -> None:
    app = default_lead_gen_app_spec("Plain Cafe", get_recipe("editorial-ledger"))
    tree = generate(app, get_recipe("editorial-ledger").to_design_spec())
    assert "_flags" not in tree["schema.sql"]
    assert "_hits" not in tree["schema.sql"]
    assert "public/rss.xml" not in tree
