"""Governance seal helpers — kept below McCabe 15.

The seal verifies that change-controlled governance files match their sealed
digests, and that the invocation contract (SEAL-INVOCATION.json) structurally
requires the ordered commands in CI, release, and pre-commit.

The protected set includes all executable architecture policy implementation
bytes, not only a thin wrapper whose unsealed helpers could be weakened.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .policy import REPO_ROOT

# The protected set: these files must not drift.
# The digest manifest (PROTECTED.sha256) does NOT protect itself.
# This includes all executable architecture policy implementation bytes.
PROTECTED: tuple[str, ...] = (
    "current/docs/governance/ENGINEERING-STANDARDS.md",
    "current/docs/governance/ARCHITECTURE-BOUNDARIES.md",
    "development/architecture/contexts.json",
    "development/architecture/debt.json",
    "development/architecture/policy.json",
    "development/architecture/disposition-ids.txt",
    "development/architecture/disposition-rows.json",
    "development/architecture/dispositions.json",
    "development/architecture/generated.json",
    "development/architecture/observations.json",
    "development/architecture/ownership.json",
    "development/architecture/public-api.json",
    "development/architecture/test-inventory.json",
    "development/architecture/research-test-retirements.json",
    "development/architecture/research-api-closeout.json",
    "development/architecture/research-test-retirement-dispositions.json",
    "development/scripts/check_soak_freeze.py",
    "development/scripts/check_arch_budget.py",
    "development/scripts/check_governance_seal.py",
    "development/scripts/check_arch_debt.py",
    "development/scripts/check_arch_imports.py",
    "development/scripts/check_public_api.py",
    "development/scripts/check_test_inventory.py",
    "development/scripts/check_ci_contract.py",
    "development/scripts/check_inventory_execution.py",
    "development/scripts/check_tool_schemas.py",
    "development/scripts/gen_arch_diagram.py",
    "current/packages/core/tests/test_build_platform_nonweb_conformance.py",
    "development/scripts/architecture/__init__.py",
    "development/scripts/architecture/policy.py",
    "development/scripts/architecture/budget.py",
    "development/scripts/architecture/debt.py",
    "development/scripts/architecture/imports.py",
    "development/scripts/architecture/observation_governance.py",
    "development/scripts/architecture/python_scan.py",
    "development/scripts/architecture/source_governance.py",
    "development/scripts/architecture/typescript_scan.py",
    "development/scripts/architecture/ts_scan.mjs",
    "development/scripts/architecture/seal.py",
    "development/scripts/architecture/ci_contract.py",
    # PKG-19-CERT-STRUCTURAL (F7): the ordered-gate comparator and the
    # ordered-list position helpers moved here when ci_contract.py hit its
    # 700-line budget. Every _parts module of a protected gate is protected
    # (public_api_parts, test_inventory_parts, generate_debt_parts); leaving
    # this one out would move load-bearing gate logic out from under the seal.
    "development/scripts/architecture/ci_contract_sealed.py",
    # PKG-19-CERT-STRUCTURAL (F1/F2/F6): the inventory execution-coverage
    # comparator — the gate that fails when the inventory certifies an id no
    # sanctioned command can collect.
    "development/scripts/architecture/inventory_execution.py",
    "development/scripts/architecture/diagram.py",
    "development/scripts/architecture/public_api.py",
    "development/scripts/architecture/public_surface.py",
    "development/scripts/architecture/test_inventory.py",
    "development/scripts/architecture/inventory_static.py",
    "development/scripts/architecture/generated.py",
    "development/scripts/architecture/generate_debt.py",
    # Epic 10's resolved-ID sets were split out of generate_debt.py (which was
    # at 690 of its own 700-line budget). They decide which rows leave the
    # active ledger, so they are hash-gated exactly like the generator.
    "development/scripts/architecture/debt_resolved_epic10.py",
    # Epic 11's sub-epic sets follow the same precedent: they decide which rows
    # leave the active ledger, so they are hash-gated exactly like the generator.
    "development/scripts/architecture/debt_resolved_epic11.py",
    "development/scripts/architecture/debt_resolved_epic12.py",
    "development/scripts/architecture/debt_resolved_pkg19.py",
    "development/scripts/architecture/generate_inventories.py",
    "development/scripts/architecture/yaml_parser.py",
    # Epic 10-D decomposed the two authorities that had run out of budget.
    # Their interiors carry the same change-controlled bytes the parents did,
    # so they are hash-gated individually: a sealed wrapper over unsealed
    # helpers is precisely the weakening this module's docstring warns about.
    "development/scripts/architecture/public_api_parts/__init__.py",
    "development/scripts/architecture/public_api_parts/_authority.py",
    "development/scripts/architecture/public_api_parts/_closeout.py",
    "development/scripts/architecture/public_api_parts/_constants.py",
    "development/scripts/architecture/public_api_parts/_contracts.py",
    # Epic 12-A added the fourth authority: no record type could express a
    # signature change to an already-public FRONTEND declaration, which binding
    # Amendment A3 requires. It decides which frontend changes may regenerate
    # the public-API authority, so it is hash-gated exactly like the other three.
    "development/scripts/architecture/public_api_parts/_frontend.py",
    "development/scripts/architecture/public_api_parts/_members.py",
    # PKG-38-UI-FIXES-V51 added the fifth authority: a frontend public target
    # that MOVES path or name read as a deletion, because a TypeScript
    # re-export cannot keep the old name alive the way a Python __init__.py
    # can. It decides which frontend deletions may regenerate the authority, so
    # it is hash-gated exactly like the other four.
    "development/scripts/architecture/public_api_parts/_relocations.py",
    "development/scripts/architecture/public_api_parts/_surface.py",
    "development/scripts/architecture/test_inventory_parts/__init__.py",
    "development/scripts/architecture/test_inventory_parts/_rows.py",
    "development/scripts/architecture/test_inventory_parts/_splits.py",
    "development/scripts/architecture/test_inventory_parts/_transitions.py",
    "development/scripts/architecture/test_inventory_parts/_retirements.py",
    # PKG-38-UI-FIXES-V51: the rename authority. A renamed test identity read
    # as an unexplained deletion, and the only records that could sanction one
    # were a module-split relocation (which rules renames out by contract) and
    # the single-use PKG-35 retirement closeout. It decides which deletions may
    # regenerate the inventory, so it is hash-gated exactly like the others.
    "development/scripts/architecture/test_inventory_parts/_renames.py",
    # Epic 11-A decomposed generate_debt.py for the same reason (695 of its own
    # 700-line budget, with four sub-epic seals to register). Only frozen DATA
    # moved, but that data decides which rows leave the active ledger, so it is
    # hash-gated exactly like the generator that reads it.
    "development/scripts/architecture/generate_debt_parts/__init__.py",
    "development/scripts/architecture/generate_debt_parts/location_overrides.py",
    "development/scripts/architecture/generate_debt_parts/resolved_ids.py",
    "current/docs/governance/SEAL-INVOCATION.json",
)

MANIFEST = "current/docs/governance/PROTECTED.sha256"
SEAL_INVOCATION = "current/docs/governance/SEAL-INVOCATION.json"

# The ordered commands that SEAL-INVOCATION.json must require.
ORDERED_COMMANDS = [
    "uv run python development/scripts/check_soak_freeze.py",
    "uv run python development/scripts/check_governance_seal.py",
    "uv run python development/scripts/check_arch_budget.py",
    "uv run python development/scripts/check_arch_debt.py",
    "uv run python development/scripts/check_arch_imports.py",
    "uv run python development/scripts/gen_arch_diagram.py --check",
    "uv run python development/scripts/check_public_api.py",
    "uv run python development/scripts/check_test_inventory.py",
    "uv run python development/scripts/check_ci_contract.py",
    "uv run python development/scripts/check_tool_schemas.py",
    "uv run pytest -q current/packages/core/tests/test_build_platform_nonweb_conformance.py",
]

PROVISIONING_COMMANDS = [
    "uv sync --all-packages --frozen",
    "npm ci",
]


def digest(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(root: Path | None = None) -> dict[str, str] | None:
    """Parse the seal manifest. Returns None if absent or malformed."""
    if root is None:
        root = REPO_ROOT
    path = root / MANIFEST
    if not path.is_file() or path.is_symlink():
        return None
    entries: dict[str, str] = {}
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            return None
        value, rel = parts[0], parts[1].strip()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            return None
        if not rel or rel in entries:
            return None
        entries[rel] = value
    return entries


def check_seal_bytes(root: Path | None = None) -> dict[str, Any]:
    """Check that every protected file matches the manifest digest."""
    if root is None:
        root = REPO_ROOT
    recorded = read_manifest(root)
    if recorded is None:
        return {
            "ok": False,
            "problems": [f"SEAL NOT ESTABLISHED: {MANIFEST} is absent or malformed"],
            "code": 3,
        }

    problems: list[str] = []

    for rel in PROTECTED:
        full = root / rel
        if not full.is_file():
            problems.append(f"MISSING FILE: {rel} is protected but does not exist")
            continue
        if full.is_symlink():
            problems.append(f"SYMLINK: {rel} is protected but is not a regular file")
            continue
        if rel not in recorded:
            problems.append(f"UNSEALED: {rel} is protected but absent from {MANIFEST}")
            continue
        actual = digest(full)
        if actual != recorded[rel]:
            problems.append(
                f"DRIFT: {rel}\n"
                f"         recorded sha256:{recorded[rel]}\n"
                f"         actual   sha256:{actual}"
            )

    for rel in recorded:
        if rel not in PROTECTED:
            problems.append(
                f"STALE ENTRY: {MANIFEST} seals {rel}, which is not in the protected set"
            )

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "code": 1 if problems else 0,
    }


def _exact_string_list(
    value: Any,
    expected: list[str],
    *,
    field: str,
    problems: list[str],
) -> None:
    """Require one exact ordered list of non-empty strings."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        problems.append(f"{SEAL_INVOCATION} field '{field}' must be a string list")
        return
    if value == expected:
        return
    before = len(problems)
    _report_list_membership(value, expected, field=field, problems=problems)
    if len(problems) == before:
        problems.append(f"{SEAL_INVOCATION} field '{field}' has invalid command order")


