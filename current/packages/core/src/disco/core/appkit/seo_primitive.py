"""The AppKit `seo` primitive (Epic F5.3-lite): meta/OG/JSON-LD + sitemap + robots.

A FILLABLE, ADDABLE primitive in the WO-A0/A1 mold: the model fills a small
validated `SeoSpec` (site description, canonical base URL, optional social image
and site name) and `app_add_primitive` folds it into `AppSpec.seo` via
`apply_seo_spec`; the app's own BASE primitive then regenerates the whole tree,
and the SHARED emitters in `generator.py` lower `app.seo` into:

* `<meta name="description">`, the OG tags and a JSON-LD ``WebSite`` block in
  the shared `index.html` head (`generator_parts.app_shell._seo_head_extras`);
* `public/robots.txt` (allow-all + the sitemap pointer) and `public/sitemap.xml`
  (one absolute `<url>` per AppSpec page) — under `public/` so Vite copies them
  verbatim into `dist/`, which is what the Worker's static-asset layer serves
  (`_seo_files`).

Like `hello`, this primitive exists to be ADDED to real apps (lead_gen /
directory / records), never scaffolded standalone: its own `default_app_spec` /
`generate` mirror hello's minimal single-page shape and are only the
registry-contract stub. Hello-compat corollary (deliberate, documented): folding
seo onto a `hello` — or a standalone `seo` — app changes only the `.disco` spec;
those minimal `generate` functions ignore `app.seo`, because the seo LOWERING
lives in the shared emitters the real verticals call.
"""

from __future__ import annotations

import html

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .primitives import (
    LOCAL_LIST_PRIMITIVE_ID,
    SEO_PRIMITIVE_ID,
    PrimitiveDefinition,
    register_primitive,
)
from .recipes import SiteRecipe
from .spec import (
    MAX_SEO_DESCRIPTION,
    MAX_SEO_SITE_NAME,
    MAX_SEO_URL,
    AppSpec,
    DesignSpec,
    Page,
    validate_http_url,
)


class SeoSpec(BaseModel):
    """The seo primitive's declarative fill-and-validate spec (WO-A1 contract).

    Mirrors `spec.SeoMeta` (the RESOLVED values persisted on the AppSpec) with
    the SAME bounds and URL checks — single-sourced from `spec` so the pair can
    never drift. `extra="forbid"` so an unknown key is a precise, self-recovering
    refusal (app_add_primitive carries this schema back), never a silent drop."""

    model_config = ConfigDict(extra="forbid")

    site_description: str = Field(
        min_length=1,
        max_length=MAX_SEO_DESCRIPTION,
        description=(
            "The site's meta description (1-300 chars) — used for "
            '<meta name="description">, og:description and the JSON-LD block.'
        ),
    )
    base_url: str = Field(
        min_length=1,
        max_length=MAX_SEO_URL,
        description=(
            "The canonical absolute base URL of the deployed site (must start "
            "with 'https://' or 'http://' and have a host, e.g. "
            "'https://example.com') — used for og:url and to build the "
            "sitemap.xml/robots.txt absolute URLs."
        ),
    )
    social_image_url: str | None = Field(
        default=None,
        max_length=MAX_SEO_URL,
        description=(
            "Optional absolute http(s) URL of the social share image (og:image). "
            "Omit if the site has none."
        ),
    )
    site_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SEO_SITE_NAME,
        description=(
            "Optional site name for og:title and the JSON-LD block; defaults to "
            "the app's name when omitted."
        ),
    )

    @field_validator("base_url")
    @classmethod
    def _base_url_is_http(cls, value: str) -> str:
        return validate_http_url(value, field="base_url")

    @field_validator("social_image_url")
    @classmethod
    def _social_image_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_http_url(value, field="social_image_url")


def apply_seo_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated SeoSpec into `AppSpec.seo` — the house dump → mutate →
    re-validate dance. Re-applying with DIFFERENT values yields a changed app;
    the identical-re-apply no-op refusal comes free from app_add_primitive's
    dump-equality check. `site_name=None` is stored as-is (it resolves to
    `app.name` at emit time — see `spec.SeoMeta`)."""
    if not isinstance(spec, SeoSpec):  # defensive: app_add_primitive validated it
        raise TypeError(f"apply_spec for {SEO_PRIMITIVE_ID!r} needs a SeoSpec")
    if app.app_kind == LOCAL_LIST_PRIMITIVE_ID:
        raise ValueError(
            "local_list does not support the seo folded primitive: its dedicated "
            "generator does not lower SEO metadata or public sitemap/robots files. "
            "Use a supported base primitive instead."
        )
    data = app.model_dump(mode="json")
    data["seo"] = {
        "site_description": spec.site_description,
        "base_url": spec.base_url,
        "social_image_url": spec.social_image_url,
        "site_name": spec.site_name,
    }
    return AppSpec.model_validate(data)


def default_seo_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The registry-contract stub: the seo primitive exists to be ADDED to real
    apps, so its standalone default is hello-minimal — one home page, no
    entities, no actions. `recipe` is deliberately unused (a metadata primitive
    has no design-preference surface)."""
    del recipe
    title = name.strip() or "Seo"
    return AppSpec(
        schema_version=1,
        app_kind=SEO_PRIMITIVE_ID,
        name=title,
        pages=(Page(id="home", route="/", title="Home"),),
    )


def prepare_seo_app_spec(app: AppSpec) -> AppSpec:
    """Identity — the seo primitive needs no normalization."""
    return app


def generate_seo(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """The registry-contract stub tree: a minimal static index.html, exactly
    hello's shape. DELIBERATELY ignores `app.seo` — the seo lowering lives in the
    SHARED emitters (`generator_parts.app_shell._seo_head_extras` / `_seo_files`) that
    the real verticals (lead_gen/directory/records) call; a standalone seo (or
    hello) app carries the folded values in its `.disco` spec only. `design` is
    deliberately unused — see `default_seo_app_spec`."""
    del design
    title = html.escape(app.name)
    return {
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="utf-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"    <title>{title}</title>\n"
            "  </head>\n"
            "  <body>\n"
            f"    <h1>{title}</h1>\n"
            "    <p>Add this primitive to a real app: it contributes SEO metadata, "
            "not a scaffold.</p>\n"
            "  </body>\n"
            "</html>\n"
        )
    }


from .primitive_verify import seo_verify  # noqa: E402

register_primitive(
    PrimitiveDefinition(
        id=SEO_PRIMITIVE_ID,
        default_app_spec=default_seo_app_spec,
        prepare_app_spec=prepare_seo_app_spec,
        generate=generate_seo,
        tier="fillable",
        host_contract=(),
        spec_schema=SeoSpec,
        verify=seo_verify,
        apply_spec=apply_seo_spec,
    )
)


__all__ = [
    "SEO_PRIMITIVE_ID",
    "SeoSpec",
    "apply_seo_spec",
    "default_seo_app_spec",
    "generate_seo",
    "prepare_seo_app_spec",
    "seo_verify",
]
