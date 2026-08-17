"""The AppKit strict verifier's check batteries: primitive-verify dispatch,
applied-primitive enforcement, mandatory security-primitive static+live
verification, design-lint, and the browser route/section coverage phase.

Each check family is a module-level function taking the data it needs and
returning `_check(...)` dict(s) — extracted from `VerifyAppKitAppTool`
verbatim (evidence strings, check names, ordering, and fail-closed semantics
all unchanged). Splitting these out of the class is what clears both their
logical-size AND (via the guard-clause extractions below) their cyclomatic
complexity: relocating alone does not reduce a callable's own McCabe score,
so `_static_primitive_result` / `_live_primitive_result` and
`_resolve_applied_primitive_record` / `_applied_primitive_record_check` each
own a real decision cluster the parent no longer branches on directly.

`route_coverage` / `section_coverage` are BY DESIGN browser paths that cannot
pass in a headless sandbox (deferred register entry #6, owned by another
program) — moved verbatim, semantics untouched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from disco.core.appkit import get_primitive, resolve_primitive
from disco.core.appkit.primitive_verify import lead_gen_verify
from disco.core.appkit.primitives import PrimitiveDefinition, PrimitiveVerifyResult
from disco.core.appkit.spec import AppSpec, DesignSpec

from ..browser import BrowserArgs, BrowserTool
from ..design_lint import DesignLintArgs, DesignLintTool
from ..verify_app import VerifyWebAppArgs, VerifyWebAppTool
from .constants import _PRIMITIVES_RELDIR
from .preview_lifecycle import PreviewLifecycle, _PreparedPreview

if TYPE_CHECKING:
    from ...anatomy import ToolContext
    from .sandbox_reader import SandboxReader


def _check(name: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "evidence": evidence}


# ---- primitive-verify dispatch --------------------------------------------------


def primitive_result_checks(result: PrimitiveVerifyResult, prim: object) -> list[dict[str, Any]]:
    """Map a `PrimitiveVerifyResult` into the verdict's `_check` dicts 1:1. A
    verify hook that reports NO per-check breakdown still lands ONE named check
    — an empty tuple must never read as a silent pass (no false affordance)."""
    if result.checks:
        return [_check(c.name, c.passed, c.evidence) for c in result.checks]
    prim_id = getattr(prim, "id", None) or "lead_gen"
    return [
        _check(
            f"primitive_verify:{prim_id}",
            bool(result.ok),
            result.detail or "the primitive verify hook reported no checks.",
        )
    ]


def dispatch_primitive_checks(
    app: AppSpec | None, design: DesignSpec | None, tree: dict[str, str]
) -> list[dict[str, Any]]:
    """Resolve `app`'s primitive and run its verify hook (WO-A3 dispatch): a
    missing/unreadable app resolves to no primitive, and a primitive WITHOUT a
    verify hook falls back to `lead_gen_verify`. Records owns its own artifact-
    derived verifier and therefore never inherits lead-generation assumptions."""
    prim = resolve_primitive(app.app_kind) if app is not None else None
    verify_fn = prim.verify if (prim is not None and prim.verify is not None) else lead_gen_verify
    result = verify_fn(app, design, tree)
    return primitive_result_checks(result, prim)


# ---- A3.2 applied-primitive enforcement (fail-closed) ----------------------------


def _resolve_applied_primitive_record(
    name: str, raw: str | None
) -> tuple[PrimitiveDefinition | None, str, str]:
    """Parse an applied-primitive provenance filename + its raw JSON into the
    resolved primitive (or None for an unknown/stale record), the check name to
    report under, and the display id used in a stale-record's evidence string."""
    prim_id = ""
    try:
        record = json.loads(raw or "")
        if isinstance(record, dict):
            prim_id = str(record.get("primitive_id") or "")
    except json.JSONDecodeError:
        prim_id = ""
    display_id = prim_id or name.removesuffix(".json")
    prim = get_primitive(prim_id) if prim_id else None
    return prim, f"primitive_verify:{display_id}", display_id