def _report_list_membership(
    value: list[str],
    expected: list[str],
    *,
    field: str,
    problems: list[str],
) -> None:
    """Report missing, unexpected and duplicate exact-list members."""
    for item in expected:
        if item not in value:
            problems.append(f"{SEAL_INVOCATION} field '{field}' missing command: {item}")
    for item in value:
        if item not in expected:
            problems.append(f"{SEAL_INVOCATION} field '{field}' has unexpected command: {item}")
    duplicates = sorted({item for item in value if value.count(item) > 1})
    for item in duplicates:
        problems.append(f"{SEAL_INVOCATION} field '{field}' has duplicate command: {item}")


def _check_section_provisioning(
    section: str, section_data: dict[str, Any], problems: list[str]
) -> None:
    """Check that a CI/release section requires the exact provisioning commands."""
    _exact_string_list(
        section_data.get("provisioning_commands"),
        PROVISIONING_COMMANDS,
        field=f"{section}.provisioning_commands",
        problems=problems,
    )


def _check_section(
    invocation: dict[str, Any],
    section: str,
    expected_commands: list[str],
    problems: list[str],
) -> None:
    """Validate one invocation section without accepting type confusion."""
    section_data = invocation.get(section)
    if not isinstance(section_data, dict):
        problems.append(f"{SEAL_INVOCATION} section '{section}' must be an object")
        return
    _exact_string_list(
        section_data.get("ordered_commands"),
        expected_commands,
        field=f"{section}.ordered_commands",
        problems=problems,
    )
    if section in ("ci", "release"):
        _check_section_provisioning(section, section_data, problems)


