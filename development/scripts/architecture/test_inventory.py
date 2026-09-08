"""Fail-closed static and runtime test-inventory gate.

Pure row helpers and the module-split transition authority live in
:mod:`architecture.test_inventory_parts`; see that package's docstring for why
this module keeps the collectors and orchestrators (the adversarial suite
monkeypatches them here) and why the split happened at all — this file is
classified a *test* module by its own filename, so its caps were never the
ones the 10-C handover recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import inventory_static
from .inventory_static import (
    accepted_mapping_identity,
    scan_mapping_static,
)
from .policy import REPO_ROOT, load_json
from .test_inventory_parts import _retirements, _splits
from .test_inventory_parts._rows import (
    LIVE_CONFIGS,
    PLAYWRIGHT_CONFIGS,
    PLAYWRIGHT_TEST_DIRS,
    PYTHON_ROOTS,
    locate_root,
    logical_node_id,
)
from .test_inventory_parts._rows import (
    ROOT_DIRS as ROOT_DIRS,
)
from .test_inventory_parts._rows import canonical_row as _canonical_row
from .test_inventory_parts._rows import check_count as _check_count
from .test_inventory_parts._rows import collection_env as _collection_env
from .test_inventory_parts._rows import collector_commands as _collector_commands
from .test_inventory_parts._rows import compare_exact as _compare_exact
from .test_inventory_parts._rows import forbidden_marker as _forbidden_marker
from .test_inventory_parts._rows import frontend_relative as _frontend_relative
from .test_inventory_parts._rows import stored_rows as _stored_rows
from .test_inventory_parts._rows import stored_strings as _stored_strings

commit_identity_resolves = inventory_static.commit_identity_resolves
SANDBOX_INTEGRATION_DESELECTED_IDS = (
    "packages/tools/tests/test_sandbox_integration.py::test_real_hostconfig_pidmode_is_never_host",
    "packages/tools/tests/test_sandbox_integration.py::test_pid_and_net_namespace_inodes_differ_from_host",
    "packages/tools/tests/test_sandbox_integration.py::test_sealed_box_cannot_reach_host_loopback",
    "packages/tools/tests/test_sandbox_integration.py::test_host_process_survives_every_signal_bypass_form",
    "packages/tools/tests/test_sandbox_integration.py::test_guest_symlink_escape_refused_live",
)
_PLAYWRIGHT_ROW = re.compile(r"^\s+\[[^\]]+\]\s+›\s+(.+?):\d+:\d+\s+›\s+(.+)$")
_PLAYWRIGHT_TOTAL = re.compile(r"^Total:\s+(\d+)\s+tests?\s+in\s+(\d+)\s+files?$")


def load_test_inventory(root: Path | None = None) -> dict[str, Any]:
    """Load the inventory owned by the explicit repository root."""
    resolved_root = REPO_ROOT if root is None else root
    bucketed = resolved_root / "development" / "architecture" / "test-inventory.json"
    if not bucketed.is_file():
        bare = resolved_root / "architecture" / "test-inventory.json"
        if bare.is_file():
            return load_json(bare)
    return load_json(bucketed)


def _collect_pytest_ids(
    root: Path,
    root_dir: str,
    marker: str = "not sandbox_integration",
) -> tuple[list[str], str]:
    # `root_dir` is the LOGICAL root label ("packages", "harness", ...). It is an
    # authority key and a node-ID prefix, so it stays stable across directory
    # moves; ROOT_DIRS maps it to wherever that root actually lives on disk.
    # Resolve against the tree being scanned rather than assuming one layout, so
    # the collector works on the real repo and on a synthetic fixture tree alike.
    disk_dir = locate_root(root, root_dir).relative_to(root).as_posix()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-m",
                marker,
                "--collect-only",
                "-q",
                disk_dir,
            ],
            capture_output=True,
            text=True,
            cwd=root,
            env=_collection_env(root),
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"collection failed for {root_dir}: {exc}"
    if result.returncode:
        detail = "; ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
        return [], f"collection failed for {root_dir}: exit {result.returncode}: {detail}"
    ids = sorted(line.strip() for line in result.stdout.splitlines() if "::" in line.strip())
    ids = sorted(logical_node_id(node) for node in ids)
    if len(ids) != len(set(ids)):
        return [], f"collection failed for {root_dir}: duplicate node IDs"
    return ids, ""


def _check_sandbox_deselections(root: Path, problems: list[str]) -> None:
    ids, error = _collect_pytest_ids(root, "packages", "sandbox_integration")
    if error:
        problems.append(f"sandbox_integration {error}")
    elif ids != sorted(SANDBOX_INTEGRATION_DESELECTED_IDS):
        problems.append(
            "sandbox_integration deselection mismatch: "
            f"expected {sorted(SANDBOX_INTEGRATION_DESELECTED_IDS)}, got {ids}"
        )


def _check_collected_ids(
    baseline: dict[str, Any],
    root: Path,
    problems: list[str],
) -> dict[str, int]:
    collected = baseline.get("collected")
    if not isinstance(collected, dict):
        problems.append("collected authority must be an object")
        collected = {}
    roots = collected.get("roots")
    counts = collected.get("counts")
    if not isinstance(roots, dict) or set(roots) != set(PYTHON_ROOTS):
        problems.append(f"collected.roots must contain exactly {PYTHON_ROOTS}")
        roots = {}
    if not isinstance(counts, dict) or set(counts) != set(PYTHON_ROOTS):
        problems.append(f"collected.counts must contain exactly {PYTHON_ROOTS}")
        counts = {}
    actual_total = 0
    for root_dir in PYTHON_ROOTS:
        actual, error = _collect_pytest_ids(root, root_dir)
        if error:
            problems.append(error)
            continue
        actual_total += len(actual)
        stored = _stored_strings(roots, root_dir, f"collected.roots.{root_dir}", problems)
        _compare_exact(f"collected IDs in {root_dir}", stored, actual, problems)
        _check_count(
            counts,
            root_dir,
            len(actual),
            f"collected.counts.{root_dir}",
            problems,
        )
    _check_count(collected, "total", actual_total, "collected.total", problems)
    return {"collected_total": actual_total}


def _check_mapping_static(
    baseline: dict[str, Any],
    root: Path,
    problems: list[str],
) -> dict[str, int]:
    mapping = baseline.get("mapping_static")
    if not isinstance(mapping, dict):
        problems.append("mapping_static authority must be an object")
        mapping = {}
    actual = scan_mapping_static(root)
    if actual["parse_errors"]:
        problems.append(f"mapping-static parse errors: {actual['parse_errors']}")
    list_fields = (
        ("python_test_files", True),
        ("python_static_test_ids", False),
        ("typescript_test_files", True),
        ("typescript_static_test_ids", False),
    )
    for key, unique in list_fields:
        stored = _stored_strings(mapping, key, f"mapping_static.{key}", problems, unique=unique)
        _compare_exact(f"mapping_static.{key}", stored, actual[key], problems)
    for key in (
        "python_test_file_count",
        "python_static_test_id_count",
        "typescript_test_file_count",
        "typescript_static_test_id_count",
    ):
        _check_count(mapping, key, actual[key], f"mapping_static.{key}", problems)
    for key in ("fixtures", "markers"):
        stored_rows = _stored_rows(mapping, key, f"mapping_static.{key}", problems)
        _compare_exact(f"mapping_static.{key}", stored_rows, actual[key], problems)
    forbidden = [row for row in actual["markers"] if _forbidden_marker(row)]
    if forbidden:
        problems.append(f"mapping-static xfail/todo/only markers are forbidden: {forbidden}")
    return {
        "python_static_ids": actual["python_static_test_id_count"],
        "typescript_static_ids": actual["typescript_static_test_id_count"],
    }


def _collect_vitest(root: Path) -> tuple[list[str], list[str], str]:
    frontend = locate_root(root, "frontend")
    executable = frontend / "node_modules" / ".bin" / "vitest"
    config = frontend / "vite.config.ts"
    if not executable.is_file() or not config.is_file():
        return [], [], "vitest executable or vite.config.ts missing"
    env = dict(os.environ)
    env["NODE_OPTIONS"] = "--max-old-space-size=1536"
    try:
        result = subprocess.run(
            [
                str(executable),
                "list",
                "--root",
                str(frontend),
                "--config",
                str(config),
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=frontend,
            env=env,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [], f"vitest collection failed: {exc}"
    if result.returncode:
        detail = "; ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
        return [], [], f"vitest collection failed: exit {result.returncode}: {detail}"
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise ValueError("top-level result is not a list")
        ids: list[str] = []
        files: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise ValueError("result row lacks name")
            if not isinstance(row.get("file"), str):
                raise ValueError("result row lacks file")
            source = _frontend_relative(root, row["file"])
            files.add(source)
            ids.append(f"{source}::{row['name']}")
    except (json.JSONDecodeError, ValueError) as exc:
        return [], [], f"vitest collection parse failed: {exc}"
    if not ids or len(ids) != len(set(ids)):
        return [], [], "vitest collection produced empty or duplicate source IDs"
    return sorted(ids), sorted(files), ""


def _collect_playwright(
    root: Path,
    config: str,
) -> tuple[list[str], list[str], str]:
    frontend = locate_root(root, "frontend")
    executable = frontend / "node_modules" / ".bin" / "playwright"
    config_path = frontend / config
    if config not in PLAYWRIGHT_TEST_DIRS:
        return [], [], f"unknown playwright config: {config}"
    if not executable.is_file() or not config_path.is_file():
        return [], [], f"playwright executable or config missing: {config}"
    try:
        result = subprocess.run(
            [str(executable), "test", "--list", f"--config={config}"],
            capture_output=True,
            text=True,
            cwd=frontend,
            env=dict(os.environ),
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [], f"playwright collection failed for {config}: {exc}"
    if result.returncode:
        detail = "; ".join((result.stderr or result.stdout).strip().splitlines()[-3:])
        return (
            [],
            [],
            (f"playwright collection failed for {config}: exit {result.returncode}: {detail}"),
        )
    ids: list[str] = []
    files: set[str] = set()
    reported_total: tuple[int, int] | None = None
    for line in result.stdout.splitlines():
        match = _PLAYWRIGHT_ROW.match(line)
        if match:
            source = (Path("frontend") / PLAYWRIGHT_TEST_DIRS[config] / match.group(1)).as_posix()
            files.add(source)
            ids.append(f"{source}::{match.group(2)}")
        total_match = _PLAYWRIGHT_TOTAL.match(line.strip())
        if total_match:
            reported_total = (int(total_match.group(1)), int(total_match.group(2)))
    if reported_total != (len(ids), len(files)):
        return (
            [],
            [],
            (
                f"playwright collection parse mismatch for {config}: "
                f"reported {reported_total}, parsed {(len(ids), len(files))}"
            ),
        )
    if not ids or len(ids) != len(set(ids)):
        return (
            [],
            [],
            (f"playwright collection for {config} produced empty or duplicate source IDs"),
        )
    return sorted(ids), sorted(files), ""


def _config_identities(root: Path) -> dict[str, str]:
    return {
        config: hashlib.sha256((root / "current" / "frontend" / config).read_bytes()).hexdigest()
        for config in PLAYWRIGHT_CONFIGS
    }


def _collect_frontend(
    root: Path,
) -> tuple[list[str], list[str], dict[str, dict[str, Any]], str]:
    vitest_ids, vitest_files, error = _collect_vitest(root)
    if error:
        return [], [], {}, error
    playwright: dict[str, dict[str, Any]] = {}
    for config in PLAYWRIGHT_CONFIGS:
        ids, files, error = _collect_playwright(root, config)
        if error:
            return [], [], {}, error
        playwright[config] = {
            "id_count": len(ids),
            "file_count": len(files),
            "ids": ids,
            "files": files,
        }
    return vitest_ids, vitest_files, playwright, ""


def _check_frontend_collection(
    baseline: dict[str, Any],
    root: Path,
    problems: list[str],
) -> None:
    frontend = baseline.get("frontend_real_collection")
    if not isinstance(frontend, dict):
        problems.append("frontend_real_collection authority must be an object")
        frontend = {}
    if frontend.get("collector_commands") != _collector_commands():
        problems.append("frontend collector command authority drift")
    vitest_ids, vitest_files, playwright, error = _collect_frontend(root)
    if error:
        problems.append(error)
        return
    for key, actual in (
        ("vitest_ids_list", vitest_ids),
        ("vitest_files_list", vitest_files),
    ):
        stored = _stored_strings(frontend, key, f"frontend.{key}", problems)
        _compare_exact(f"frontend.{key}", stored, actual, problems)
    _check_count(frontend, "vitest_ids", len(vitest_ids), "frontend.vitest_ids", problems)
    _check_count(frontend, "vitest_files", len(vitest_files), "frontend.vitest_files", problems)
    stored_configs = frontend.get("playwright_configs")
    if not isinstance(stored_configs, dict) or set(stored_configs) != set(PLAYWRIGHT_CONFIGS):
        problems.append("frontend.playwright_configs must contain all six exact configs")
        stored_configs = {}
    for config, actual in playwright.items():
        stored = stored_configs.get(config)
        if not isinstance(stored, dict):
            problems.append(f"frontend playwright authority missing for {config}")
            stored = {}
        for key in ("ids", "files"):
            stored_rows = _stored_strings(stored, key, f"frontend.{config}.{key}", problems)
            _compare_exact(f"frontend.{config}.{key}", stored_rows, actual[key], problems)
        _check_count(
            stored, "id_count", actual["id_count"], f"frontend.{config}.id_count", problems
        )
        _check_count(
            stored,
            "file_count",
            actual["file_count"],
            f"frontend.{config}.file_count",
            problems,
        )
    live_a = playwright[LIVE_CONFIGS[0]]
    live_b = playwright[LIVE_CONFIGS[1]]
    same_live = live_a["ids"] == live_b["ids"] and live_a["files"] == live_b["files"]
    live_authority = _retirements.live_authority(root, baseline.get("additive_transitions", []))
    expected_counts = tuple(map(len, live_authority)) if live_authority else (38, 36)
    if not same_live or (live_a["id_count"], live_a["file_count"]) != expected_counts:
        problems.append(
            f"live configs must select identical {expected_counts[0]} IDs "
            f"in {expected_counts[1]} files"
        )
    if live_authority and (live_a["ids"], live_a["files"]) != live_authority:
        problems.append("live configs differ from the exact closeout source authority")
    if (
        frontend.get(
            "live_configs_select_same_source_ids",
            frontend.get("live_configs_select_same_38_source_ids"),
        )
        is not True
    ):
        problems.append("stored live-config equality authority must be true")
    identities = frontend.get("config_identities")
    if identities != _config_identities(root):
        problems.append("playwright config identities drift")
    headline_counts = {
        "playwright_hermetic_ids": playwright["playwright.config.ts"]["id_count"],
        "playwright_hermetic_files": playwright["playwright.config.ts"]["file_count"],
        "playwright_live_ids": live_a["id_count"],
        "playwright_live_files": live_a["file_count"],
        "policy_ids": sum(
            playwright[name]["id_count"]
            for name in (
                "playwright.security-policy.config.ts",
                "playwright.trace-policy.config.ts",
            )
        ),
        "policy_files": sum(
            playwright[name]["file_count"]
            for name in (
                "playwright.security-policy.config.ts",
                "playwright.trace-policy.config.ts",
            )
        ),
        "full_harness_ids": playwright["e2e-full/full.config.ts"]["id_count"],
        "full_harness_files": playwright["e2e-full/full.config.ts"]["file_count"],
    }
    for key, actual in headline_counts.items():
        _check_count(frontend, key, actual, f"frontend.{key}", problems)


def _split_record_problems(baseline: dict[str, Any], root: Path) -> list[str]:
    """Validate stored module-split records against the live file inventory."""
    problems: list[str] = []
    records = _splits.split_authority(baseline, root, problems)
    mapping = baseline.get("mapping_static")
    files = mapping.get("python_test_files") if isinstance(mapping, dict) else None
    if not isinstance(files, list):
        problems.append("module split records need a python_test_files authority")
        return problems
    return problems + _splits.path_disposition_problems(records, files)


def check_test_inventory(root: Path | None = None) -> dict[str, Any]:
    """Execute every collector and compare all exact identities."""
    resolved_root = REPO_ROOT if root is None else root
    baseline = load_test_inventory(resolved_root)
    problems: list[str] = []
    if baseline.get("schema") != "disclaude-architecture-test-inventory-v1":
        problems.append("test inventory schema mismatch")
    problems += inventory_static.candidate_inventory_problems(baseline, resolved_root)
    problems += _split_record_problems(baseline, resolved_root)
    static = _check_mapping_static(baseline, resolved_root, problems)
    _check_frontend_collection(baseline, resolved_root, problems)
    _check_sandbox_deselections(resolved_root, problems)
    collected = _check_collected_ids(baseline, resolved_root, problems)
    return {
        "ok": not problems,
        "problems": problems,
        **static,
        **collected,
    }


def _assert_no_deletions(
    label: str,
    previous: list[Any],
    current: list[Any],
    *,
    authorized: set[str] | None = None,
) -> list[Any]:
    """Return additions, refusing any deletion no authority explains.

    ``authorized`` carries the canonical rows a ``module_split_transitions``
    record has already justified. It defaults to nothing, so a caller that
    supplies no authority keeps the original absolute refusal.
    """
    allowed = set() if authorized is None else authorized
    current_rows = {_canonical_row(item) for item in current}
    deleted = [
        item
        for item in previous
        if _canonical_row(item) not in current_rows and _canonical_row(item) not in allowed
    ]
    if deleted:
        raise RuntimeError(f"unexplained deletion in {label}: {deleted}")
    previous_rows = {_canonical_row(item) for item in previous}
    return [item for item in current if _canonical_row(item) not in previous_rows]


def _collected_row(
    previous: list[str],
    current: list[str],
    added: list[str],
    relocated: int,
    retired: int = 0,
) -> dict[str, Any]:
    """Build one collected_roots transition row.

    ``relocated_count`` is emitted only when a relocation actually happened,
    so a root that merely gained tests keeps the exact shape every accepted
    row already has.
    """
    row: dict[str, Any] = {
        "before_count": len(previous),
        "after_count": len(current),
        "added_ids": added,
    }
    if relocated:
        row["relocated_count"] = relocated
    if retired:
        row["retired_count"] = retired
    return row


def _split_authorizations(
    root: Path,
    split_rows: list[dict[str, Any]],
    previous_roots: dict[str, list[str]],
    current_roots: dict[str, list[str]],
    previous_mapping: dict[str, Any],
    current_mapping: dict[str, Any],
) -> tuple[dict[str, set[str]], list[dict[str, Any]], Any]:
    """Validate the effective module-split records and derive what they allow."""
    problems: list[str] = []
    records = _splits.split_authority({"module_split_transitions": split_rows}, root, problems)
    problems += _splits.path_disposition_problems(records, current_mapping["python_test_files"])
    if problems:
        raise RuntimeError(f"module split transitions are invalid: {problems}")
    authorized, accounted, ledger = _splits.build_authorizations(
        records, previous_mapping, current_mapping, previous_roots, current_roots
    )
    return authorized, accounted, ledger


def _accepted_authorities(
    root: Path,
    accepted_collected_roots: dict[str, list[str]] | None,
    accepted_mapping_static: dict[str, Any] | None,
) -> tuple[dict[str, list[str]], dict[str, Any], list[dict[str, Any]], str]:
    previous = load_test_inventory(root)
    roots = previous.get("collected", {}).get("roots", {})
    mapping = previous.get("mapping_static", {})
    if accepted_collected_roots is not None:
        roots = accepted_collected_roots
    if accepted_mapping_static is not None:
        mapping = accepted_mapping_static
    if not isinstance(roots, dict) or set(roots) != set(PYTHON_ROOTS):
        raise RuntimeError("accepted exact collected roots are required for migration")
    if not all(isinstance(roots[name], list) and roots[name] for name in PYTHON_ROOTS):
        raise RuntimeError("accepted exact collected root IDs must be non-empty")
    required_mapping = {
        "python_test_files",
        "python_static_test_ids",
        "typescript_test_files",
        "typescript_static_test_ids",
        "fixtures",
        "markers",
    }
    if not isinstance(mapping, dict) or not required_mapping <= set(mapping):
        raise RuntimeError("accepted exact mapping-static authority is required")
    identity = accepted_mapping_identity(
        previous,
        root,
        mapping,
        explicit=accepted_mapping_static is not None,
    )
    transitions = previous.get("additive_transitions", [])
    if not isinstance(transitions, list):
        raise RuntimeError("accepted additive transitions must be a list")
    return roots, mapping, transitions, identity


def regenerate_inventory(
    root: Path | None = None,
    source_identity: str | None = None,
    *,
    package: str = "PKG-02-GATE",
    accepted_collected_roots: dict[str, list[str]] | None = None,
    accepted_mapping_static: dict[str, Any] | None = None,
    module_split_transitions: list[dict[str, Any]] | None = None,
    null_advance: bool = False,
) -> dict[str, Any]:
    """Regenerate after proving the transition from accepted exact authority.

    ``null_advance`` authorizes the identity to advance when the epic changed
    no test at all.  It cannot suppress a real delta: a run that did add tests
    takes the ordinary branch regardless, so the flag only ever permits the
    row that pins every collected root as unchanged.
    """
    resolved_root = REPO_ROOT if root is None else root
    if not re.fullmatch(r"PKG-\d{2}-[A-Z0-9-]+", package):
        raise ValueError(f"invalid owning package: {package}")
    source_identity, _ = inventory_static.regeneration_prior(resolved_root, source_identity)
    previous_roots, previous_mapping, transitions, previous_identity = _accepted_authorities(
        resolved_root, accepted_collected_roots, accepted_mapping_static
    )
    current_roots: dict[str, list[str]] = {}
    for root_dir in PYTHON_ROOTS:
        ids, error = _collect_pytest_ids(resolved_root, root_dir)
        if error:
            raise RuntimeError(error)
        current_roots[root_dir] = ids
    sandbox_ids, error = _collect_pytest_ids(resolved_root, "packages", "sandbox_integration")
    if error or sandbox_ids != sorted(SANDBOX_INTEGRATION_DESELECTED_IDS):
        raise RuntimeError(f"sandbox_integration authority failed: {error or sandbox_ids}")
    current_mapping = scan_mapping_static(resolved_root)
    if current_mapping["parse_errors"]:
        raise RuntimeError(f"mapping-static parse errors: {current_mapping['parse_errors']}")
    forbidden = [row for row in current_mapping["markers"] if _forbidden_marker(row)]
    if forbidden:
        raise RuntimeError(f"xfail/todo/only markers are forbidden: {forbidden}")
    vitest_ids, vitest_files, playwright, error = _collect_frontend(resolved_root)
    if error:
        raise RuntimeError(error)
    live = playwright[LIVE_CONFIGS[0]]
    live_authority = _retirements.live_authority(resolved_root, transitions, package)
    expected_counts = tuple(map(len, live_authority)) if live_authority else (38, 36)
    if live_authority and (live["ids"], live["files"]) != live_authority:
        raise RuntimeError("live configs differ from the exact closeout source authority")
    if (
        live["ids"] != playwright[LIVE_CONFIGS[1]]["ids"]
        or live["files"] != playwright[LIVE_CONFIGS[1]]["files"]
        or (live["id_count"], live["file_count"]) != expected_counts
    ):
        raise RuntimeError(
            f"live configs do not select identical {expected_counts} source authority"
        )

    split_rows = (
        load_test_inventory(resolved_root).get("module_split_transitions", [])
        if module_split_transitions is None
        else module_split_transitions
    )
    authorized, accounted_markers, ledger = _split_authorizations(
        resolved_root,
        split_rows,
        previous_roots,
        current_roots,
        previous_mapping,
        current_mapping,
    )
    retired = _retirements.regeneration_authorizations(
        resolved_root, previous_identity, package, current_roots, current_mapping, authorized
    )
    explained = {
        key: authorized.get(key, set()) | retired.get(key, set())
        for key in authorized.keys() | retired.keys()
    }
    added_by_root = {
        name: _assert_no_deletions(
            f"collected.{name}",
            previous_roots[name],
            current_roots[name],
            authorized=explained.get(f"collected.{name}"),
        )
        for name in PYTHON_ROOTS
    }
    static_added = {
        key: _assert_no_deletions(
            f"mapping_static.{key}",
            previous_mapping[key],
            current_mapping[key],
            authorized=explained.get(f"mapping_static.{key}"),
        )
        for key in (
            "python_test_files",
            "python_static_test_ids",
            "typescript_test_files",
            "typescript_static_test_ids",
            "fixtures",
        )
    }
    static_added["fixtures"] = _splits.unaccounted_fixture_line_drift(
        static_added["fixtures"],
        previous_mapping["fixtures"],
        current_mapping["fixtures"],
    )
    marker_added = _assert_no_deletions(
        "mapping_static.markers",
        previous_mapping["markers"],
        current_mapping["markers"],
        authorized=explained.get("mapping_static.markers"),
    )
    marker_growth = _splits.unaccounted_marker_growth(marker_added, accounted_markers, ledger)
    permitted_markers = _retirements.marker_additions(resolved_root, previous_identity, package)
    if sorted(marker_growth, key=_canonical_row) != sorted(permitted_markers, key=_canonical_row):
        raise RuntimeError(f"skip/xfail/todo/only marker growth is forbidden: {marker_growth}")
    count_problems = ledger.count_problems()
    if count_problems:
        raise RuntimeError(f"module split transition counts are not exact: {count_problems}")

    changed_roots = [
        name for name, added in added_by_root.items() if added or explained.get(f"collected.{name}")
    ]
    owns_addition = bool(changed_roots or any(static_added.values()))
    transition = {
        "package": package,
        "root": changed_roots[0] if len(changed_roots) == 1 else "multiple",
        "source_identity_before": previous_identity,
        "source_identity_after": source_identity,
        "collected_roots": {
            name: _collected_row(
                previous_roots[name],
                current_roots[name],
                added_by_root[name],
                len(authorized.get(f"collected.{name}", ())),
                len(retired.get(f"collected.{name}", ())),
            )
            for name in PYTHON_ROOTS
            # A null advance pins EVERY root, so the row asserts the counts it
            # claims rather than asserting nothing; see _transitions.
            if not owns_addition or added_by_root[name] or explained.get(f"collected.{name}")
        },
        "mapping_static_additions": static_added,
    }
    if owns_addition or (null_advance and source_identity != previous_identity):
        transitions = [
            item
            for item in transitions
            if not isinstance(item, dict) or item.get("package") != package
        ]
        transitions.append(transition)
    elif source_identity != previous_identity:
        raise RuntimeError(
            "source identity advance requires an exact additive transition; an "
            "epic that changes no test must pass null_advance=True, which records "
            "a row pinning every collected root as unchanged"
        )

    counts = {name: len(ids) for name, ids in current_roots.items()}
    frontend = {
        "collector_commands": _collector_commands(),
        "vitest_ids": len(vitest_ids),
        "vitest_files": len(vitest_files),
        "vitest_ids_list": vitest_ids,
        "vitest_files_list": vitest_files,
        "playwright_configs": playwright,
        "playwright_hermetic_ids": playwright["playwright.config.ts"]["id_count"],
        "playwright_hermetic_files": playwright["playwright.config.ts"]["file_count"],
        "playwright_live_ids": live["id_count"],
        "playwright_live_files": live["file_count"],
        "policy_ids": sum(
            playwright[name]["id_count"]
            for name in (
                "playwright.security-policy.config.ts",
                "playwright.trace-policy.config.ts",
            )
        ),
        "policy_files": sum(
            playwright[name]["file_count"]
            for name in (
                "playwright.security-policy.config.ts",
                "playwright.trace-policy.config.ts",
            )
        ),
        "full_harness_ids": playwright["e2e-full/full.config.ts"]["id_count"],
        "full_harness_files": playwright["e2e-full/full.config.ts"]["file_count"],
        "live_configs_select_same_source_ids": True,
        "config_identities": _config_identities(resolved_root),
    }
    mapping = {
        "identity": source_identity,
        **{key: value for key, value in current_mapping.items() if key != "parse_errors"},
        "limitations": [
            "mapping-static does not expand runtime parametrization",
            "real collection remains a distinct, mandatory authority",
        ],
    }
    inventory = {
        "schema": "disclaude-architecture-test-inventory-v1",
        "source_identity": source_identity,
        "collected": {
            "counts": counts,
            "total": sum(counts.values()),
            "roots": current_roots,
        },
        "mapping_static": mapping,
        "frontend_real_collection": frontend,
        "inventory_rules": {
            "any_unexplained_deletion_fails": True,
            "deselection_fails": True,
            "skip_xfail_todo_only_growth_fails": True,
            "command_drift_fails": True,
            "additions_allowed_only_when_baseline_updated_in_owning_package": True,
            "real_collection_distinct_from_mapping_static": True,
            "module_splits_require_an_exact_transition_record": True,
        },
        "additive_transitions": sorted(transitions, key=lambda item: _canonical_row(item)),
        "module_split_transitions": sorted(split_rows, key=lambda item: item["old_path"]),
    }
    inventory_static.validate_regenerated_inventory(inventory, resolved_root)
    output = resolved_root / "development" / "architecture" / "test-inventory.json"
    output.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return {
        "written": str(output),
        "collected_total": sum(counts.values()),
        "python_static_ids": current_mapping["python_static_test_id_count"],
        "typescript_static_ids": current_mapping["typescript_static_test_id_count"],
        "transition_added_ids": sum(len(value) for value in added_by_root.values()),
    }
