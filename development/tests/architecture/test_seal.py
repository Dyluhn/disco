"""Governance seal mutation tests for the architecture gate.

Tests seal byte drift, stale manifest entries, missing commands in
SEAL-INVOCATION.json, and seal/checker tampering. Uses the
``check_seal_bytes(root=)`` and ``check_seal_invocation(root=)`` boundaries
with temp repos.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import write

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import seal  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def valid_invocation() -> dict[str, Any]:
    return {
        "schema": "disclaude-governance-seal-invocation-v1",
        "provisioning_command": seal.PROVISIONING_COMMANDS[0],
        "frontend_provisioning_command": seal.PROVISIONING_COMMANDS[1],
        "ordered_commands_full": list(seal.ORDERED_COMMANDS),
        "ci": {
            "provisioning_commands": list(seal.PROVISIONING_COMMANDS),
            "ordered_commands": list(seal.ORDERED_COMMANDS),
        },
        "release": {
            "provisioning_commands": list(seal.PROVISIONING_COMMANDS),
            "ordered_commands": list(seal.ORDERED_COMMANDS),
        },
        "precommit": {"ordered_commands": list(seal.ORDERED_COMMANDS[:6])},
    }


def write_invocation(root: Path, invocation: object) -> None:
    write(
        root / seal.SEAL_INVOCATION,
        json.dumps(invocation, indent=2, sort_keys=True) + "\n",
    )


# ---------------------------------------------------------------------------
# Protected set — constant check with companion mutation
# ---------------------------------------------------------------------------


class TestProtectedSet:
    def test_protected_includes_all_gate_files(self):
        """All executable architecture gate files must be protected."""
        required = [
            "development/governance/ENGINEERING-STANDARDS.md",
            "development/governance/ARCHITECTURE-BOUNDARIES.md",
            "development/architecture/policy.json",
            "development/architecture/contexts.json",
            "development/architecture/debt.json",
            "development/architecture/disposition-ids.txt",
            "development/architecture/generated.json",
            "development/architecture/ownership.json",
            "development/architecture/public-api.json",
            "development/architecture/test-inventory.json",
            "development/scripts/check_soak_freeze.py",
            "development/scripts/check_arch_budget.py",
            "development/scripts/check_governance_seal.py",
            "development/scripts/check_tool_schemas.py",
            "packages/core/tests/test_build_platform_nonweb_conformance.py",
            "development/governance/SEAL-INVOCATION.json",
        ]
        for path in required:
            assert path in seal.PROTECTED, f"missing protected file: {path}"

    def test_manifest_does_not_protect_itself(self):
        """The manifest file must not protect itself."""
        assert "development/governance/PROTECTED.sha256" not in seal.PROTECTED

    def test_protected_includes_implementation_bytes(self):
        """The protected set must include implementation bytes, not just wrappers."""
        impl_files = [
            "development/scripts/architecture/policy.py",
            "development/scripts/architecture/budget.py",
            "development/scripts/architecture/debt.py",
            "development/scripts/architecture/imports.py",
            "development/scripts/architecture/observation_governance.py",
            "development/scripts/architecture/python_scan.py",
            "development/scripts/architecture/source_governance.py",
            "development/scripts/architecture/seal.py",
            "development/scripts/architecture/ci_contract.py",
            "development/scripts/architecture/generated.py",
            "development/scripts/architecture/public_surface.py",
            "development/scripts/architecture/inventory_static.py",
        ]
        for path in impl_files:
            assert path in seal.PROTECTED, f"missing implementation file: {path}"

    def test_drifted_implementation_file_detected(self, tmp_path: Path) -> None:
        """A drifted protected implementation file must be detected by check_seal_bytes."""
        manifest_dir = tmp_path / "development" / "governance"
        manifest_dir.mkdir(parents=True)
        arch_dir = tmp_path / "development" / "architecture"
        arch_dir.mkdir(parents=True)
        # Write a protected file with known content
        write(arch_dir / "policy.json", '{"test": true}')
        # Write a manifest with a WRONG hash for policy.json
        manifest = f"{'0' * 64}  development/architecture/policy.json\n"
        write(manifest_dir / "PROTECTED.sha256", manifest)
        result = seal.check_seal_bytes(tmp_path)
        assert not result["ok"]
        assert any("DRIFT" in p for p in result["problems"])


# ---------------------------------------------------------------------------
# Ordered commands — constant check with companion mutation
# ---------------------------------------------------------------------------


class TestOrderedCommands:
    def test_ordered_commands_correct(self):
        """The ordered commands must start with soak_freeze and end with non-web."""
        assert seal.ORDERED_COMMANDS[0] == "uv run python development/scripts/check_soak_freeze.py"
        assert seal.ORDERED_COMMANDS[1] == "uv run python development/scripts/check_governance_seal.py"
        assert seal.ORDERED_COMMANDS[-1] == (
            "uv run pytest -q packages/core/tests/test_build_platform_nonweb_conformance.py"
        )
        assert "uv run python development/scripts/check_tool_schemas.py" in seal.ORDERED_COMMANDS

    def test_ordered_commands_include_all_gates(self):
        """All gate commands must be in the ordered list."""
        assert len(seal.ORDERED_COMMANDS) >= 11

    def test_missing_command_in_invocation_fails(self, tmp_path: Path) -> None:
        """A SEAL-INVOCATION.json missing one command must fail."""
        invocation = valid_invocation()
        invocation["ci"]["ordered_commands"].pop()
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("missing command" in p for p in result["problems"])


# ---------------------------------------------------------------------------
# Seal byte drift — mutations through check_seal_bytes
# ---------------------------------------------------------------------------


class TestSealByteDrift:
    def test_seal_rejects_drift(self, tmp_path: Path) -> None:
        """A digest mismatch must fail."""
        manifest_dir = tmp_path / "development" / "governance"
        manifest_dir.mkdir(parents=True)
        write(tmp_path / "development" / "architecture" / "policy.json", "{}")
        manifest = f"{'0' * 64}  development/architecture/policy.json\n"
        write(manifest_dir / "PROTECTED.sha256", manifest)
        result = seal.check_seal_bytes(tmp_path)
        assert not result["ok"]
        assert any("DRIFT" in p for p in result["problems"])

        symlink_root = tmp_path / "symlink-case"
        target = symlink_root / "target.json"
        write(target, "{}")
        protected = symlink_root / "development" / "architecture" / "policy.json"
        protected.parent.mkdir(parents=True)
        protected.symlink_to(target)
        write(
            symlink_root / seal.MANIFEST,
            f"{hashlib.sha256(b'{}').hexdigest()}  development/architecture/policy.json\n",
        )
        result = seal.check_seal_bytes(symlink_root)
        assert not result["ok"]
        assert any("SYMLINK" in problem for problem in result["problems"])

    def test_seal_rejects_stale_entry(self, tmp_path: Path) -> None:
        """A manifest entry for a non-protected file must fail."""
        manifest_dir = tmp_path / "development" / "governance"
        manifest_dir.mkdir(parents=True)
        manifest = "abc123  extra/file.py\n"
        write(manifest_dir / "PROTECTED.sha256", manifest)
        result = seal.check_seal_bytes(tmp_path)
        assert not result["ok"]

    def test_seal_rejects_missing_manifest(self, tmp_path: Path) -> None:
        """An absent manifest must fail with code 3."""
        result = seal.check_seal_bytes(tmp_path)
        assert not result["ok"]
        assert result.get("code") == 3

    def test_seal_rejects_malformed_manifest(self, tmp_path: Path) -> None:
        """A malformed manifest must fail with code 3."""
        manifest_dir = tmp_path / "development" / "governance"
        manifest_dir.mkdir(parents=True)
        write(manifest_dir / "PROTECTED.sha256", "not a valid manifest\n")
        result = seal.check_seal_bytes(tmp_path)
        assert not result["ok"]
        assert result.get("code") == 3

        # Duplicate paths must not silently overwrite one another.
        write(
            tmp_path / seal.MANIFEST,
            f"{'0' * 64}  development/architecture/policy.json\n"
            f"{'1' * 64}  development/architecture/policy.json\n",
        )
        result = seal.check_seal_bytes(tmp_path)
        assert not result["ok"]
        assert result["code"] == 3

    def test_seal_rejects_unsealed_protected_file(self, tmp_path: Path) -> None:
        """A protected file absent from the manifest must fail as unsealed."""
        manifest_dir = tmp_path / "development" / "governance"
        manifest_dir.mkdir(parents=True)
        arch_dir = tmp_path / "development" / "architecture"
        arch_dir.mkdir(parents=True)
        write(arch_dir / "policy.json", "{}")
        # Manifest exists but doesn't include policy.json
        write(manifest_dir / "PROTECTED.sha256", "")
        result = seal.check_seal_bytes(tmp_path)
        assert not result["ok"]
        assert any("UNSEALED" in p or "MISSING" in p for p in result["problems"])


# ---------------------------------------------------------------------------
# Seal invocation — mutations through check_seal_invocation
# ---------------------------------------------------------------------------


class TestSealInvocation:
    def test_seal_invocation_valid(self):
        result = seal.check_seal_invocation()
        assert result["ok"], result["problems"]

    def test_seal_invocation_rejects_missing_command(self, tmp_path: Path) -> None:
        """A missing command in a section must fail."""
        invocation = valid_invocation()
        invocation["ci"]["ordered_commands"].pop()
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("missing command" in p for p in result["problems"])

        invocation = valid_invocation()
        invocation["ci"]["provisioning_commands"].reverse()
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any(
            "ci.provisioning_commands" in problem for problem in result["problems"]
        )

        invocation = valid_invocation()
        invocation["ordered_commands_full"].pop()
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("ordered_commands_full" in problem for problem in result["problems"])

    def test_seal_invocation_rejects_wrong_schema(self, tmp_path: Path) -> None:
        """An invalid schema must fail."""
        invocation = valid_invocation()
        invocation["schema"] = "wrong-schema"
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("invalid schema" in p for p in result["problems"])

        invocation = valid_invocation()
        invocation["release"] = []
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("must be an object" in problem for problem in result["problems"])

    def test_seal_invocation_rejects_missing_file(self, tmp_path: Path) -> None:
        """An absent SEAL-INVOCATION.json must fail."""
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("absent" in p for p in result["problems"])

        for rel in seal.PROTECTED:
            write(tmp_path / rel, "{}")
        write_invocation(tmp_path, [])
        seal.write_manifest(tmp_path)
        result = seal.check_seal(tmp_path)
        assert not result["ok"]
        assert result["code"] == 1

    def test_seal_invocation_rejects_out_of_order(self, tmp_path: Path) -> None:
        """Out-of-order commands in a section must fail."""
        invocation = valid_invocation()
        commands = invocation["ci"]["ordered_commands"]
        commands[0], commands[1] = commands[1], commands[0]
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("order" in p.lower() for p in result["problems"])

        invocation = valid_invocation()
        commands = invocation["release"]["ordered_commands"]
        commands.insert(1, commands[0])
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("duplicate command" in problem for problem in result["problems"])

        invocation = valid_invocation()
        commands = invocation["precommit"]["ordered_commands"]
        commands[0], commands[1] = commands[1], commands[0]
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("command order" in problem for problem in result["problems"])

    def test_seal_invocation_rejects_empty_precommit(self, tmp_path: Path) -> None:
        """An empty precommit section must fail."""
        invocation = valid_invocation()
        invocation["precommit"]["ordered_commands"] = []
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]

        # A concatenated string must not satisfy list membership checks.
        invocation = valid_invocation()
        invocation["ci"]["ordered_commands"] = "\n".join(seal.ORDERED_COMMANDS)
        write_invocation(tmp_path, invocation)
        result = seal.check_seal_invocation(tmp_path)
        assert not result["ok"]
        assert any("string list" in problem for problem in result["problems"])


# ---------------------------------------------------------------------------
# Full seal check — through the real boundary
# ---------------------------------------------------------------------------


class TestFullSeal:
    def test_check_seal_invocation_passes_on_real_repo(self):
        """The seal invocation check must pass on the real repo."""
        result = seal.check_seal_invocation()
        assert result["ok"], result["problems"]

    def test_check_seal_bytes_on_real_repo(self):
        """The seal bytes check must run on the real repo.

        The manifest must be established and all protected files must match.
        If the manifest is not yet established, this is a real failure, not
        a silent skip. The parent implementation is expected to fail this
        until the manifest is rebaselined with all protected files.
        """
        result = seal.check_seal_bytes()
        # The seal must be established on the real repo
        assert result["code"] != 3, "seal manifest is absent or malformed"
        # All protected files must be sealed and match — this is the real gate.
        # The parent implementation is expected to fail here until the manifest
        # includes all protected files.
        assert result["ok"], (
            f"seal byte drift detected — protected files not sealed or drifted: "
            f"{result['problems'][:3]}"
        )