def check_seal_invocation(root: Path | None = None) -> dict[str, Any]:
    """Check that SEAL-INVOCATION.json structurally requires the ordered commands
    and provisioning.

    Verifies the invocation structure, ordered commands, provisioning commands,
    and drift/missing/stale entries. Excludes its own manifest from the
    protected set.
    """
    if root is None:
        root = REPO_ROOT
    path = root / SEAL_INVOCATION
    if not path.is_file() or path.is_symlink():
        return {
            "ok": False,
            "problems": [f"{SEAL_INVOCATION} is absent or is a symlink"],
        }

    try:
        invocation = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "problems": [f"{SEAL_INVOCATION} is malformed: {exc}"],
        }

    if not isinstance(invocation, dict):
        return {
            "ok": False,
            "problems": [f"{SEAL_INVOCATION} root must be an object"],
        }

    problems: list[str] = []
    if invocation.get("schema") != "disclaude-governance-seal-invocation-v1":
        problems.append(f"{SEAL_INVOCATION} has invalid schema")
    _exact_string_list(
        invocation.get("ordered_commands_full"),
        ORDERED_COMMANDS,
        field="ordered_commands_full",
        problems=problems,
    )
    if invocation.get("provisioning_command") != PROVISIONING_COMMANDS[0]:
        problems.append(f"{SEAL_INVOCATION} has invalid provisioning_command")
    if invocation.get("frontend_provisioning_command") != PROVISIONING_COMMANDS[1]:
        problems.append(f"{SEAL_INVOCATION} has invalid frontend_provisioning_command")

    _check_section(invocation, "ci", ORDERED_COMMANDS, problems)
    _check_section(invocation, "release", ORDERED_COMMANDS, problems)
    _check_section(invocation, "precommit", ORDERED_COMMANDS[:6], problems)

    return {
        "ok": len(problems) == 0,
        "problems": problems,
    }


def check_seal(root: Path | None = None) -> dict[str, Any]:
    """Run the full governance seal check (bytes + invocation)."""
    bytes_result = check_seal_bytes(root)
    invocation_result = check_seal_invocation(root)

    problems = bytes_result["problems"] + invocation_result["problems"]
    code = 3 if bytes_result.get("code") == 3 else (1 if problems else 0)

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "code": code,
        "protected_count": len(PROTECTED),
    }


def write_manifest(root: Path | None = None) -> list[str]:
    """Write the seal manifest for all protected files (rebaseline helper)."""
    if root is None:
        root = REPO_ROOT
    path = root / MANIFEST
    header = (
        "# Seal manifest for the change-controlled governance and gate files.\n"
        "# Verified by development/scripts/check_governance_seal.py.\n"
        "# Do not hand-edit. Rebaselining requires an explicit owner instruction.\n"
    )
    lines = [header]
    for rel in PROTECTED:
        full = root / rel
        if not full.is_file() or full.is_symlink():
            raise ValueError(f"protected path is not a regular file: {rel}")
        lines.append(f"{digest(full)}  {rel}\n")
    path.write_text("".join(lines), encoding="utf-8")
    return list(PROTECTED)
