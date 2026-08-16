"""Artifact-derived verifier for the AppKit records primitive."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping

from ..primitives import PrimitiveVerifyResult, VerifyCheck
from ..spec import AppSpec, DesignSpec
from .policy_worker import records_policy_enabled


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _exact_file_check(
    name: str,
    path: str,
    tree: Mapping[str, str],
    expected: Mapping[str, str],
) -> VerifyCheck:
    actual = tree.get(path)
    wanted = expected.get(path)
    if actual is None or wanted is None:
        return VerifyCheck(name, False, f"missing generated records artifact: {path}")
    if actual != wanted:
        return VerifyCheck(
            name,
            False,
            f"{path} differs from the AppSpec-derived trusted output "
            f"(actual {_digest(actual)}, expected {_digest(wanted)})",
        )
    return VerifyCheck(name, True, f"{path} matches AppSpec-derived output {_digest(actual)}")


def _schema_check(tree: Mapping[str, str], expected: Mapping[str, str]) -> VerifyCheck:
    exact = _exact_file_check("records_schema", "schema.sql", tree, expected)
    if not exact.passed:
        return exact
    schema = tree["schema.sql"]
    if tree.get("migrations/0001_init.sql") != schema:
        return VerifyCheck(
            "records_schema",
            False,
            "migrations/0001_init.sql must exactly match schema.sql",
        )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(schema)
        fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        return VerifyCheck("records_schema", False, f"schema.sql rejected by SQLite: {exc}")
    finally:
        connection.close()
    if fk_errors:
        return VerifyCheck("records_schema", False, f"schema foreign-key check failed: {fk_errors}")
    return VerifyCheck(
        "records_schema",
        True,
        f"schema and migration match trusted output {_digest(schema)} and execute cleanly",
    )


def records_verify(
    app: AppSpec | None,
    design: DesignSpec | None,
    tree: Mapping[str, str],
) -> PrimitiveVerifyResult:
    """Verify records artifacts against their validated, artifact-derived contract."""
    if app is None or design is None:
        check = VerifyCheck(
            "records_specs",
            False,
            "missing or invalid AppSpec/DesignSpec; run app_create first",
        )
        return PrimitiveVerifyResult(False, check.evidence, (check,))
    if app.app_kind != "records":
        check = VerifyCheck("records_specs", False, "AppSpec app_kind is not records")
        return PrimitiveVerifyResult(False, check.evidence, (check,))
    try:
        from ..records_primitive import generate_records

        expected = generate_records(app, design)
    except Exception as exc:  # noqa: BLE001 - typed verifier failure
        check = VerifyCheck("records_projection", False, f"records projection failed: {exc}")
        return PrimitiveVerifyResult(False, check.evidence, (check,))

    checks = [
        _schema_check(tree, expected),
        _exact_file_check("records_drizzle_contract", "src/db/schema.ts", tree, expected),
        _exact_file_check("records_worker_contract", "worker/index.ts", tree, expected),
    ]
    if records_policy_enabled(app):
        checks.extend(
            (
                _exact_file_check(
                    "records_policy_ui",
                    "src/components/RecordsWorkspace.tsx",
                    tree,
                    expected,
                ),
                _exact_file_check("records_policy_shell", "src/App.tsx", tree, expected),
            )
        )
    ok = all(check.passed for check in checks)
    first_failure = next((check for check in checks if not check.passed), None)
    detail = (
        "records schema, trusted Worker, Drizzle model, and policy UI match the validated AppSpec"
        if ok
        else first_failure.evidence
        if first_failure is not None
        else "records verification failed"
    )
    return PrimitiveVerifyResult(ok, detail, tuple(checks))


__all__ = ["records_verify"]
