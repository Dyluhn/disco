"""Two small, cohesive decision clusters `local_verify.check_schema_sql` and
`local_verify.check_drizzle_schema` delegate to: the "every REQUIRED column
rejects NULL" probe loop, and the "package.json declares the Drizzle runtime +
tooling dependencies" check. Extracted verbatim (same SQL, same reason strings)
so the owning functions stay thin orchestrators, and moved to this parts module
(rather than staying inline in `local_verify.py`) to keep that module under its
own module-logical-line budget.

`_required_columns_reject_null` calls back into the pure ``local_verify``
`_representative_value`. That import is performed INSIDE the function body
(never at this module's top level) so the reference is re-resolved on every
call — if a test ever monkeypatches that name on `local_verify`, this caller
observes the patch exactly as a caller still living in `local_verify.py` would.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from disco.core.appkit.local_verify import CheckResult
    from disco.core.appkit.spec import Entity


def _required_columns_reject_null(
    conn: sqlite3.Connection, table: str, lead: Entity, col_list: str, placeholders: str
) -> tuple[str | None, int]:
    """Insert a representative row with each REQUIRED column NULLed in turn and
    confirm the schema's NOT NULL constraint rejects it. Returns (failing_column,
    required_count) — `failing_column` is the name of the first required column
    that WRONGLY accepted NULL, or None if every required column correctly rejects
    it."""
    from disco.core.appkit.local_verify import _representative_value

    required = [f.name for f in lead.fields if f.required]
    for req in required:
        row_vals = [
            None if f.name == req else _representative_value(f.name, f.type) for f in lead.fields
        ]
        try:
            conn.execute(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', row_vals)
        except sqlite3.IntegrityError:
            continue  # good — NULL rejected
        return req, len(required)
    return None, len(required)


def _package_json_declares_drizzle(package_json: str, name: str) -> CheckResult | None:
    """package.json must parse as a JSON object and declare `drizzle-orm` in
    `dependencies` + `drizzle-kit` in `devDependencies`."""
    from disco.core.appkit.local_verify import CheckResult

    try:
        pkg = json.loads(package_json)
    except json.JSONDecodeError as exc:
        return CheckResult(name, False, f"package.json is not valid JSON: {exc}")
    if not isinstance(pkg, dict):
        return CheckResult(name, False, "package.json is not a JSON object.")
    deps = pkg.get("dependencies")
    dev_deps = pkg.get("devDependencies")
    if not isinstance(deps, dict) or "drizzle-orm" not in deps:
        return CheckResult(
            name,
            False,
            "package.json dependencies must declare drizzle-orm.",
        )
    if not isinstance(dev_deps, dict) or "drizzle-kit" not in dev_deps:
        return CheckResult(
            name,
            False,
            "package.json devDependencies must declare drizzle-kit.",
        )
    return None
