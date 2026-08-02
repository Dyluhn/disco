#!/usr/bin/env python3
"""Generate the immutable architecture data authorities from the frozen disposition rows.

This generator is tracked under ``scripts/architecture/`` and produces the
machine-readable authorities under ``architecture/``. It is deterministic: the
same input disposition rows always produce the same output bytes.

The generator NEVER rescans or renumbers mapping IDs. It reads the frozen 991-ID
disposition universe and projects it into:
  - ``dispositions.json`` — all 991 immutable original IDs with state
  - ``disposition-ids.txt`` — the sorted ID universe
  - ``debt.json`` — the exact active executable rows after accepted packages
  - ``observations.json`` — exact non-budget legacy observations

Debt identity is ``(path, qualified_symbol, rule)``. The qualified symbol is
reconstructed from the Python AST using the frozen baseline line range as an
anchor. Unqualified ``run``/``check`` collisions in one file are invalid; the
qualified name disambiguates ``AppCreateTool.run`` from ``AppAddSectionTool.run``.

Usage:
    python scripts/architecture/generate_debt.py \
        --disposition-rows test-record/campaign-inputs/PKG-02-GATE/authority/disposition-rows.json \
        --output-dir architecture
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

# The frozen PKG-02..PKG-09 resolved-ID sets, Epic 10's aggregate and the AST
# location overrides live in ``generate_debt_parts/`` (also sealed). This file
# sat at 695 logical against the 700-line cap THIS GENERATOR ITSELF ENFORCES,
# so Epic 11's four sub-epic seals could not have been registered here. Only the
# aggregates are imported, so sealing a sub-epic never touches this file.
try:
    from .generate_debt_parts.location_overrides import LOCATION_OVERRIDES
    from .generate_debt_parts.resolved_ids import RESOLVED_IDS
except ImportError:
    from generate_debt_parts.location_overrides import LOCATION_OVERRIDES
    from generate_debt_parts.resolved_ids import RESOLVED_IDS

# Epic 10-A registered 75 rows: 383 -> 308. Epic 10-B registered 37: 308 -> 271.
# Epic 10-C registers 48 — 44 resolved in source (PKG-10-TOOLS 36 + the
# PKG-10-SANDBOX browser cluster 8) plus 4 owner-ADJUDICATED width rows
# (PY-0836/0846/0873/0877): 271 -> 223, the original Epic-10 target (383 - 160).
# Epic 10 closes fully disposed: 156 resolved + 4 adjudicated = 160/160.
# Every id is `PY-`, so EXPECTED_OBSERVATIONS is unchanged.
#
# CORRECTION (root, 2026-08-01): the "standing +2 tree-count offset
# (EventStore, BuildKernel)" this comment previously asserted does not exist.
# `budget.collect_current_violations` FILTERS every registered typed
# classification, so PY-0664 and PY-0188 are absent from the tree count rather
# than carried in it. Measured at the 10-B seal: 209 tree violations == 209
# active python rows, exactly, with zero difference in either direction.
# The 4 rows adjudicated in 10-C are likewise filtered, via the new
# `policy.json` -> typed_classifications.adjudicated_width_non_violations
# registry that `budget.check_adjudicated_width_non_violations` re-proves
# against the live scan on every run. Reconciliation at the 10-C seal:
# 161 python tree violations + 62 TypeScript rows = 223.
#
# Epic 11-A registers 57 rows resolved IN SOURCE (no adjudications): all 55
# PKG-11-BUILD-SPECS rows plus the two PKG-11-AUTH-QUOTA rows PY-0670/0671 that
# the `pkg08-corestripe` cherry-pick cleared in the same commit. Set-equality
# against `owner_package` was proven mechanically before the sets were wired;
# see `debt_resolved_epic11.py`. Reconciliation at the 11-A seal:
# 104 python tree violations + 62 TypeScript rows = 166.
EXPECTED_ACTIVE_DEBT_ROWS = 62
EXPECTED_OBSERVATIONS = 12

# The source identity the disposition rows were sealed against.
SOURCE_IDENTITY = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"

# Rule name mapping from role_category text to the canonical rule key used by
# the scanner. The disposition rows store a human-readable role_category; the
# debt ledger stores the canonical machine rule key.
RULE_MAP = {
    "python callable ast mccabe gt 15": "python_callable_ast_mccabe_gt_15",
    "python callable logical gt 100": "python_callable_logical_gt_100",
    "python or harness module logical gt 700": "python_or_harness_module_logical_gt_700",
    "python class logical gt 350": "python_class_logical_gt_350",
    "typescript callable ast mccabe gt 15": "typescript_callable_ast_mccabe_gt_15",
    "python service public methods gt 12 candidate": (
        "python_service_public_methods_gt_12_candidate"
    ),
    "test module logical gt 1200": "test_module_logical_gt_1200",
    "typescript module logical gt 500": "typescript_module_logical_gt_500",
    "react component logical gt 250": "react_component_logical_gt_250",
    "http ws route handler logical gt 80": "http_ws_route_handler_logical_gt_80",
    "harness oracle module logical gt 400": "harness_oracle_module_logical_gt_400",
    "react hook logical gt 200": "react_hook_logical_gt_200",
}

# Rule to limit mapping (the hard cap each violation must shrink below).
RULE_LIMITS = {
    "python_callable_ast_mccabe_gt_15": 15,
    "python_callable_logical_gt_100": 100,
    "python_or_harness_module_logical_gt_700": 700,
    "python_class_logical_gt_350": 350,
    "typescript_callable_ast_mccabe_gt_15": 15,
    "python_service_public_methods_gt_12_candidate": 12,
    "test_module_logical_gt_1200": 1200,
    "typescript_module_logical_gt_500": 500,
    "react_component_logical_gt_250": 250,
    "http_ws_route_handler_logical_gt_80": 80,
    "harness_oracle_module_logical_gt_400": 400,
    "react_hook_logical_gt_200": 200,
}

# Location pattern: path:symbol:line_start-line_end
_LOCATION_RE = re.compile(r"^(.+):([^:]+):(\d+)-(\d+)$")


def parse_location(location: str) -> dict[str, Any]:
    """Parse a disposition location string into path, symbol, and line range."""
    match = _LOCATION_RE.match(location)
    if match:
        path, symbol, line_start, line_end = match.groups()
        return {
            "path": path,
            "symbol": symbol,
            "line_start": int(line_start),
            "line_end": int(line_end),
        }
    return {"path": location, "symbol": "<module>", "line_start": 0, "line_end": 0}


def parse_metrics(metrics: str) -> dict[str, str]:
    """Parse the metrics string into a dict of key-value pairs."""
    result: dict[str, str] = {}
    for part in metrics.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def extract_observed(role_category: str, metrics: str) -> int:
    """Extract the observed metric value from the role_category and metrics."""
    metric_values = parse_metrics(metrics)
    if "mccabe" in role_category:
        return int(metric_values.get("complexity", 0))
    if "public methods" in role_category:
        return int(metric_values.get("public", 0))
    return int(metric_values.get("logical", 0))


def reconstruct_qualified_symbol_python(
    path: str, symbol: str, line_start: int, line_end: int, repo_root: Path
) -> str:
    """Reconstruct the qualified symbol from a Python AST using line-range anchoring.

    Recursively walks the AST to build (lineno, end_lineno, name, qualified_name)
    tuples for every class/function. Matches the frozen baseline line range to
    find the exact node and returns its qualified name (e.g. ``AppCreateTool.run``).

    Never falls back: absent, unparseable, or ambiguous matches raise ValueError.
    Exactly one qualified symbol must be found via exact line anchor + name.
    """
    if symbol == "<module>":
        return "<module>"
    full = repo_root / path
    if not full.is_file():
        raise ValueError(
            f"cannot reconstruct qualified symbol for {path}:{symbol}: "
            f"file absent from working tree"
        )
    try:
        text = full.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=path)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"cannot reconstruct qualified symbol for {path}:{symbol}: file unparseable: {exc!r}"
        ) from exc

    def walk_all(node: ast.AST, prefix: str = "") -> list[tuple[int, int, str, str]]:
        results: list[tuple[int, int, str, str]] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qname = f"{prefix}.{child.name}" if prefix else child.name
                end = int(child.end_lineno or child.lineno)
                results.append((child.lineno, end, child.name, qname))
                results.extend(walk_all(child, qname))
            else:
                results.extend(walk_all(child, prefix))
        return results

    all_symbols = walk_all(tree)

    # Exact line range + name match — must return exactly one qualified symbol
    exact_matches = [
        qname
        for lineno, end_lineno, name, qname in all_symbols
        if lineno == line_start and end_lineno == line_end and name == symbol
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise ValueError(
            f"ambiguous qualified symbol for {path}:{symbol} "
            f"at lines {line_start}-{line_end}: {exact_matches}"
        )
    # No exact match — this is a defect, not a fallback case
    raise ValueError(
        f"no AST node matches {path}:{symbol} "
        f"at lines {line_start}-{line_end}: "
        f"the frozen baseline line anchor does not correspond to "
        f"any current source symbol"
    )


def reconstruct_qualified_symbol(
    path: str, symbol: str, line_start: int, line_end: int, repo_root: Path
) -> str:
    """Reconstruct the qualified symbol from AST + frozen line range.

    For Python files, uses ``ast`` to walk the tree and find the node matching
    the frozen baseline line range, returning its qualified name.

    For TypeScript files, the disposition scanner already records unique
    top-level symbol names (component/function names are unique per file in the
    baseline), so the unqualified symbol is the qualified symbol.

    For ``<module>`` symbols, returns ``<module>``.
    """
    if symbol == "<module>":
        return "<module>"
    if path.endswith(".py"):
        return reconstruct_qualified_symbol_python(path, symbol, line_start, line_end, repo_root)
    # TypeScript: top-level symbols are unique per file in the baseline
    return symbol


def build_dispositions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the immutable 991-ID disposition universe."""
    dispositions = []
    for row in rows:
        state = "resolved" if row["id"] in RESOLVED_IDS else "active"
        dispositions.append(
            {
                "id": row["id"],
                "disposition": row["disposition"],
                "state": state,
                "original_type": row["disposition"],
                "owner_package": row["owning_package"],
                "location": LOCATION_OVERRIDES.get(row["id"], row["location"]),
                "role_category": row["role_category"],
            }
        )
    dispositions.sort(key=lambda d: d["id"])
    return dispositions