def _applied_primitive_record_check(
    prim: PrimitiveDefinition | None,
    app: AppSpec | None,
    design: DesignSpec | None,
    tree: dict[str, str],
    relpath: str,
    raw: str | None,
    check_name: str,
    display_id: str,
    required_ids: frozenset[str],
) -> dict[str, Any] | None:
    """Evaluate one applied-primitive provenance record into its verdict check
    (or None when the record needs no independent check here: it's covered by
    the mandatory security path, or it's a fillable primitive with no verify
    hook — the model owns that output)."""
    if prim is None:
        return _check(
            check_name,
            False,
            f"applied-primitive record {relpath} names an unknown "
            f"primitive {display_id!r} — stale record (fail-closed).",
        )
    if prim.id in required_ids:
        # The mandatory AppSpec-derived security path already checked this
        # exact record and dispatched the live exploit harness.
        return None
    if prim.verify is None:
        if prim.tier == "template_only":
            return _check(
                check_name,
                False,
                f"template_only primitive '{prim.id}' declares no verify "
                "harness — cannot ship unverified (fail-closed).",
            )
        # fillable + no verify: nothing to enforce — the model owns the output.
        return None
    record_tree = dict(tree)
    if raw is not None:
        record_tree[relpath] = raw
    result = prim.verify(app, design, record_tree)
    evidence = result.detail
    first_fail = next((c for c in result.checks if not c.passed), None)
    if first_fail is not None:
        evidence = f"{result.detail}; first failure: {first_fail.name} — {first_fail.evidence}"
    return _check(check_name, bool(result.ok), evidence)


