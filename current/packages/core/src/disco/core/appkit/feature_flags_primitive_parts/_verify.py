"""Feature flags primitive: the verify hook (seeded table/routes/admin section).

Extracted from ``feature_flags_primitive`` to keep that module's public facade
under the module logical-line budget; the facade re-imports ``feature_flags_verify``
unchanged so every caller keeps importing from
``disco.core.appkit.feature_flags_primitive``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from ..feature_flags_primitive import (
    _FLAGS_GET_ROUTE,
    _FLAGS_TABLE,
    _FLAGS_TOGGLE_ROUTE,
    FeatureFlag,
    feature_flags_for,
)
from ..primitives import PrimitiveVerifyResult, VerifyCheck
from ..spec import AppSpec, DesignSpec


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    n_fail = sum(1 for c in checks if not c.passed)
    return PrimitiveVerifyResult(
        ok=n_fail == 0,
        detail=f"{len(checks) - n_fail} passed / {n_fail} failed",
        checks=tuple(checks),
    )


def _verify_seeded_table(
    app: AppSpec | None, tree: Mapping[str, str]
) -> tuple[VerifyCheck, tuple[FeatureFlag, ...]]:
    if app is None:
        return (
            VerifyCheck(
                "feature_flags_seeded",
                False,
                "no .disco/appspec.json - run app_create and app_add_primitive first.",
            ),
            (),
        )
    try:
        flags = feature_flags_for(app)
    except ValueError as exc:
        return VerifyCheck("feature_flags_seeded", False, str(exc)), ()
    if not flags:
        return (
            VerifyCheck(
                "feature_flags_seeded",
                False,
                "the AppSpec has no folded feature_flags admin marker.",
            ),
            (),
        )
    schema_sql = tree.get("schema.sql")
    if schema_sql is None:
        return VerifyCheck("feature_flags_seeded", False, "missing schema.sql."), flags
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_sql)
        rows = conn.execute(
            f'SELECT "key", "description", "enabled" FROM "{_FLAGS_TABLE}" ORDER BY "key"'
        ).fetchall()
    except sqlite3.Error as exc:
        return (
            VerifyCheck(
                "feature_flags_seeded",
                False,
                f"schema.sql did not create and seed {_FLAGS_TABLE}: {exc}",
            ),
            flags,
        )
    finally:
        conn.close()
    expected = sorted((f.key, f.description, 1 if f.enabled else 0) for f in flags)
    ok = rows == expected
    evidence = (
        f"{_FLAGS_TABLE} contains the spec keys ({', '.join(f.key for f in flags)})."
        if ok
        else (f"{_FLAGS_TABLE} rows do not match the spec (rows={rows!r}, expected={expected!r}).")
    )
    return (
        VerifyCheck(
            "feature_flags_seeded",
            ok,
            evidence,
        ),
        flags,
    )


def _verify_public_route(worker_ts: str | None) -> VerifyCheck:
    if worker_ts is None:
        return VerifyCheck("feature_flags_public_route", False, "missing worker/index.ts.")
    enabled_filter = 'WHERE "enabled" = 1' in worker_ts or 'WHERE \\"enabled\\" = 1' in worker_ts
    ok = (
        f'url.pathname === "{_FLAGS_GET_ROUTE}" && request.method === "GET"' in worker_ts
        and enabled_filter
        and "return json({ flags: enabled });" in worker_ts
    )
    return VerifyCheck(
        "feature_flags_public_route",
        ok,
        "GET /api/_flags selects only enabled rows and returns the enabled map."
        if ok
        else "GET /api/_flags is missing or does not filter to enabled rows.",
    )


def _verify_toggle_route(worker_ts: str | None) -> VerifyCheck:
    if worker_ts is None:
        return VerifyCheck("feature_flags_toggle_owner_gated", False, "missing worker/index.ts.")
    route = f'url.pathname === "{_FLAGS_TOGGLE_ROUTE}" && request.method === "POST"'
    route_pos = worker_ts.find(route)
    auth_pos = worker_ts.find("if (!isAuthorized(request, env))", route_pos)
    body_pos = worker_ts.find("body = await request.json();", route_pos)
    token_ok = (
        "const expected = env.ADMIN_TOKEN;" in worker_ts
        and "if (!expected) return false;" in worker_ts
    )
    ok = route_pos >= 0 and auth_pos > route_pos and body_pos > auth_pos and token_ok
    return VerifyCheck(
        "feature_flags_toggle_owner_gated",
        ok,
        "POST /api/_flags/toggle checks the ADMIN_TOKEN-backed isAuthorized guard "
        "before reading the body."
        if ok
        else (
            "POST /api/_flags/toggle is missing or does not guard with ADMIN_TOKEN before mutation."
        ),
    )


def _verify_admin_section(tree: Mapping[str, str], flags: tuple[FeatureFlag, ...]) -> VerifyCheck:
    components = [
        src
        for path, src in tree.items()
        if path.startswith("src/components/") and path.endswith(".tsx")
    ]
    found = next((src for src in components if "feature-flags-admin" in src), None)
    ok = found is not None and all(flag.key in found for flag in flags)
    return VerifyCheck(
        "feature_flags_admin_section",
        ok,
        "the SPA emits a feature-flags admin section with every spec key."
        if ok
        else "no generated feature-flags admin section contains every spec key.",
    )


def feature_flags_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Verify the applied feature_flags primitive using pure tree/spec checks."""
    del design
    checks: list[VerifyCheck] = []
    seeded, flags = _verify_seeded_table(app, tree)
    checks.append(seeded)
    worker_ts = tree.get("worker/index.ts")
    checks.append(_verify_public_route(worker_ts))
    checks.append(_verify_toggle_route(worker_ts))
    checks.append(_verify_admin_section(tree, flags))
    return _result(checks)
