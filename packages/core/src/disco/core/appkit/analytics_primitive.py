"""The AppKit `analytics` primitive (Epic 6.1): first-party page analytics.

This is an ADD-ON primitive for lead_gen-shaped Cloudflare/D1 apps. The fillable
spec is intentionally small: an optional dashboard page title. The fold persists
resolved metadata on `AppSpec.analytics` and appends an owner-facing `/analytics`
SPA page with a dashboard section. The generated tree then gains:

* a `_hits` D1 table in `schema.sql`;
* a public POST `/api/_hits` beacon route with a simple per-worker rate counter;
* an owner-gated GET `/api/_hits/summary` route that reuses the existing
  `Authorization: Bearer <ADMIN_TOKEN>` convention;
* a dashboard component that reads the summary route only after the owner enters
  the token;
* a tiny no-dependency SPA entry helper that emits a beacon on initial load and
  history route changes.

Layering: pure core only. Generator internals are imported lazily inside emitter
helpers so registration can happen from `generator.py`'s bottom-of-module import.

This module is the public compatibility/export facade. Cohesive private
implementation (Worker/schema/SPA lowering, the dashboard component emitter, and
the verify hook) lives under :mod:`disco.core.appkit.analytics_primitive_parts`
and is re-imported here so every public (and tested private) symbol keeps its
original import path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .primitives import (
    ANALYTICS_PRIMITIVE_ID,
    LEAD_GEN_PRIMITIVE_ID,
    PrimitiveDefinition,
    register_primitive,
    resolve_primitive,
)
from .spec import (
    DEFAULT_ANALYTICS_DASHBOARD_PAGE_TITLE,
    MAX_ANALYTICS_DASHBOARD_PAGE_TITLE,
    AnalyticsMeta,
    AppSpec,
    DesignSpec,
    Page,
    Section,
    SectionContent,
)

if TYPE_CHECKING:
    from .recipes import SiteRecipe

_DASHBOARD_PAGE_ID = "analytics"
_DASHBOARD_ROUTE = "/analytics"
_DASHBOARD_SECTION_ID = "analytics-dashboard"
_HITS_TABLE = "_hits"
_SUMMARY_ROUTE = "/api/_hits/summary"
_BEACON_ROUTE = "/api/_hits"


class AnalyticsSpec(BaseModel):
    """The analytics primitive's declarative spec.

    `dashboard_page_title` is optional via its default. Unknown keys are refused
    so app_add_primitive can return a self-recovering schema error.
    """

    model_config = ConfigDict(extra="forbid")

    dashboard_page_title: str = Field(
        default=DEFAULT_ANALYTICS_DASHBOARD_PAGE_TITLE,
        min_length=1,
        max_length=MAX_ANALYTICS_DASHBOARD_PAGE_TITLE,
        description="Owner dashboard page title (optional; 1-120 chars).",
    )

    @field_validator("dashboard_page_title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dashboard_page_title must not be blank")
        return stripped


# ---- fold: AnalyticsSpec -> AppSpec -------------------------------------------


def analytics_enabled(app: AppSpec) -> bool:
    """True when the folded AppSpec asks the shared emitters to lower analytics."""
    return app.analytics is not None


def is_analytics_dashboard_section(section: Section) -> bool:
    """The fold marker for the generated dashboard component."""
    return section.id == _DASHBOARD_SECTION_ID and section.content_ref == _HITS_TABLE


def _dashboard_section(title: str) -> Section:
    return Section(
        id=_DASHBOARD_SECTION_ID,
        kind="custom",
        content_ref=_HITS_TABLE,
        content=SectionContent(
            heading=title,
            subheading="Owner-only page analytics.",
        ),
    )


def _dashboard_page(title: str) -> Page:
    return Page(
        id=_DASHBOARD_PAGE_ID,
        route=_DASHBOARD_ROUTE,
        title=title,
        sections=(_dashboard_section(title),),
    )


def _insert_dashboard_page(
    data: dict[str, Any],
    pages: list[Any],
    dashboard_json: dict[str, Any],
    section_json: dict[str, Any],
) -> None:
    """Add the dashboard page when the app has none yet (fresh insert)."""
    route_owner = next(
        (page.get("id") for page in pages if page.get("route") == _DASHBOARD_ROUTE),
        None,
    )
    if route_owner is not None:
        raise ValueError(
            f"route {_DASHBOARD_ROUTE!r} is already used by page {route_owner!r}; "
            "rename that page before adding analytics"
        )
    if any(
        section.get("id") == _DASHBOARD_SECTION_ID
        for page in pages
        for section in page.get("sections", [])
    ):
        raise ValueError(
            f"section id {_DASHBOARD_SECTION_ID!r} already exists; rename that "
            "section before adding analytics"
        )
    data["pages"] = [*pages, dashboard_json]


def _update_dashboard_page(
    data: dict[str, Any],
    pages: list[Any],
    page_idx: int,
    title: str,
    section_json: dict[str, Any],
) -> None:
    """Re-fold onto an existing dashboard page (re-apply of the same primitive)."""
    page = dict(pages[page_idx])
    if page.get("route") != _DASHBOARD_ROUTE:
        raise ValueError(
            f"page {_DASHBOARD_PAGE_ID!r} already exists with route "
            f"{page.get('route')!r}; analytics needs {_DASHBOARD_ROUTE!r}"
        )
    sections = list(page.get("sections") or [])
    section_idx = next(
        (i for i, section in enumerate(sections) if section.get("id") == _DASHBOARD_SECTION_ID),
        None,
    )
    if section_idx is None:
        if any(
            section.get("id") == _DASHBOARD_SECTION_ID
            for i, other in enumerate(pages)
            if i != page_idx
            for section in other.get("sections", [])
        ):
            raise ValueError(f"section id {_DASHBOARD_SECTION_ID!r} already exists on another page")
        sections.append(section_json)
    else:
        existing = dict(sections[section_idx])
        if existing.get("kind") != "custom" or existing.get("content_ref") != _HITS_TABLE:
            raise ValueError(
                f"section id {_DASHBOARD_SECTION_ID!r} is already taken by a non-analytics "
                "section; rename it before adding analytics"
            )
        if existing.get("variant_id") is not None:
            section_json["variant_id"] = existing["variant_id"]
        sections[section_idx] = {
            **existing,
            "kind": "custom",
            "content_ref": _HITS_TABLE,
            "content": section_json["content"],
        }
    page["title"] = title
    page["sections"] = sections
    pages[page_idx] = page
    data["pages"] = pages


def _upsert_dashboard_page(data: dict[str, Any], title: str) -> None:
    pages = list(data.get("pages") or [])
    page_idx = next(
        (i for i, page in enumerate(pages) if page.get("id") == _DASHBOARD_PAGE_ID),
        None,
    )
    dashboard_json = _dashboard_page(title).model_dump(mode="json")
    section_json = dashboard_json["sections"][0]
    if page_idx is None:
        _insert_dashboard_page(data, pages, dashboard_json, section_json)
        return
    _update_dashboard_page(data, pages, page_idx, title, section_json)


def apply_analytics_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold AnalyticsSpec into a lead_gen-shaped AppSpec.

    The fold uses the house dump -> mutate -> re-validate pattern. Directory is
    static and records owns a different Worker contract, so this slice scopes to
    the lead_gen shape that already has D1 plus owner Bearer-token auth.
    """
    if not isinstance(spec, AnalyticsSpec):
        raise TypeError(f"apply_spec for {ANALYTICS_PRIMITIVE_ID!r} needs an AnalyticsSpec")

    base = resolve_primitive(app.app_kind)
    if base.id != LEAD_GEN_PRIMITIVE_ID:
        raise ValueError(
            "the analytics primitive currently folds only into lead_gen-shaped apps; "
            f"this app's app_kind {app.app_kind!r} lowers through {base.id!r}"
        )
    if not app.pages:
        raise ValueError("the app has no pages to place the analytics dashboard on")

    title = spec.dashboard_page_title
    data = app.model_dump(mode="json")
    data["analytics"] = AnalyticsMeta(dashboard_page_title=title).model_dump(mode="json")
    _upsert_dashboard_page(data, title)
    return AppSpec.model_validate(data)


