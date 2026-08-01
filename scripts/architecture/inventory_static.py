"""Exact mapping-static test inventory scanner.

This preserves the accepted mapping campaign's bounded source rules.  It is
deliberately separate from runtime collection: parametrized runtime IDs are
not invented here.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .test_inventory_parts import _transitions

_TS_FILE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
_TS_MARKER = re.compile(r"\b(?:describe|it|test)\.(skip|todo|only)\s*\(")
_TS_TEST = re.compile(r"\b(?:it|test)\s*\(\s*([\"'`])(.+?)\1")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_PACKAGE = re.compile(r"PKG-\d{2}-[A-Z0-9-]+")
_TRANSITION_KEYS = {
    "package",
    "root",
    "source_identity_before",
    "source_identity_after",
    "collected_roots",
    "mapping_static_additions",
}
_PKG02 = "PKG-02-GATE"
_PKG02_BEFORE = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"
_SCHEMA = "disclaude-architecture-test-inventory-v1"
_INVENTORY_PATH = "architecture/test-inventory.json"
_ACCEPTED_INVENTORY_COMMIT = "c89f517c95fe3104dd52c9f78b4f94d18f0ec1f7"
_ACCEPTED_INVENTORY_SHA256 = "a79cdd6f6f245694bf7266996569e7355b3be4a53f7869eb21d28f49145fbbac"
_DERIVED_PATHS = {
    "architecture/public-api.json",
    _INVENTORY_PATH,
    "docs/governance/CAMPAIGN-STATUS.md",
    "docs/governance/PROTECTED.sha256",
}
_PKG02_TEST_PATHS = (
    "scripts/architecture/test_inventory.py",
    "tests/architecture/_helpers.py",
    "tests/architecture/test_ci_contract.py",
    "tests/architecture/test_debt.py",
    "tests/architecture/test_diagram.py",
    "tests/architecture/test_generated.py",
    "tests/architecture/test_imports.py",
    "tests/architecture/test_ownership.py",
    "tests/architecture/test_public_api.py",
    "tests/architecture/test_python_scan.py",
    "tests/architecture/test_seal.py",
    "tests/architecture/test_targets.py",
    "tests/architecture/test_test_inventory.py",
    "tests/architecture/test_typescript_scan.py",
)


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return sorted(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def _row_key(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def commit_identity_resolves(root: Path, value: Any) -> bool:
    """Return whether ``value`` is one exact, locally resolvable commit ID."""
    if not isinstance(value, str) or not _GIT_COMMIT.fullmatch(value):
        return False
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "cat-file",
                "-e",
                f"{value}^{{commit}}",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _git_output(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("inventory source identity must resolve to a Git commit") from error


def _inventory_at(root: Path, revision: str) -> tuple[dict[str, Any], bytes]:
    blob = _git_output(root, "show", f"{revision}:{_INVENTORY_PATH}")
    try:
        inventory = json.loads(blob)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Git test inventory is malformed") from error
    if not isinstance(inventory, dict) or inventory.get("schema") != _SCHEMA:
        raise RuntimeError("Git test inventory schema mismatch")
    return inventory, blob


def _pinned_inventory(root: Path) -> tuple[dict[str, Any], bytes]:
    inventory, blob = _inventory_at(root, _ACCEPTED_INVENTORY_COMMIT)
    if hashlib.sha256(blob).hexdigest() != _ACCEPTED_INVENTORY_SHA256:
        raise RuntimeError("pinned test inventory digest mismatch")
    return inventory, blob


def _source_prior(
    root: Path,
    source_identity: Any,
) -> tuple[dict[str, Any], bytes, str]:
    if not isinstance(source_identity, str) or not _GIT_COMMIT.fullmatch(source_identity):
        raise RuntimeError("inventory source_identity must be a full Git commit")
    lineage = (
        _git_output(
            root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            source_identity,
        )
        .decode()
        .split()
    )
    if len(lineage) != 2 or lineage[0] != source_identity:
        raise RuntimeError("inventory source_identity must have exactly one parent")
    _, source_blob = _inventory_at(root, source_identity)
    if lineage[1] == _PKG02_BEFORE:
        prior, _ = _pinned_inventory(root)
        if hashlib.sha256(source_blob).hexdigest() != _ACCEPTED_INVENTORY_SHA256:
            raise RuntimeError("PKG-02 source inventory is not the pinned bootstrap")
    else:
        prior, prior_blob = _inventory_at(root, lineage[1])
        if source_blob != prior_blob:
            raise RuntimeError("source commit changed its parent test inventory")
    return prior, source_blob, lineage[1]


def candidate_prior(root: Path, source_identity: Any) -> dict[str, Any]:
    """Validate HEAD as the source or its derived-only final sibling."""
    prior, _, parent = _source_prior(root, source_identity)
    head = _git_output(root, "rev-parse", "HEAD").decode().strip()
    if head == source_identity:
        return prior
    lineage = _git_output(root, "rev-list", "--parents", "-n", "1", head).decode().split()
    if len(lineage) != 2 or lineage[0] != head or lineage[1] != parent:
        raise RuntimeError("test inventory candidate is not the source sibling")
    changed = set(
        _git_output(root, "diff", "--name-only", source_identity, head, "--").decode().splitlines()
    )
    if not changed <= _DERIVED_PATHS:
        raise RuntimeError(f"test inventory candidate has non-derived drift: {sorted(changed)}")
    return prior


def candidate_problems(root: Path, source_identity: Any) -> list[str]:
    """Return a fail-closed checker problem for invalid candidate lineage."""
    try:
        candidate_prior(root, source_identity)
    except RuntimeError as error:
        return [f"test inventory source authority: {error}"]
    return []


def regeneration_prior(
    root: Path,
    source_identity: Any,
) -> tuple[str, dict[str, Any]]:
    """Require exact source checkout and committed prewrite authority bytes."""
    prior, source_blob, _ = _source_prior(root, source_identity)
    head = _git_output(root, "rev-parse", "HEAD").decode().strip()
    if source_identity != head:
        raise RuntimeError("supplied inventory source_identity must equal HEAD")
    try:
        working_blob = (root / _INVENTORY_PATH).read_bytes()
    except OSError as error:
        raise RuntimeError("working prewrite test inventory is unavailable") from error
    if working_blob != source_blob:
        raise RuntimeError("working prewrite inventory must equal the source commit blob")
    return source_identity, prior


def _commit_identity(
    root: Path,
    value: Any,
    label: str,
    problems: list[str],
    cache: dict[str, bool],
) -> str | None:
    if not isinstance(value, str) or not _GIT_COMMIT.fullmatch(value):
        problems.append(f"{label} must be a full lowercase Git commit SHA")
        return None
    if value not in cache:
        cache[value] = commit_identity_resolves(root, value)
    if not cache[value]:
        problems.append(f"{label} does not resolve to a Git commit: {value}")
    return value


def _ids_owned_by_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    paths = set(_PKG02_TEST_PATHS)
    return [item for item in value if isinstance(item, str) and item.partition("::")[0] in paths]


def _check_pkg02_exact_additions(
    row: dict[str, Any],
    claims: dict[str, set[str]],
    baseline: dict[str, Any],
    problems: list[str],
) -> None:
    collected = row.get("collected_roots")
    tests = collected.get("tests") if isinstance(collected, dict) else {}
    actual_collected = tests.get("added_ids") if isinstance(tests, dict) else None
    owned_collected = set(actual_collected) if isinstance(actual_collected, list) else set()
    later_collected = claims.get("collected_roots.tests", set()) - owned_collected
    current_collected = baseline.get("collected", {}).get("roots", {}).get("tests")
    frozen_collected = [
        node_id
        for node_id in _ids_owned_by_paths(current_collected)
        if node_id not in later_collected
    ]
    if actual_collected != frozen_collected:
        problems.append("PKG-02-GATE collected additions must equal its frozen live test IDs")

    additions = row.get("mapping_static_additions")
    if not isinstance(additions, dict):
        return
    if additions.get("python_test_files") != list(_PKG02_TEST_PATHS):
        problems.append("PKG-02-GATE test-file additions must equal its frozen 14 paths")
    current_mapping = baseline.get("mapping_static")
    current_static = (
        current_mapping.get("python_static_test_ids") if isinstance(current_mapping, dict) else None
    )
    actual_static = additions.get("python_static_test_ids")
    owned_static = set(actual_static) if isinstance(actual_static, list) else set()
    later_static = claims.get("mapping_static.python_static_test_ids", set()) - owned_static
    frozen_static = [
        node_id for node_id in _ids_owned_by_paths(current_static) if node_id not in later_static
    ]
    if additions.get("python_static_test_ids") != frozen_static:
        problems.append("PKG-02-GATE static additions must equal its frozen live test IDs")


def _check_pkg02_transition(
    transitions: list[dict[str, Any]],
    baseline: dict[str, Any],
    claims: dict[str, set[str]],
    problems: list[str],
) -> None:
    rows = [row for row in transitions if row.get("package") == _PKG02]
    if len(rows) != 1:
        problems.append("additive_transitions must contain exactly one PKG-02-GATE row")
        return
    row = rows[0]
    collected = row.get("collected_roots")
    tests = collected.get("tests") if isinstance(collected, dict) else None
    additions = row.get("mapping_static_additions")
    test_facts = (
        row.get("root"),
        row.get("source_identity_before"),
        set(collected) if isinstance(collected, dict) else None,
        tests.get("before_count") if isinstance(tests, dict) else None,
        tests.get("after_count") if isinstance(tests, dict) else None,
        len(tests.get("added_ids", []))
        if isinstance(tests, dict) and isinstance(tests.get("added_ids"), list)
        else None,
    )
    expected_test_facts = (
        "tests",
        _PKG02_BEFORE,
        {"tests"},
        25,
        327,
        302,
    )
    if test_facts != expected_test_facts:
        problems.append("PKG-02-GATE transition must be the exact tests 25->327/+302 relation")
    if not isinstance(additions, dict):
        return
    mapping_facts = (
        len(additions["python_test_files"])
        if isinstance(additions.get("python_test_files"), list)
        else None,
        len(additions["python_static_test_ids"])
        if isinstance(additions.get("python_static_test_ids"), list)
        else None,
        additions.get("typescript_test_files"),
        additions.get("typescript_static_test_ids"),
        additions.get("fixtures"),
    )
    if mapping_facts != (14, 302, [], [], []):
        problems.append("PKG-02-GATE mapping-static addition relation is not exact")
    _check_pkg02_exact_additions(row, claims, baseline, problems)


def _check_identity_chain(
    transitions: list[dict[str, Any]],
    source_identity: str | None,
    problems: list[str],
) -> None:
    next_identity: dict[str, str] = {}
    for row in transitions:
        before = row.get("source_identity_before")
        after = row.get("source_identity_after")
        if not isinstance(before, str) or not isinstance(after, str):
            continue
        if before in next_identity:
            problems.append(f"additive-transition identity chain branches at {before}")
            continue
        next_identity[before] = after
    seen: set[str] = set()
    cursor = _PKG02_BEFORE
    while cursor in next_identity and cursor not in seen:
        seen.add(cursor)
        cursor = next_identity[cursor]
    if cursor in seen:
        problems.append("additive-transition identity chain contains a cycle")
        return
    if len(seen) != len(transitions):
        problems.append("additive-transition identity chain is disconnected")
    if cursor != source_identity:
        problems.append("source_identity must equal the latest additive-transition identity")


def _check_transition_row(
    row: dict[str, Any],
    index: int,
    baseline: dict[str, Any],
    root: Path,
    cache: dict[str, bool],
    claims: dict[str, set[str]],
    packages: set[str],
    after_identities: set[str],
    problems: list[str],
) -> dict[str, Any] | None:
    label = f"additive_transitions[{index}]"
    if set(row) != _TRANSITION_KEYS:
        problems.append(f"{label} schema mismatch")
        return None
    package = row["package"]
    if not isinstance(package, str) or not _PACKAGE.fullmatch(package):
        problems.append(f"{label}.package is invalid: {package!r}")
    elif package in packages:
        problems.append(f"duplicate additive-transition package: {package}")
    else:
        packages.add(package)
    before = _commit_identity(
        root,
        row["source_identity_before"],
        f"{label}.source_identity_before",
        problems,
        cache,
    )
    after = _commit_identity(
        root,
        row["source_identity_after"],
        f"{label}.source_identity_after",
        problems,
        cache,
    )
    if before is not None and before == after:
        problems.append(f"{label} before/after identities must differ")
    if after is not None and after in after_identities:
        problems.append(f"duplicate additive-transition after identity: {after}")
    if after is not None:
        after_identities.add(after)
    collected_count = _transitions.check_collected_additions(row, baseline, claims, problems)
    mapping_count = _transitions.check_mapping_additions(row, baseline, claims, problems)
    if collected_count + mapping_count == 0:
        _transitions.check_null_advance(row, problems)
    return row


def check_inventory_metadata(
    baseline: dict[str, Any],
    root: Path,
) -> list[str]:
    """Validate source identity and owned additive-transition authority."""
    problems: list[str] = []
    cache: dict[str, bool] = {}
    source = _commit_identity(
        root, baseline.get("source_identity"), "source_identity", problems, cache
    )
    mapping = baseline.get("mapping_static")
    mapping_identity = mapping.get("identity") if isinstance(mapping, dict) else None
    _commit_identity(root, mapping_identity, "mapping_static.identity", problems, cache)
    if mapping_identity != source:
        problems.append("mapping_static.identity must equal source_identity")

    transitions = baseline.get("additive_transitions")
    if not isinstance(transitions, list) or not all(isinstance(row, dict) for row in transitions):
        problems.append("additive_transitions must be an exact object list")
        return problems
    if transitions != sorted(transitions, key=_row_key):
        problems.append("additive_transitions must be canonically sorted")

    claims: dict[str, set[str]] = {}
    packages: set[str] = set()
    after_identities: set[str] = set()
    valid_rows: list[dict[str, Any]] = []
    for index, row in enumerate(transitions):
        checked = _check_transition_row(
            row,
            index,
            baseline,
            root,
            cache,
            claims,
            packages,
            after_identities,
            problems,
        )
        if checked is not None:
            valid_rows.append(checked)
    _check_pkg02_transition(valid_rows, baseline, claims, problems)
    _check_identity_chain(valid_rows, source, problems)
    return problems


def candidate_inventory_problems(
    baseline: dict[str, Any],
    root: Path,
) -> list[str]:
    """Validate both immutable metadata and source/final candidate lineage."""
    return candidate_problems(root, baseline.get("source_identity")) + check_inventory_metadata(
        baseline, root
    )


def validate_regenerated_inventory(
    inventory: dict[str, Any],
    root: Path,
) -> None:
    """Reject a constructed authority before any bytes are replaced."""
    problems = check_inventory_metadata(inventory, root)
    if problems:
        raise RuntimeError(f"regenerated test-inventory metadata is invalid: {problems}")


def accepted_mapping_identity(
    baseline: dict[str, Any],
    root: Path,
    mapping: dict[str, Any],
    *,
    explicit: bool,
) -> str:
    """Return one validated identity for inventory regeneration."""
    problems = check_inventory_metadata(baseline, root)
    if problems:
        raise RuntimeError(f"accepted test-inventory metadata is invalid: {problems}")
    identity = mapping.get("identity")
    if not explicit and identity != baseline.get("source_identity"):
        raise RuntimeError("accepted mapping-static identity must equal source identity")
    if not isinstance(identity, str) or not _GIT_COMMIT.fullmatch(identity):
        raise RuntimeError("accepted mapping-static source identity must be a Git commit SHA")
    if not commit_identity_resolves(root, identity):
        raise RuntimeError("accepted mapping-static source identity must resolve to a Git commit")
    return identity


def _python_files(tracked: list[str]) -> list[str]:
    return [
        rel
        for rel in tracked
        if rel.endswith(".py")
        and ("/tests/" in rel or Path(rel).name.startswith("test_") or rel.startswith("tests/"))
    ]


def _typescript_files(tracked: list[str]) -> list[str]:
    return [rel for rel in tracked if _TS_FILE.search(rel)]


def _fixture_row(node: ast.AST, rel: str) -> dict[str, Any] | None:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    decorators = [ast.unparse(item) for item in node.decorator_list]
    fixture_scope = next((item for item in decorators if "fixture" in item), None)
    if fixture_scope is None:
        return None
    return {
        "path": rel,
        "line": node.lineno,
        "fixture": node.name,
        "scope": fixture_scope,
    }


def _marker_row(node: ast.AST, text: str, rel: str) -> dict[str, Any] | None:
    if isinstance(node, ast.Call):
        marker = ast.unparse(node.func)
        if any(name in marker for name in ("pytest.skip", "pytest.xfail")):
            return {
                "framework": "pytest",
                "path": rel,
                "line": node.lineno,
                "marker": marker,
                "source": ast.get_source_segment(text, node) or "",
            }
    if isinstance(node, ast.Attribute):
        marker = ast.unparse(node)
        if any(name in marker for name in ("pytest.mark.skip", "pytest.mark.xfail")):
            return {
                "framework": "pytest",
                "path": rel,
                "line": node.lineno,
                "marker": marker,
                "source": "",
            }
    return None


def _scan_python(
    root: Path, paths: list[str]
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    test_ids: list[str] = []
    fixtures: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for rel in paths:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            parse_errors.append(f"{rel}:{exc.lineno}:{exc.offset}: {exc.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    test_ids.append(f"{rel}::{node.name}")
            fixture = _fixture_row(node, rel)
            if fixture is not None:
                fixtures.append(fixture)
            marker = _marker_row(node, text, rel)
            if marker is not None:
                markers.append(marker)
    return (
        sorted(test_ids),
        sorted(fixtures, key=_row_key),
        sorted(markers, key=_row_key),
        sorted(parse_errors),
    )


def _scan_typescript(root: Path, paths: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    test_ids: list[str] = []
    markers: list[dict[str, Any]] = []
    for rel in paths:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        test_ids.extend(f"{rel}::{match.group(2)}" for match in _TS_TEST.finditer(text))
        for line_number, line in enumerate(text.splitlines(), 1):
            markers.extend(
                {
                    "framework": "vitest/playwright",
                    "path": rel,
                    "line": line_number,
                    "marker": match.group(0).strip(),
                    "source": line.strip(),
                }
                for match in _TS_MARKER.finditer(line)
            )
    return sorted(test_ids), sorted(markers, key=_row_key)


def scan_mapping_static(root: Path) -> dict[str, Any]:
    """Return the exact accepted mapping-static inventory for ``root``."""
    tracked = _tracked_files(root)
    python_files = _python_files(tracked)
    typescript_files = _typescript_files(tracked)
    python_ids, fixtures, python_markers, parse_errors = _scan_python(root, python_files)
    typescript_ids, typescript_markers = _scan_typescript(root, typescript_files)
    return {
        "python_test_file_count": len(python_files),
        "python_static_test_id_count": len(python_ids),
        "python_test_files": python_files,
        "python_static_test_ids": python_ids,
        "typescript_test_file_count": len(typescript_files),
        "typescript_static_test_id_count": len(typescript_ids),
        "typescript_test_files": typescript_files,
        "typescript_static_test_ids": typescript_ids,
        "fixtures": fixtures,
        "markers": sorted(python_markers + typescript_markers, key=_row_key),
        "parse_errors": parse_errors,
    }
