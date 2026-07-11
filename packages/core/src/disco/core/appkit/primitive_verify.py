"""AppKit WO-A3 — the per-primitive `verify` hooks (pure ports of the tool checks).

`verify_appkit_app` (tools layer) used to hard-code its primitive-specific check
bundles behind an `if primitive_id == DIRECTORY ... else lead_gen` branch. WO-A3
moves those bundles HERE as pure functions with the `PrimitiveDefinition.verify`
signature — ``(app, design, tree) -> PrimitiveVerifyResult`` — so the tool
DISPATCHES on the resolved primitive's `verify` hook and new primitives plug in
without touching the verifier.

The ports are BEHAVIOR-PRESERVING: check names and evidence strings are byte-
identical to the tool's originals; the tool's sandbox `_read_text(ctx, path)`
becomes ``tree.get(path)`` (the tree maps relpath → on-disk TEXT; a missing file
is an absent key), and the components-dir `list_dir` becomes iterating the tree's
``src/components/*.tsx`` keys in sorted order.

PURITY / LAYERING: sibling core modules only (`local_verify`, `worker_inspect`,
`primitives`, `generator`'s pure `resolve_lead_entity`) — no sandbox, no browser,
no IO. The browser-dependent route/section coverage stays in the tools layer.

CYCLE NOTE: this module imports `generator` (for `resolve_lead_entity`) and
`generator` imports THIS module at its bottom to register the verify hooks — that
works because the bottom-of-generator import runs after `resolve_lead_entity` is
defined, and `disco.core.appkit.__init__` always loads `generator` first.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping

from .generator import _comp_name, _component_names, _table_name, _ts, resolve_lead_entity
from .local_verify import (
    CF_EXPORT_FILES,
    STATIC_CF_EXPORT_FILES,
    WorkerAuthModel,
    check_drizzle_schema,
    check_schema_sql,
    cloudflare_export_ready,
    cloudflare_export_ready_static,
    local_api_roundtrip,
)
from .primitives import PrimitiveVerifyResult, VerifyCheck
from .spec import AppSpec, DesignSpec, Entity, Section
from .worker_inspect import (
    _strip_ts_comments,
    _use_submit_leads_in_call_position,
    inspect_directory_listing,
    inspect_lead_form,
    inspect_static_worker,
    inspect_submit_support,
    inspect_worker,
)

_SCHEMA_RELPATH = "schema.sql"
_DRIZZLE_SCHEMA_RELPATH = "src/db/schema.ts"
_PACKAGE_RELPATH = "package.json"
_WORKER_RELPATH = "worker/index.ts"
_COMPONENTS_DIR = "src/components"


def _component_paths(tree: Mapping[str, str]) -> list[str]:
    """The tree's component sources, in the same order the tool's sorted
    `list_dir` iteration produced (sorted basenames under a fixed prefix)."""
    return sorted(k for k in tree if k.startswith(_COMPONENTS_DIR + "/") and k.endswith(".tsx"))


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    n_fail = sum(1 for c in checks if not c.passed)
    n_pass = len(checks) - n_fail
    return PrimitiveVerifyResult(
        ok=n_fail == 0,
        detail=f"{n_pass} passed / {n_fail} failed",
        checks=tuple(checks),
    )


# ---- lead_gen -------------------------------------------------------------------


def _check_lead_form(tree: Mapping[str, str], lead: Entity) -> VerifyCheck:
    """Pure port of the tool's `_check_lead_form` — same selection rule (a REAL
    useSubmit("/api/leads") CALL), same evidence strings."""
    form_srcs: list[str] = []
    for path in _component_paths(tree):
        src = tree.get(path)
        # Select form components by a REAL useSubmit("/api/leads") CALL (not
        # inert string/comment content), consistent with inspect_lead_form.
        if src and _use_submit_leads_in_call_position(_strip_ts_comments(src)):
            form_srcs.append(src)
    if not form_srcs:
        return VerifyCheck(
            "lead_form_posts",
            False,
            'no form component calls useSubmit("/api/leads") — the lead form is missing or broken.',
        )
    client_src = tree.get("src/api/client.ts")
    hook_src = tree.get("src/hooks/useSubmit.ts")
    support_ok, support_reasons = inspect_submit_support(client_src or "", hook_src or "")
    if not support_ok:
        return VerifyCheck("lead_form_posts", False, "; ".join(support_reasons))
    for src in form_srcs:
        ok, reasons = inspect_lead_form(src, lead)
        if not ok:
            return VerifyCheck("lead_form_posts", False, "; ".join(reasons))
    return VerifyCheck(
        "lead_form_posts",
        True,
        f"the lead form submits JSON through useSubmit/postJson with an input per lead field "
        f"({', '.join(f.name for f in lead.fields)}).",
    )


def lead_gen_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """The LEAD-GEN contract checks (Epic E/G/I): schema_sql_valid,
    drizzle_schema_valid, worker_contract, lead_form_posts, local_api_roundtrip,
    cloudflare_export_ready — in that order. Pure port of the tool's
    `_lead_gen_checks`; also the verifier's FALLBACK for a missing/unreadable app
    and for primitives without a verify hook (records, unrecognized app_kinds)."""
    del design  # accepted-but-unused today — the shared signature is for future primitives
    lead = resolve_lead_entity(app) if app is not None else None
    checks: list[VerifyCheck] = []

    schema_sql = tree.get(_SCHEMA_RELPATH)
    drizzle_schema = tree.get(_DRIZZLE_SCHEMA_RELPATH)
    package_json = tree.get(_PACKAGE_RELPATH)
    worker_ts = tree.get(_WORKER_RELPATH)
    auth_model: WorkerAuthModel | None = None

    if lead is None or schema_sql is None:
        checks.append(
            VerifyCheck(
                "schema_sql_valid",
                False,
                "missing .disco/appspec.json or schema.sql — run app_create first.",
            )
        )
    else:
        res = check_schema_sql(schema_sql, lead)
        checks.append(VerifyCheck(res.name, res.passed, res.evidence))

    drizzle_res = check_drizzle_schema(
        {
            "schema.sql": schema_sql,
            "src/db/schema.ts": drizzle_schema,
            "package.json": package_json,
        }
    )
    checks.append(VerifyCheck(drizzle_res.name, drizzle_res.passed, drizzle_res.evidence))

    if worker_ts is None:
        checks.append(VerifyCheck("worker_contract", False, "no worker/index.ts in the workspace."))
    elif lead is not None:
        post_ok, inspected_auth, reasons = inspect_worker(worker_ts, lead)
        auth_model = inspected_auth
        ok = (
            post_ok
            and inspected_auth.reads_require_auth
            and inspected_auth.fail_closed_without_token
            and inspected_auth.admin_token_safe
            and inspected_auth.all_lead_reads_guarded
        )
        evidence = (
            "STRUCTURE verified (presence + ordering + parameterization + "
            "guard-first): public POST /api/leads region contains a Drizzle "
            "insert in a non-dead position; GET /api/leads + /admin "
            "early-return 401 as the first guard statement before any read; "
            "Bearer-checked + fail-closed on missing ADMIN_TOKEN. Local runtime "
            "proof lives in packages/core/tests/test_workerd_persistence.py; "
            "hosted Cloudflare deploy remains owner-gated."
            if ok
            else "; ".join(reasons)
        )
        checks.append(VerifyCheck("worker_contract", ok, evidence))

    if lead is not None:
        checks.append(_check_lead_form(tree, lead))

    if lead is not None and schema_sql is not None and auth_model is not None:
        res = local_api_roundtrip(schema_sql, lead, auth_model)
        checks.append(VerifyCheck(res.name, res.passed, res.evidence))
    else:
        checks.append(
            VerifyCheck(
                "local_api_roundtrip",
                False,
                "cannot model the lead flow without a valid schema.sql + worker contract.",
            )
        )

    cf_files: dict[str, str | None] = {rel: tree.get(rel) for rel in CF_EXPORT_FILES}
    cf_files[".dev.vars"] = tree.get(".dev.vars")
    cf_res = cloudflare_export_ready(cf_files)
    checks.append(VerifyCheck(cf_res.name, cf_res.passed, cf_res.evidence))
    return _result(checks)


# ---- directory ------------------------------------------------------------------


def _check_directory_listing(tree: Mapping[str, str], app: AppSpec | None) -> VerifyCheck:
    """directory_listing — the AppSpec declares a non-home route (the directory
    page) AND a `list` section, AND a generated component renders that listing with
    a controlled search input that filters the entries. Pure port of the tool's
    `_check_directory_listing`."""
    if app is None:
        return VerifyCheck(
            "directory_listing",
            False,
            "no .disco/appspec.json — run app_create first.",
        )
    has_dir_route = any(p.route != "/" for p in app.pages)
    has_list_section = any(s.kind == "list" for p in app.pages for s in p.sections)
    if not has_dir_route:
        return VerifyCheck(
            "directory_listing",
            False,
            "the directory site declares no page route other than '/' (no directory "
            "page to browse).",
        )
    if not has_list_section:
        return VerifyCheck(
            "directory_listing",
            False,
            "the directory site declares no `list` section to render the listings.",
        )
    listing_reasons: list[str] = []
    for path in _component_paths(tree):
        src = tree.get(path)
        if src is None:
            continue
        ok, reasons = inspect_directory_listing(src)
        if ok:
            return VerifyCheck(
                "directory_listing",
                True,
                "the directory listing renders entries behind a controlled search "
                "input that filters them client-side.",
            )
        # remember the closest candidate's reasons (an input-bearing component)
        if "<input" in src and reasons:
            listing_reasons = reasons
    return VerifyCheck(
        "directory_listing",
        False,
        "; ".join(listing_reasons)
        or "no listing component renders a searchable/filterable list of entries.",
    )


def directory_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """The DIRECTORY contract checks (Epic N): directory_listing (a non-home route
    + a searchable `list` section), static_worker_contract (asset passthrough, no
    lead API / D1), cloudflare_export_ready (static export shape). Pure port of the
    tool's `_directory_checks`."""
    del design  # accepted-but-unused today — the shared signature is for future primitives
    checks: list[VerifyCheck] = []

    checks.append(_check_directory_listing(tree, app))

    worker_ts = tree.get(_WORKER_RELPATH)
    if worker_ts is None:
        checks.append(
            VerifyCheck("static_worker_contract", False, "no worker/index.ts in the workspace.")
        )
    else:
        ok, reasons = inspect_static_worker(worker_ts)
        evidence = (
            "STRUCTURE verified: the Worker is a static asset passthrough "
            "(env.ASSETS.fetch) with no lead API, no D1 insert, and no admin "
            "read-back — the directory site has no server data plane."
            if ok
            else "; ".join(reasons)
        )
        checks.append(VerifyCheck("static_worker_contract", ok, evidence))

    cf_files: dict[str, str | None] = {rel: tree.get(rel) for rel in STATIC_CF_EXPORT_FILES}
    cf_files[".dev.vars"] = tree.get(".dev.vars")
    cf_res = cloudflare_export_ready_static(cf_files)
    checks.append(VerifyCheck(cf_res.name, cf_res.passed, cf_res.evidence))
    return _result(checks)