async def applied_primitive_checks(
    ctx: ToolContext,
    sandbox: SandboxReader,
    app: AppSpec | None,
    design: DesignSpec | None,
    tree: dict[str, str],
    *,
    required_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """A3.2 — applied-primitive enforcement (fail-closed). Every
    `.disco/primitives/*.json` provenance record (written by app_add_primitive)
    lands a `primitive_verify:<id>` check:

    * an unknown/unparseable primitive id FAILS (stale record);
    * a template_only primitive with NO verify hook FAILS (cannot ship
      unverified — fail-closed);
    * a primitive WITH a verify hook (any tier) runs it against the same
      app/design/tree and reports its result (first failing sub-check named).

    A fillable primitive without a verify hook lands no check — only
    template_only output is Disco-owned and gate-enforced."""
    assert ctx.sandbox is not None
    try:
        names = await ctx.sandbox.list_dir(_PRIMITIVES_RELDIR)
    except Exception:  # noqa: BLE001 — no applied-primitive records
        return []
    checks: list[dict[str, Any]] = []
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        relpath = f"{_PRIMITIVES_RELDIR}/{name}"
        raw = await sandbox.read_text(ctx, relpath)
        prim, check_name, display_id = _resolve_applied_primitive_record(name, raw)
        record_check = _applied_primitive_record_check(
            prim, app, design, tree, relpath, raw, check_name, display_id, required_ids
        )
        if record_check is not None:
            checks.append(record_check)
    return checks


# ---- mandatory security-primitive static+live verification ----------------------


def _verify_result_problem(result: PrimitiveVerifyResult) -> str | None:
    if not result.detail.strip() or not result.checks:
        return "primitive verifier returned an empty result (fail-closed)."
    names = [item.name for item in result.checks]
    if any(not item.name.strip() or not item.evidence.strip() for item in result.checks):
        return "primitive verifier returned an empty check name/evidence (fail-closed)."
    if len(set(names)) != len(names):
        return "primitive verifier returned duplicate check names (fail-closed)."
    checks_ok = all(item.passed for item in result.checks)
    if result.ok != checks_ok:
        return "primitive verifier result is inconsistent with its check breakdown (fail-closed)."
    return None


def _static_primitive_result(
    prim: PrimitiveDefinition,
    app: AppSpec | None,
    design: DesignSpec | None,
    tree: dict[str, str],
    static_name: str,
    live_id: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Run the static half of a security primitive's verification. Returns
    `(static_check, blocking_live_evidence)`; `blocking_live_evidence` is
    `None` exactly when the static result is trustworthy AND passing, i.e.
    live verification should proceed."""
    if prim.verify is None or live_id is None:
        return (
            _check(
                static_name,
                False,
                f"security primitive {prim.id!r} is missing static/live verification wiring.",
            ),
            "live verification was not dispatched.",
        )
    static_result = prim.verify(app, design, tree)
    static_consistency = _verify_result_problem(static_result)
    if static_consistency is not None:
        return (
            _check(static_name, False, static_consistency),
            "live verification withheld because static verification is invalid.",
        )
    static_failure = next((item for item in static_result.checks if not item.passed), None)
    static_evidence = static_result.detail
    if static_failure is not None:
        static_evidence = (
            f"{static_result.detail}; first failure: {static_failure.name} — "
            f"{static_failure.evidence}"
        )
    static_check = _check(static_name, static_result.ok, static_evidence)
    if not static_result.ok:
        return static_check, "live verification withheld until trusted static checks pass."
    return static_check, None


async def _live_primitive_result(
    ctx: ToolContext,
    prim: PrimitiveDefinition,
    live_id: str,
    app: AppSpec,
    design: DesignSpec,
    tree: dict[str, str],
    live_name: str,
) -> dict[str, Any]:
    """Run the live half of a security primitive's verification through the
    host-injected callback. Missing wiring, exceptions, wrong/empty results,
    and unknown/missing exploit checks all fail closed."""
    callback = ctx.primitive_live_verifier
    if callback is None:
        return _check(live_name, False, "host live verifier is not wired (fail-closed).")
    try:
        live_result = await callback(live_id, app, design, tree)
    except Exception as exc:  # noqa: BLE001 - host failures become a closed gate
        return _check(
            live_name, False, f"host live verifier raised {type(exc).__name__} (fail-closed)."
        )
    if not isinstance(live_result, PrimitiveVerifyResult):
        return _check(live_name, False, "host live verifier returned an unknown result shape.")
    live_problem = _verify_result_problem(live_result)
    if live_problem is not None:
        return _check(live_name, False, live_problem)
    actual_live_checks = frozenset(item.name for item in live_result.checks)
    if actual_live_checks != frozenset(prim.live_verify_checks):
        return _check(
            live_name,
            False,
            "host live verifier returned unknown/missing exploit checks (fail-closed).",
        )
    live_failure = next((item for item in live_result.checks if not item.passed), None)
    live_evidence = live_result.detail
    if live_failure is not None:
        live_evidence = (
            f"{live_result.detail}; first failure: {live_failure.name} — {live_failure.evidence}"
        )
    return _check(live_name, live_result.ok, live_evidence)


async def security_primitive_checks(
    ctx: ToolContext,
    prim: PrimitiveDefinition,
    app: AppSpec | None,
    design: DesignSpec | None,
    tree: dict[str, str],
) -> list[dict[str, Any]]:
    """Run the pure and live halves of a mandatory security primitive.

    The live callback is an injected host capability, not a registry global.
    Missing wiring, exceptions, wrong/empty results, duplicate checks, and an
    ``ok`` bit inconsistent with the check breakdown all fail closed.
    """
    static_name = f"security_primitive:{prim.id}"
    live_id = prim.live_verify_id
    live_name = f"primitive_live:{live_id or prim.id}"
    static_check, blocked = _static_primitive_result(prim, app, design, tree, static_name, live_id)
    if blocked is not None:
        return [static_check, _check(live_name, False, blocked)]
    if app is None or design is None:
        return [
            static_check,
            _check(
                live_name,
                False,
                "valid AppSpec and DesignSpec are required for live verification.",
            ),
        ]
    assert live_id is not None  # `blocked` is None only when live_id resolved above
    live_check = await _live_primitive_result(ctx, prim, live_id, app, design, tree, live_name)
    return [static_check, live_check]


# ---- design_lint_clean -----------------------------------------------------------


async def design_lint_check(ctx: ToolContext) -> dict[str, Any]:
    out = await DesignLintTool().run(DesignLintArgs(root="."), ctx)
    st = out.structured or {}
    if not out.success or not st:
        return _check("design_lint_clean", False, "design_lint could not scan the workspace.")
    if st.get("ok"):
        return _check(
            "design_lint_clean",
            True,
            f"no design slop across {st.get('scanned_files', 0)} scanned file(s).",
        )
    return _check("design_lint_clean", False, st.get("summary", "design slop found."))


# ---- route_coverage / section_coverage (the W-45 browser path) ------------------


async def _browser_checks(
    ctx: ToolContext, app: AppSpec | None, url: str
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """Drive the W-45 browser path for route_coverage + section_coverage and
    return (embedded verify_web_app verdict, route_check, section_check)."""
    vtool = VerifyWebAppTool()
    base = (url or "").strip() or await vtool._detect_preview_url(ctx)
    base = base.rstrip("/") or base

    routes = ["/"]
    if app is not None:
        seen: set[str] = set()
        routes = []
        for page in app.pages:
            if page.route not in seen:
                seen.add(page.route)
                routes.append(page.route)
        routes = routes or ["/"]

    embedded: dict[str, Any] | None = None
    route_failures: list[str] = []
    for route in routes:
        route_url = base if route == "/" else base + "/" + route.lstrip("/")
        out = await vtool.run(VerifyWebAppArgs(url=route_url), ctx)
        rv = out.structured or {}
        if embedded is None:
            embedded = rv  # the first (base) route is the structural verdict
        if not rv.get("passed"):
            route_failures.append(f"{route}: {rv.get('summary', 'did not pass')}")

    if route_failures:
        route_check = _check(
            "route_coverage",
            False,
            "; ".join(route_failures[:3]),
        )
    else:
        route_check = _check(
            "route_coverage",
            True,
            f"all {len(routes)} route(s) rendered 2xx with no console/network errors.",
        )

    # section_coverage — every section's data-appkit-section marker in the DOM.
    section_check = await _check_sections(ctx, app, base, routes)
    return embedded, route_check, section_check


async def _check_sections(
    ctx: ToolContext, app: AppSpec | None, base: str, routes: list[str]
) -> dict[str, Any]:
    if app is None:
        return _check("section_coverage", False, "no AppSpec to enumerate sections from.")
    expected = {s.id for page in app.pages for s in page.sections}
    if not expected:
        return _check("section_coverage", True, "no sections declared.")
    found: set[str] = set()
    for route in routes:
        route_url = base if route == "/" else base + "/" + route.lstrip("/")
        out = await BrowserTool().run(BrowserArgs(action="navigate", url=route_url), ctx)
        if out.success and out.structured:
            for marker in out.structured.get("appkit_sections", []) or []:
                if marker:
                    found.add(str(marker))
    missing = sorted(expected - found)
    if missing:
        return _check(
            "section_coverage",
            False,
            "section(s) declared in the AppSpec did NOT render (no data-appkit-section "
            f"marker in the DOM): {', '.join(missing)}.",
        )
    return _check(
        "section_coverage",
        True,
        f"all {len(expected)} AppSpec section(s) rendered (data-appkit-section markers present).",
    )


@dataclass(frozen=True)
class _BrowserPhaseResult:
    embedded: dict[str, Any] | None
    route_check: dict[str, Any]
    section_check: dict[str, Any]
    retain_prepared_runtime: bool
    prepared: _PreparedPreview


async def browser_phase_checks(
    ctx: ToolContext, preview: PreviewLifecycle, app: AppSpec | None, url: str
) -> _BrowserPhaseResult:
    """Prepare the managed Vite preview, drive route_coverage + section_coverage
    against it, and stop any preview this call started UNLESS its runtime is
    being retained for the sealed-output identity check."""
    prepared = await preview.prepare_for_browser_checks(ctx, url)
    retain_prepared_runtime = False
    try:
        if prepared.failure_evidence is not None:
            embedded: dict[str, Any] | None = {
                "verdict": "fail",
                "passed": False,
                "url": prepared.url,
                "summary": prepared.failure_evidence,
                "failure_fingerprint": hashlib.sha256(
                    prepared.failure_evidence.encode("utf-8")
                ).hexdigest()[:16],
            }
            route_check = _check("route_coverage", False, prepared.failure_evidence)
            section_check = _check("section_coverage", False, prepared.failure_evidence)
        else:
            embedded, route_check, section_check = await _browser_checks(ctx, app, prepared.url)
            retain_prepared_runtime = bool(
                embedded
                and embedded.get("passed") is True
                and route_check["passed"]
                and section_check["passed"]
            )
    finally:
        if prepared.stop_name is not None and not retain_prepared_runtime:
            await preview.stop(ctx, prepared.stop_name)
    return _BrowserPhaseResult(
        embedded=embedded,
        route_check=route_check,
        section_check=section_check,
        retain_prepared_runtime=retain_prepared_runtime,
        prepared=prepared,
    )
