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

from collections.abc import Mapping

from .generator import _comp_name, _component_names, resolve_lead_entity
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
from .spec import AppSpec, DesignSpec, Entity
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


def _check_worker_contract(
    worker_ts: str | None, lead: Entity | None
) -> tuple[VerifyCheck | None, WorkerAuthModel | None]:
    """The worker_contract check, extracted from `lead_gen_verify`'s
    `if worker_ts is None / elif lead is not None` decision cluster. Pure port —
    check name and both evidence strings are byte-unchanged. Preserves the
    original's silent no-append when `worker_ts` is present but `lead` is None
    (no early lead resolution) by returning `(None, None)` in that case."""
    if worker_ts is None:
        return (
            VerifyCheck("worker_contract", False, "no worker/index.ts in the workspace."),
            None,
        )
    if lead is None:
        return None, None
    post_ok, inspected_auth, reasons = inspect_worker(worker_ts, lead)
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
    return VerifyCheck("worker_contract", ok, evidence), inspected_auth


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

    worker_check, auth_model = _check_worker_contract(worker_ts, lead)
    if worker_check is not None:
        checks.append(worker_check)

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


# The cohesive per-primitive check clusters live under
# ``primitive_verify_parts`` and are re-imported here so every existing
# `disco.core.appkit.primitive_verify.{form,seo,collection}_verify` import
# path keeps working unchanged.
from .primitive_verify_parts._collection import collection_verify  # noqa: E402
from .primitive_verify_parts._form import form_verify  # noqa: E402
from .primitive_verify_parts._seo import seo_verify  # noqa: E402

__all__ = [
    "collection_verify",
    "directory_verify",
    "form_verify",
    "lead_gen_verify",
    "seo_verify",
]