# ---- registration ------------------------------------------------------------


def default_analytics_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The analytics primitive is an add-on, never a base scaffold."""
    del name, recipe
    raise ValueError(
        "the 'analytics' primitive is an ADD-ON, not a base scaffold: app_create a "
        "base lead_gen app first, then app_add_primitive(primitive_id='analytics', spec={})."
    )


def prepare_analytics_app_spec(app: AppSpec) -> AppSpec:
    return app


def generate_analytics(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    del app, design
    raise ValueError(
        "the 'analytics' primitive does not generate a tree of its own; the host "
        "app's base primitive regenerates from the folded AppSpec."
    )


# ---- private implementation re-imports ---------------------------------------
# The cohesive private helpers live under ``analytics_primitive_parts`` and are
# imported here AFTER all public models/constants are defined, so the private
# modules can import those public names back without a circular-dependency
# error. These names are the runtime implementation behind the public facade;
# external callers must continue to import them from
# ``disco.core.appkit.analytics_primitive``.

from .analytics_primitive_parts._dashboard_component import (  # noqa: E402
    emit_analytics_dashboard_component,
)
from .analytics_primitive_parts._lowering import (  # noqa: E402
    lower_analytics_app_tsx,
    lower_analytics_main_tsx,
    lower_analytics_schema_sql,
    lower_analytics_worker_ts,
)
from .analytics_primitive_parts._verify import analytics_verify  # noqa: E402

register_primitive(
    PrimitiveDefinition(
        id=ANALYTICS_PRIMITIVE_ID,
        default_app_spec=default_analytics_app_spec,
        prepare_app_spec=prepare_analytics_app_spec,
        generate=generate_analytics,
        tier="fillable",
        host_contract=(),
        spec_schema=AnalyticsSpec,
        verify=analytics_verify,
        apply_spec=apply_analytics_spec,
    )
)


__all__ = [
    "ANALYTICS_PRIMITIVE_ID",
    "AnalyticsSpec",
    "analytics_enabled",
    "analytics_verify",
    "apply_analytics_spec",
    "default_analytics_app_spec",
    "emit_analytics_dashboard_component",
    "generate_analytics",
    "is_analytics_dashboard_section",
    "lower_analytics_app_tsx",
    "lower_analytics_main_tsx",
    "lower_analytics_schema_sql",
    "lower_analytics_worker_ts",
    "prepare_analytics_app_spec",
]
