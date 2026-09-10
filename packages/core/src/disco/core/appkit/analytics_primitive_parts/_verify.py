"""Analytics primitive: the WO-A3 verify hook (schema/worker/dashboard/beacon).

Extracted from ``analytics_primitive`` to keep that module's public facade
under the module logical-line budget; the facade re-imports ``analytics_verify``
unchanged so every caller keeps importing from
``disco.core.appkit.analytics_primitive``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping

from ..analytics_primitive import (
    _DASHBOARD_ROUTE,
    _DASHBOARD_SECTION_ID,
    _SUMMARY_ROUTE,
    is_analytics_dashboard_section,
)
from ..primitives import PrimitiveVerifyResult, VerifyCheck
from ..spec import AppSpec, DesignSpec
from ..worker_inspect import _route_handler, _strip_ts_comments


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    n_fail = sum(1 for check in checks if not check.passed)
    return PrimitiveVerifyResult(
        ok=n_fail == 0,
        detail=f"{len(checks) - n_fail} passed / {n_fail} failed",
        checks=tuple(checks),
    )


def _schema_verify(schema_sql: str | None) -> VerifyCheck:
    if schema_sql is None:
        return VerifyCheck("analytics_schema", False, "schema.sql is missing.")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(":memory:")
        conn.executescript(schema_sql)
        rows = conn.execute('PRAGMA table_info("_hits")').fetchall()
        columns = {str(row[1]): row for row in rows}
        expected = {"path", "ts", "referrer", "ua_class"}
        missing = sorted(expected - set(columns))
        if missing:
            return VerifyCheck(
                "analytics_schema",
                False,
                f"_hits table is missing column(s): {', '.join(missing)}.",
            )
        not_null = {name for name, row in columns.items() if int(row[3]) == 1}
        if not {"path", "ts", "ua_class"} <= not_null:
            return VerifyCheck(
                "analytics_schema",
                False,
                "_hits path, ts and ua_class columns must be NOT NULL.",
            )
        conn.execute(
            'INSERT INTO "_hits" ("path", "referrer", "ua_class") VALUES (?, ?, ?)',
            ("/", None, "desktop"),
        )
        count = conn.execute('SELECT COUNT(*) FROM "_hits"').fetchone()
        if count is None or int(count[0]) != 1:
            return VerifyCheck(
                "analytics_schema",
                False,
                "a representative _hits row could not be read back.",
            )
    except sqlite3.Error as exc:
        return VerifyCheck("analytics_schema", False, f"schema.sql is not valid: {exc}")
    finally:
        if conn is not None:
            conn.close()
    return VerifyCheck(
        "analytics_schema",
        True,
        "_hits table exists with path/ts/referrer/ua_class and accepts a hit row.",
    )


def _summary_route_guarded(block: str | None) -> bool:
    if block is None:
        return False
    return (
        re.search(
            r"^\s*\{\s*if\s*\(\s*!\s*isAuthorized\s*\(\s*request\s*,\s*env\s*\)\s*\)"
            r"\s*\{\s*return\s+json\s*\([^;]*,\s*401\s*\)",
            block,
            re.DOTALL,
        )
        is not None
    )


def _worker_verify(worker_ts: str | None) -> VerifyCheck:
    if worker_ts is None:
        return VerifyCheck("analytics_worker_routes", False, "worker/index.ts is missing.")
    src = _strip_ts_comments(worker_ts)
    post_block = _route_handler(
        src,
        r'url\.pathname\s*===\s*"/api/_hits"\s*&&\s*request\.method\s*===\s*"POST"',
    )
    summary_block = _route_handler(
        src,
        r'url\.pathname\s*===\s*"/api/_hits/summary"\s*&&\s*request\.method\s*===\s*"GET"',
    )
    post_ok = (
        post_block is not None
        and "recordHit(request, env, body)" in post_block
        and "hitRateLimited()" in src
        and 'INSERT INTO "_hits"' in src
        and ".bind(" in src
    )
    read_ok = (
        summary_block is not None
        and _summary_route_guarded(summary_block)
        and "listHitAggregates(env)" in summary_block
    )
    if post_ok and read_ok:
        return VerifyCheck(
            "analytics_worker_routes",
            True,
            "worker has public /api/_hits POST with rate counter + parameterized insert "
            "and owner-gated /api/_hits/summary read.",
        )
    reasons: list[str] = []
    if not post_ok:
        reasons.append("missing or incomplete public /api/_hits POST beacon route")
    if not read_ok:
        reasons.append("missing or unguarded owner /api/_hits/summary read route")
    return VerifyCheck("analytics_worker_routes", False, "; ".join(reasons))


def _dashboard_verify(app: AppSpec | None, tree: Mapping[str, str]) -> VerifyCheck:
    app_has_section = bool(
        app is not None
        and app.analytics is not None
        and any(
            page.route == _DASHBOARD_ROUTE
            and any(is_analytics_dashboard_section(section) for section in page.sections)
            for page in app.pages
        )
    )
    component_sources = [
        source
        for path, source in tree.items()
        if path.startswith("src/components/") and path.endswith(".tsx")
    ]
    tree_has_section = any(
        f'id="{_DASHBOARD_SECTION_ID}"' in source
        and _SUMMARY_ROUTE in source
        and "Authorization" in source
        and "Bearer" in source
        for source in component_sources
    )
    ok = app_has_section and tree_has_section
    return VerifyCheck(
        "analytics_dashboard_section",
        ok,
        "AppSpec carries the analytics dashboard section and the generated component "
        "fetches the owner-gated summary route."
        if ok
        else "analytics dashboard section is missing from the AppSpec or generated component.",
    )


def _beacon_verify(main_tsx: str | None) -> VerifyCheck:
    src = _strip_ts_comments(main_tsx or "")
    ok = all(
        needle in src
        for needle in (
            "installAnalyticsBeacon",
            '"/api/_hits"',
            "sendBeacon",
            "pushState",
            "popstate",
        )
    )
    return VerifyCheck(
        "analytics_spa_beacon",
        ok,
        "src/main.tsx installs a no-dependency beacon helper for initial load and route changes."
        if ok
        else "src/main.tsx does not contain the analytics beacon route-change helper.",
    )


def analytics_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """Verify the generated analytics surface structurally and with sqlite."""
    del design
    checks = [
        _schema_verify(tree.get("schema.sql")),
        _worker_verify(tree.get("worker/index.ts")),
        _dashboard_verify(app, tree),
        _beacon_verify(tree.get("src/main.tsx")),
    ]
    return _result(checks)