def build_debt_rows(rows: list[dict[str, Any]], repo_root: Path) -> list[dict[str, Any]]:
    """Build the active executable debt rows.

    Active executable debt = VIOLATION dispositions not resolved by an accepted
    package.

    Each row is keyed by ``(path, qualified_symbol, rule)``. The qualified
    symbol is reconstructed from the AST using the frozen baseline line range.
    All active keys must be unique.
    """
    debt = []
    for row in rows:
        if row["disposition"] != "VIOLATION":
            continue
        if row["id"] in RESOLVED_IDS:
            continue
        location = LOCATION_OVERRIDES.get(row["id"]) or row["location"]
        loc = parse_location(location)
        role_category = str(row["role_category"])
        rule = RULE_MAP.get(role_category, role_category)
        observed = extract_observed(role_category, row["metrics"])
        limit = RULE_LIMITS.get(rule, 0)
        qualified_symbol = reconstruct_qualified_symbol(
            loc["path"], loc["symbol"], loc["line_start"], loc["line_end"], repo_root
        )
        debt.append(
            {
                "id": row["id"],
                "path": loc["path"],
                "symbol": loc["symbol"],
                "qualified_symbol": qualified_symbol,
                "line_start": loc["line_start"],
                "line_end": loc["line_end"],
                "rule": rule,
                "observed": observed,
                "limit": limit,
                "owner_package": row["owning_package"],
                "delete_by": row["owning_package"],
                "source_identity": SOURCE_IDENTITY,
            }
        )
    debt.sort(key=lambda d: d["id"])

    # Verify all active (path, qualified_symbol, rule) keys are unique.
    keys = [(d["path"], d["qualified_symbol"], d["rule"]) for d in debt]
    from collections import Counter

    dupes = {k: v for k, v in Counter(keys).items() if v > 1}
    if dupes:
        raise ValueError(
            f"debt identity collision: {len(dupes)} duplicate "
            f"(path, qualified_symbol, rule) keys: {list(dupes.keys())[:5]}"
        )
    return debt


