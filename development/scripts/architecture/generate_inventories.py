#!/usr/bin/env python3
"""Generate the public API and test-inventory authorities from S0 inputs.

Produces:
  - ``development/architecture/public-api.json`` — deterministic baseline surfaces
  - ``development/architecture/test-inventory.json`` — collected test IDs and static baseline

Usage:
    python development/scripts/architecture/generate_inventories.py \
        --public-api-input test-record/campaign-inputs/PKG-02-GATE/s0/public-api-inventory.json \
        --collected-test-ids test-record/campaign-inputs/PKG-02-GATE/s0/collected-test-ids.json \
        --mapping-static \
        test-record/campaign-inputs/PKG-02-GATE/s0/mapping-static-test-inventory.json \
        --output-dir architecture
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SOURCE_IDENTITY = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"


def build_public_api(public_api_input: dict) -> dict:
    """Build the deterministic public API baseline."""
    initializers = []
    for init in public_api_input.get("python_initializers", []):
        initializers.append(
            {
                "path": init["path"],
                "sha256": init["sha256"],
                "bytes": init["bytes"],
                "public_imports": sorted(init.get("public_imports") or []),
                "public_definitions": sorted(init.get("public_definitions") or []),
                "explicit_all": sorted(init.get("explicit_all") or []),
            }
        )
    initializers.sort(key=lambda d: d["path"])

    contract_files = []
    for cf in public_api_input.get("contract_files", []):
        contract_files.append(
            {
                "path": cf["path"],
                "sha256": cf["sha256"],
                "bytes": cf["bytes"],
            }
        )
    contract_files.sort(key=lambda d: d["path"])

    return {
        "schema": "disclaude-architecture-public-api-v1",
        "source_identity": SOURCE_IDENTITY,
        "python_initializers": initializers,
        "contract_files": contract_files,
        "compatibility_rule": (
            "public surface deletion fails unless an additive compatible "
            "bridge is present first and the owning package records the "
            "transition; schema/event/route/settings/tool surface changes "
            "require the same explicit compatibility treatment"
        ),
    }


def build_test_inventory(
    collected: dict, mapping_static: dict
) -> dict:
    """Build the deterministic test inventory authority."""
    # The mapping static inventory stores counts for the static test ID lists.
    py_static_ids = mapping_static.get("python_static_test_ids", [])
    ts_static_ids = mapping_static.get("typescript_static_test_ids", [])
    py_test_files = mapping_static.get("python_test_files", [])
    ts_test_files = mapping_static.get("typescript_test_files", [])

    # Some fields may be counts (int) rather than lists; normalize.
    py_static_count = py_static_ids if isinstance(py_static_ids, int) else len(py_static_ids)
    ts_static_count = ts_static_ids if isinstance(ts_static_ids, int) else len(ts_static_ids)
    py_files_count = py_test_files if isinstance(py_test_files, int) else len(py_test_files)
    ts_files_count = ts_test_files if isinstance(ts_test_files, int) else len(ts_test_files)

    return {
        "schema": "disclaude-architecture-test-inventory-v1",
        "source_identity": SOURCE_IDENTITY,
        "collected": {
            "counts": collected.get("counts", {}),
            "total": collected.get("total", 0),
            "roots": collected.get("roots", {}),
        },
        "mapping_static": {
            "identity": mapping_static.get("identity", SOURCE_IDENTITY),
            "python_static_test_id_count": py_static_count,
            "python_test_file_count": py_files_count,
            "typescript_static_test_id_count": ts_static_count,
            "typescript_test_file_count": ts_files_count,
            "markers": mapping_static.get("markers", []),
            "fixtures": mapping_static.get("fixtures", []),
            "limitations": mapping_static.get("limitations", []),
        },
        "frontend_real_collection": {
            "vitest_ids": 1150,
            "vitest_files": 175,
            "playwright_hermetic_ids": 30,
            "playwright_hermetic_files": 21,
            "playwright_live_ids": 38,
            "playwright_live_files": 36,
            "policy_ids": 2,
            "policy_files": 2,
            "full_harness_ids": 3,
            "full_harness_files": 3,
            "live_configs_select_same_38_source_ids": True,
            "config_identities_both_retained": True,
        },
        "inventory_rules": {
            "any_unexplained_deletion_fails": True,
            "deselection_fails": True,
            "skip_xfail_todo_only_growth_fails": True,
            "command_drift_fails": True,
            "additions_allowed_only_when_baseline_updated_in_owning_package": True,
            "real_collection_distinct_from_mapping_static": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--public-api-input",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--collected-test-ids",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--mapping-static",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
    )
    args = parser.parse_args()

    public_api_input = json.loads(args.public_api_input.read_text(encoding="utf-8"))
    collected = json.loads(args.collected_test_ids.read_text(encoding="utf-8"))
    mapping_static = json.loads(args.mapping_static.read_text(encoding="utf-8"))

    public_api = build_public_api(public_api_input)
    test_inventory = build_test_inventory(collected, mapping_static)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "public-api.json").write_text(
        json.dumps(public_api, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "test-inventory.json").write_text(
        json.dumps(test_inventory, indent=2) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "public_api_initializers": len(public_api["python_initializers"]),
                "public_api_contract_files": len(public_api["contract_files"]),
                "test_inventory_collected_total": test_inventory["collected"]["total"],
                "test_inventory_python_static_ids": (
                    test_inventory["mapping_static"]["python_static_test_id_count"]
                ),
                "test_inventory_ts_static_ids": (
                    test_inventory["mapping_static"]["typescript_static_test_id_count"]
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