def _component_path(app: AppSpec, page_id: str, section_id: str) -> str:
    names = _component_names(app)
    page = next(p for p in app.pages if p.id == page_id)
    section = next(s for s in page.sections if s.id == section_id)
    return f"{_COMPONENTS_DIR}/{_comp_name(names, page, section)}.tsx"


def _form_sections(app: AppSpec) -> list[tuple[str, Section, Entity]]:
    from .form_primitive import form_submission_entities_for

    try:
        lead = resolve_lead_entity(app)
        forms = form_submission_entities_for(app, reserved_entities=(lead,))
    except Exception:
        forms = form_submission_entities_for(app)
    by_id = {entity.id: entity for entity in forms}
    out: list[tuple[str, Section, Entity]] = []
    for page in app.pages:
        for section in page.sections:
            if section.kind != "form" or section.content_ref is None:
                continue
            entity = by_id.get(section.content_ref)
            if entity is not None:
                out.append((page.id, section, entity))
    return out


def form_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Pure checks for folded form primitive output."""
    del design
    checks: list[VerifyCheck] = []
    if app is None:
        return _result(
            [
                VerifyCheck(
                    "form_schema_table",
                    False,
                    "no .disco/appspec.json — cannot verify folded form output.",
                ),
                VerifyCheck(
                    "form_worker_validate_route",
                    False,
                    "no .disco/appspec.json — cannot derive folded form routes.",
                ),
                VerifyCheck(
                    "form_section_component",
                    False,
                    "no .disco/appspec.json — cannot locate folded form sections.",
                ),
                VerifyCheck(
                    "form_success_message",
                    False,
                    "no .disco/appspec.json — cannot verify form success copy.",
                ),
            ]
        )

    forms = _form_sections(app)
    if not forms:
        return _result(
            [
                VerifyCheck(
                    "form_schema_table",
                    False,
                    "the AppSpec has no folded form sections with form content_ref entities.",
                ),
                VerifyCheck(
                    "form_worker_validate_route",
                    False,
                    "the AppSpec has no folded form routes to verify.",
                ),
                VerifyCheck(
                    "form_section_component",
                    False,
                    "the AppSpec has no folded form section components to verify.",
                ),
                VerifyCheck(
                    "form_success_message",
                    False,
                    "the AppSpec has no folded form success messages to verify.",
                ),
            ]
        )

    schema_sql = tree.get(_SCHEMA_RELPATH)
    if schema_sql is None:
        checks.append(VerifyCheck("form_schema_table", False, "no schema.sql in the workspace."))
    else:
        missing_tables = [
            _table_name(entity)
            for _page_id, _section, entity in forms
            if f'CREATE TABLE IF NOT EXISTS "{_table_name(entity)}"' not in schema_sql
        ]
        checks.append(
            VerifyCheck(
                "form_schema_table",
                not missing_tables,
                (
                    "schema.sql contains the folded form submissions table(s): "
                    + ", ".join(_table_name(entity) for _p, _s, entity in forms)
                    if not missing_tables
                    else "schema.sql is missing folded form submissions table(s): "
                    + ", ".join(missing_tables)
                ),
            )
        )

    worker_ts = tree.get(_WORKER_RELPATH)
    if worker_ts is None:
        checks.append(
            VerifyCheck("form_worker_validate_route", False, "no worker/index.ts in the workspace.")
        )
    else:
        from .form_primitive import form_route_for

        missing_routes = [
            form_route_for(app, entity)
            for _page_id, _section, entity in forms
            if _ts(form_route_for(app, entity)) not in worker_ts
        ]
        has_422 = "return json({ error: check.error }, 422);" in worker_ts
        has_validator = "function validateAppForm" in worker_ts and "APP_FORM_ROUTES" in worker_ts
        checks.append(
            VerifyCheck(
                "form_worker_validate_route",
                not missing_routes and has_422 and has_validator,
                (
                    "worker/index.ts contains app-form route(s) with validateAppForm and "
                    "the 422 validation path."
                    if not missing_routes and has_422 and has_validator
                    else "; ".join(
                        [
                            *(
                                [f"missing form route(s): {', '.join(missing_routes)}"]
                                if missing_routes
                                else []
                            ),
                            *(
                                ["missing validateAppForm/APP_FORM_ROUTES"]
                                if not has_validator
                                else []
                            ),
                            *(["missing 422 validation response"] if not has_422 else []),
                        ]
                    )
                ),
            )
        )

    missing_components: list[str] = []
    stale_components: list[str] = []
    missing_success: list[str] = []
    for page_id, section, _entity in forms:
        path = _component_path(app, page_id, section.id)
        src = tree.get(path)
        if src is None:
            missing_components.append(path)
            missing_success.append(section.id)
            continue
        if f'data-appkit-section="{section.id}"' not in src:
            stale_components.append(path)
        success = (
            section.content.success_message
            if section.content is not None and section.content.success_message is not None
            else "Thanks — we will be in touch."
        )
        if _ts(success) not in src and success not in src:
            missing_success.append(section.id)
    checks.append(
        VerifyCheck(
            "form_section_component",
            not missing_components and not stale_components,
            (
                "folded form section component(s) exist on their target pages."
                if not missing_components and not stale_components
                else "; ".join(
                    [
                        *(
                            ["missing component(s): " + ", ".join(missing_components)]
                            if missing_components
                            else []
                        ),
                        *(
                            ["component(s) missing section marker: " + ", ".join(stale_components)]
                            if stale_components
                            else []
                        ),
                    ]
                )
            ),
        )
    )
    checks.append(
        VerifyCheck(
            "form_success_message",
            not missing_success,
            (
                "folded form success_message literal(s) are present in the emitted component(s)."
                if not missing_success
                else "success_message literal missing for form section(s): "
                + ", ".join(missing_success)
            ),
        )
    )
    return _result(checks)


def seo_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Pure checks for SEO metadata + robots/sitemap output."""
    del design
    if app is None or app.seo is None:
        return _result(
            [
                VerifyCheck(
                    "seo_head_metadata",
                    False,
                    "the AppSpec has no folded seo metadata to verify.",
                ),
                VerifyCheck(
                    "seo_robots_txt",
                    False,
                    "the AppSpec has no folded seo metadata to derive robots.txt.",
                ),
                VerifyCheck(
                    "seo_sitemap_xml",
                    False,
                    "the AppSpec has no folded seo metadata to derive sitemap.xml.",
                ),
            ]
        )

    seo = app.seo
    base = seo.base_url.rstrip("/")
    site_name = seo.site_name or app.name
    index = tree.get("index.html")
    head_ok = False
    head_reasons: list[str] = []
    if index is None:
        head_reasons.append("no index.html in the workspace")
    else:
        desc = html.escape(seo.site_description, quote=True)
        title = html.escape(site_name, quote=True)
        url = html.escape(base + "/", quote=True)
        expected = [
            f'<meta name="description" content="{desc}" />',
            f'<meta property="og:title" content="{title}" />',
            f'<meta property="og:description" content="{desc}" />',
            '<meta property="og:type" content="website" />',
            f'<meta property="og:url" content="{url}" />',
        ]
        missing = [tag for tag in expected if tag not in index]
        if seo.social_image_url is not None:
            image = html.escape(seo.social_image_url, quote=True)
            if f'<meta property="og:image" content="{image}" />' not in index:
                missing.append("og:image")
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', index)
        if match is None:
            missing.append("JSON-LD WebSite block")
        else:
            try:
                ld = json.loads(match.group(1))
            except json.JSONDecodeError:
                missing.append("parseable JSON-LD")
            else:
                if (
                    ld.get("@type") != "WebSite"
                    or ld.get("name") != site_name
                    or ld.get("description") != seo.site_description
                    or ld.get("url") != base + "/"
                ):
                    missing.append("JSON-LD values matching app.seo")
        if missing:
            head_reasons.append("missing/mismatched head metadata: " + ", ".join(missing))
        head_ok = not head_reasons

    robots = tree.get("public/robots.txt")
    sitemap_url = f"{base}/sitemap.xml"
    robots_ok = (
        robots is not None
        and "User-agent: *\n" in robots
        and "Allow: /\n" in robots
        and f"Sitemap: {sitemap_url}\n" in robots
    )

    sitemap = tree.get("public/sitemap.xml")
    expected_locs = [f"{base}{page.route}" for page in app.pages]
    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap or "")
    sitemap_ok = sitemap is not None and locs == expected_locs

    return _result(
        [
            VerifyCheck(
                "seo_head_metadata",
                head_ok,
                "index.html head carries description, OG tags, and JSON-LD from app.seo."
                if head_ok
                else "; ".join(head_reasons),
            ),
            VerifyCheck(
                "seo_robots_txt",
                robots_ok,
                "public/robots.txt exists and points at the generated sitemap."
                if robots_ok
                else "public/robots.txt is missing or does not allow all + reference the sitemap.",
            ),
            VerifyCheck(
                "seo_sitemap_xml",
                sitemap_ok,
                "public/sitemap.xml lists every AppSpec page route."
                if sitemap_ok
                else (
                    "public/sitemap.xml is missing or has wrong routes: "
                    f"expected {expected_locs}, got {locs}"
                ),
            ),
        ]
    )