def build_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the exact non-budget legacy observations.

    Each observation cites an existing disposition ID. Distributed concerns
    that are not resolved by an accepted package remain as observations. The
    typed non-violations are also recorded as classification observations.
    """
    observations = []
    for row in rows:
        if row["id"] in RESOLVED_IDS:
            continue
        if row["disposition"] == "DISTRIBUTED_CONCERN":
            observations.append(
                {
                    "id": row["id"],
                    "kind": "distributed_concern",
                    "location": LOCATION_OVERRIDES.get(row["id"], row["location"]),
                    "concern": row["role_category"],
                    "owner_package": row["owning_package"],
                    "source_disposition_id": row["id"],
                }
            )
        elif row["disposition"] == "TYPED_NON_VIOLATION":
            observations.append(
                {
                    "id": row["id"],
                    "kind": "typed_non_violation",
                    "location": LOCATION_OVERRIDES.get(row["id"], row["location"]),
                    "classification": row["role_category"],
                    "owner_package": row["owning_package"],
                    "source_disposition_id": row["id"],
                }
            )
    observations.sort(key=lambda o: o["id"])
    return observations


REPO_ROOT = Path(__file__).resolve().parents[2]
ARCH_DIR = REPO_ROOT / "architecture"
TRACKED_DISPOSITION_ROWS = ARCH_DIR / "disposition-rows.json"

# The four authority files regenerated from the frozen disposition rows.
AUTHORITY_FILES = (
    "dispositions.json",
    "disposition-ids.txt",
    "debt.json",
    "observations.json",
)


def _generate_all_authorities(
    disposition_rows_path: Path, output_dir: Path, repo_root: Path
) -> dict[str, int]:
    """Generate all four authority files into output_dir.

    Returns a dict with the counts.
    """
    rows = json.loads(disposition_rows_path.read_text(encoding="utf-8"))
    if len(rows) != 991:
        raise ValueError(f"expected 991 disposition rows, got {len(rows)}")

    ids = sorted(row["id"] for row in rows)
    if len(ids) != 991 or len(set(ids)) != 991:
        raise ValueError("duplicate or incomplete IDs in disposition rows")

    dispositions = build_dispositions(rows)
    if len(dispositions) != 991:
        raise ValueError(f"expected 991 dispositions, got {len(dispositions)}")

    debt = build_debt_rows(rows, repo_root)
    if len(debt) != EXPECTED_ACTIVE_DEBT_ROWS:
        raise ValueError(f"expected {EXPECTED_ACTIVE_DEBT_ROWS} active debt rows, got {len(debt)}")

    observations = build_observations(rows)
    if len(observations) != EXPECTED_OBSERVATIONS:
        raise ValueError(f"expected {EXPECTED_OBSERVATIONS} observations, got {len(observations)}")

    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "dispositions.json").write_text(
        json.dumps(dispositions, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "disposition-ids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")
    (output_dir / "debt.json").write_text(json.dumps(debt, indent=2) + "\n", encoding="utf-8")
    (output_dir / "observations.json").write_text(
        json.dumps(observations, indent=2) + "\n", encoding="utf-8"
    )

    return {
        "dispositions": len(dispositions),
        "debt_rows": len(debt),
        "observations": len(observations),
    }


def _check_authorities(repo_root: Path) -> dict[str, Any]:
    """Regenerate all four authorities into a temp dir and byte-compare.

    Stable code must never read ``test-record``. The tracked
    ``architecture/disposition-rows.json`` is the frozen input authority.
    """
    import tempfile

    arch_dir = repo_root / "architecture"
    tracked_disposition_rows = arch_dir / "disposition-rows.json"

    if not tracked_disposition_rows.is_file():
        return {
            "ok": False,
            "problems": [f"tracked frozen input authority missing: {tracked_disposition_rows}"],
        }

    with tempfile.TemporaryDirectory(prefix="arch-debt-check-") as tmp:
        tmp_dir = Path(tmp)
        try:
            _generate_all_authorities(tracked_disposition_rows, tmp_dir, repo_root)
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            return {
                "ok": False,
                "problems": [f"authority regeneration failed: {exc}"],
            }

        problems: list[str] = []
        for fname in AUTHORITY_FILES:
            tracked = arch_dir / fname
            regenerated = tmp_dir / fname
            if not tracked.is_file():
                problems.append(f"tracked authority missing: architecture/{fname}")
                continue
            tracked_bytes = tracked.read_bytes()
            regenerated_bytes = regenerated.read_bytes()
            if tracked_bytes != regenerated_bytes:
                problems.append(
                    f"authority drift: architecture/{fname} does not match regenerated bytes"
                )
        return {"ok": len(problems) == 0, "problems": problems}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--disposition-rows",
        type=Path,
        default=TRACKED_DISPOSITION_ROWS,
        help="Path to the frozen disposition-rows.json "
        "(defaults to architecture/disposition-rows.json)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory for architecture data files",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root for AST-based qualified symbol reconstruction",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Regenerate all four authorities into a temp directory and "
        "byte-compare them against the tracked architecture/ files",
    )
    args = parser.parse_args()

    if args.check:
        result = _check_authorities(args.repo_root)
        if not result["ok"]:
            print("DEBT AUTHORITY CHECK FAIL:\n")
            for problem in result["problems"]:
                print(f"  - {problem}")
            return 1
        print("DEBT AUTHORITY CHECK OK — all four authorities match regenerated bytes.")
        return 0

    if args.output_dir is None:
        parser.error("--output-dir is required unless --check is used")

    counts = _generate_all_authorities(args.disposition_rows, args.output_dir, args.repo_root)
    payload: dict[str, Any] = {
        **counts,
        "resolved_ids": sorted(RESOLVED_IDS),
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