def collection_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Pure checks for folded collection sections.

    Reality note: lead_gen/directory/records components render collection items
    through generated `content.ts`, so item literals live there rather than inside
    the TSX component source.
    """
    del design
    if app is None:
        return _result(
            [
                VerifyCheck(
                    "collection_sections_render",
                    False,
                    "no .disco/appspec.json — cannot locate folded collection sections.",
                ),
                VerifyCheck(
                    "collection_items_present",
                    False,
                    "no .disco/appspec.json — cannot verify folded collection items.",
                ),
            ]
        )
    sections: list[tuple[str, Section]] = [
        (page.id, section)
        for page in app.pages
        for section in page.sections
        if section.kind == "list" and section.id.startswith("collection-")
    ]
    if not sections:
        return _result(
            [
                VerifyCheck(
                    "collection_sections_render",
                    False,
                    "the AppSpec has no folded collection-* list sections.",
                ),
                VerifyCheck(
                    "collection_items_present",
                    False,
                    "the AppSpec has no folded collection items to verify.",
                ),
            ]
        )

    missing_components: list[str] = []
    stale_components: list[str] = []
    for page_id, section in sections:
        path = _component_path(app, page_id, section.id)
        src = tree.get(path)
        if src is None:
            missing_components.append(path)
        elif f'data-appkit-section="{section.id}"' not in src or "c.items" not in src:
            stale_components.append(path)

    content_ts = tree.get("src/generated/content.ts", "")
    missing_items: list[str] = []
    for _page_id, section in sections:
        if section.content is None:
            continue
        for item in section.content.items:
            if _ts(item) not in content_ts and item not in content_ts:
                missing_items.append(f"{section.id}: {item}")

    return _result(
        [
            VerifyCheck(
                "collection_sections_render",
                not missing_components and not stale_components,
                "folded collection section component(s) render on their target pages."
                if not missing_components and not stale_components
                else "; ".join(
                    [
                        *(
                            ["missing component(s): " + ", ".join(missing_components)]
                            if missing_components
                            else []
                        ),
                        *(
                            [
                                "component(s) missing collection render markers: "
                                + ", ".join(stale_components)
                            ]
                            if stale_components
                            else []
                        ),
                    ]
                ),
            ),
            VerifyCheck(
                "collection_items_present",
                not missing_items,
                "folded collection item literal(s) are present in generated content.ts."
                if not missing_items
                else "missing folded collection item literal(s): " + ", ".join(missing_items),
            ),
        ]
    )


__all__ = [
    "collection_verify",
    "directory_verify",
    "form_verify",
    "lead_gen_verify",
    "seo_verify",
]
