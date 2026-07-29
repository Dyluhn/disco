"""TypeScript scanner mutation tests for the architecture gate.

Tests the pinned-compiler boundary: oversized TS module/component/hook, parse
errors, and compiler version mismatch. Uses the ``scan_typescript(root=)``
boundary and the Node scanner script with git-initialized temp repos.

The pinned TypeScript compiler (5.9.3) must be provisioned via ``npm ci`` in
``frontend/``. Node must be on PATH. There is no skip — a missing compiler or
Node is a provisioning failure, not a silent pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import git_commit, make_temp_repo, write, write_and_track

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from architecture import typescript_scan  # noqa: E402


def write_context_authority(
    root: Path,
    *,
    extra_contexts: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    identity = "fixture-source"
    source = "frontend/src/components/Legacy.tsx"
    target = "frontend/src/api/dummy.ts"
    contexts: dict[str, dict[str, list[str]]] = {
        "legacy_source": {"modules": ["components/Legacy"]},
        "legacy_target": {"modules": ["api/dummy", "api/moved"]},
    }
    contexts.update(extra_contexts or {})
    registry = {
        "schema": "disclaude-architecture-contexts-v1",
        "source_identity": identity,
        "bounded_contexts": {
            "frontend": {
                "root": "frontend/src",
                "contexts": contexts,
                "dm007_legacy": {
                    "observation_id": "DM-007",
                    "source_identity": identity,
                    "owner_package": "PKG-12-FE-BUILD",
                    "edge_fields": [
                        "source",
                        "source_context",
                        "import",
                        "target",
                        "target_context",
                    ],
                    "edges": [
                        [
                            source,
                            "legacy_source",
                            "../api/dummy",
                            target,
                            "legacy_target",
                        ]
                    ],
                    "cycles": [],
                },
                "cross_feature_imports_rejected": True,
            }
        },
    }
    write(root / "architecture/contexts.json", json.dumps(registry))
    write_and_track(
        root,
        source,
        'import { dummy } from "../api/dummy";\nexport const legacy = dummy;\n',
    )
    write_and_track(root, target, "export const dummy = 1;\n")


class TestCompilerCheck:
    def test_compiler_mismatch_raises(self, tmp_path: Path) -> None:
        """A wrong compiler version must raise RuntimeError."""
        fake_pkg = tmp_path / "typescript" / "package.json"
        write(fake_pkg, json.dumps({"version": "1.0.0"}))
        original = typescript_scan.TS_PACKAGE_JSON
        try:
            typescript_scan.TS_PACKAGE_JSON = fake_pkg
            with pytest.raises(RuntimeError, match="version mismatch"):
                typescript_scan.check_compiler()
        finally:
            typescript_scan.TS_PACKAGE_JSON = original

    def test_compiler_missing_raises(self, tmp_path: Path) -> None:
        """An absent compiler must raise FileNotFoundError."""
        original = typescript_scan.TS_PACKAGE_JSON
        try:
            typescript_scan.TS_PACKAGE_JSON = tmp_path / "nonexistent" / "package.json"
            with pytest.raises(FileNotFoundError):
                typescript_scan.check_compiler()
        finally:
            typescript_scan.TS_PACKAGE_JSON = original

    def test_pinned_compiler_version_is_593(self):
        """The required TypeScript compiler version must be 5.9.3."""
        assert typescript_scan.REQUIRED_TS_VERSION == "5.9.3"

    def test_pinned_compiler_is_installed(self):
        """The pinned TypeScript compiler must be installed and match 5.9.3."""
        version = typescript_scan.check_compiler()
        assert version == "5.9.3"


class TestTypeScriptScanMutations:
    """TypeScript scanner mutations that must fail their intended rules.

    These tests require the pinned TypeScript compiler and Node. They build
    temp repos with tracked .tsx/.ts files and invoke the scanner.
    """

    def _scan_file_via_node(self, root: Path, rel: str) -> dict:
        """Run the Node scanner on a single file and return its result."""
        scanner_script = (
            Path(__file__).resolve().parents[2] / "scripts" / "architecture" / "ts_scan.mjs"
        )
        result = subprocess.run(
            [
                "node", str(scanner_script),
                "--repo", str(root),
                "--compiler", str(typescript_scan.TS_COMPILER_PATH),
            ],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(result.stdout)
        for module in payload.get("modules", []):
            if module["path"] == rel:
                return module
        return {"violations": [], "parse_diagnostics": []}

    def test_oversized_ts_module_fails(self) -> None:
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = "const x = 1;\n" * 501
        write_and_track(root, "frontend/src/big.ts", code)
        write_context_authority(root)
        git_commit(root)
        module = self._scan_file_via_node(root, "frontend/src/big.ts")
        v = [v for v in module["violations"] if v["rule"] == "typescript_module_logical_gt_500"]
        rules = [row["rule"] for row in module["violations"]]
        assert len(v) == 1, f"expected typescript_module_logical_gt_500, got {rules}"
        assert v[0]["value"] > 500
        tmp.cleanup()

    def test_oversized_ts_component_fails(self) -> None:
        tmp = make_temp_repo()
        root = Path(tmp.name)
        lines = ["export function MyComponent() {", "  return <div>"]
        lines += [f"    <span>{i}</span>" for i in range(250)]
        lines += ["  </div>", "}"]
        write_and_track(root, "frontend/src/BigComp.tsx", "\n".join(lines))
        write_context_authority(root)
        git_commit(root)
        module = self._scan_file_via_node(root, "frontend/src/BigComp.tsx")
        v = [v for v in module["violations"] if v["rule"] == "react_component_logical_gt_250"]
        rules = [row["rule"] for row in module["violations"]]
        assert len(v) == 1, f"expected react_component_logical_gt_250, got {rules}"
        tmp.cleanup()

    def test_oversized_ts_hook_fails(self) -> None:
        tmp = make_temp_repo()
        root = Path(tmp.name)
        lines = ["export function useBigHook() {"]
        lines += [f"  const x{i} = {i};" for i in range(201)]
        lines += ["}"]
        write_and_track(root, "frontend/src/useBig.ts", "\n".join(lines))
        write_context_authority(root)
        git_commit(root)
        module = self._scan_file_via_node(root, "frontend/src/useBig.ts")
        v = [v for v in module["violations"] if v["rule"] == "react_hook_logical_gt_200"]
        rules = [row["rule"] for row in module["violations"]]
        assert len(v) == 1, f"expected react_hook_logical_gt_200, got {rules}"
        tmp.cleanup()

    def test_ts_parse_error_collected(self) -> None:
        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(root, "frontend/src/bad.ts", "const x = {\n")
        write_context_authority(root)
        git_commit(root)
        module = self._scan_file_via_node(root, "frontend/src/bad.ts")
        assert module["parse_diagnostics"], "parse diagnostics expected for broken TS"
        tmp.cleanup()

    def test_oversized_ts_test_module_fails(self) -> None:
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = "const x = 1;\n" * 1201
        write_and_track(root, "frontend/src/big.test.ts", code)
        write_context_authority(root)
        git_commit(root)
        module = self._scan_file_via_node(root, "frontend/src/big.test.ts")
        v = [v for v in module["violations"] if v["rule"] == "test_module_logical_gt_1200"]
        assert len(v) == 1
        tmp.cleanup()

    def test_small_ts_module_passes(self) -> None:
        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(root, "frontend/src/small.ts", "const x = 1;\n")
        with pytest.raises(subprocess.CalledProcessError):
            self._scan_file_via_node(root, "frontend/src/small.ts")
        write(root / "architecture/contexts.json", "{}")
        with pytest.raises(subprocess.CalledProcessError):
            self._scan_file_via_node(root, "frontend/src/small.ts")
        write_context_authority(root)
        git_commit(root)
        module = self._scan_file_via_node(root, "frontend/src/small.ts")
        assert module["violations"] == [], f"expected no violations, got {module['violations']}"
        tmp.cleanup()

    def test_scan_typescript_boundary_runs_on_real_repo(self) -> None:
        """The scan_typescript(root=) boundary must run on the real repo."""
        result = typescript_scan.scan_typescript()
        assert "modules" in result
        assert result["compiler_version"] == "5.9.3"
        # The real repo must have at least one tracked TS file
        assert len(result["modules"]) > 0


# ---------------------------------------------------------------------------
# TS context cycle / cross-feature / DM-007 drift
# ---------------------------------------------------------------------------


class TestTSContextGraph:
    """Tests for compiler-derived TS context cycle and cross-feature detection.

    The frontend bounded context declares ``cross_feature_imports_rejected``.
    The TS scanner must detect cross-feature imports (e.g. build importing
    research modules) and context cycles using the pinned compiler's import
    resolution. These are expected reds until the F writer adds context-graph
    enforcement to the TS scanner.
    """

    def test_cross_feature_import_detected(self) -> None:
        """A cross-feature import (build -> research) must be detected.

        The frontend bounded context rejects cross-feature imports. A build
        module importing a research module is a cross-feature violation.
        """
        tmp = make_temp_repo()
        root = Path(tmp.name)
        # build feature module imports research feature module
        write_and_track(
            root,
            "frontend/src/hooks/useBuild.ts",
            "import { useDeepResearch } from '../hooks/useDeepResearch';\n"
            "export function useBuild() { return 1; }\n",
        )
        write_and_track(
            root,
            "frontend/src/hooks/useDeepResearch.ts",
            "export function useDeepResearch() { return 2; }\n",
        )
        write_context_authority(
            root,
            extra_contexts={
                "build": {"modules": ["hooks/useBuild"]},
                "research": {"modules": ["hooks/useDeepResearch"]},
            },
        )
        git_commit(root)
        result = typescript_scan.scan_typescript(root)
        problems = result["frontend_context_policy_problems"]
        edge_drift = next(
            row
            for row in problems
            if row["rule"] == "dm007_exact_legacy_edge_drift"
        )
        assert any("useBuild" in row for row in edge_drift["added"])

        write(
            root / "frontend/src/hooks/useBuild.ts",
            "export async function useBuild() {\n"
            "  return import('./useDeepResearch');\n"
            "}\n",
        )
        dynamic = typescript_scan.scan_typescript(root)
        dynamic_drift = next(
            row
            for row in dynamic["frontend_context_policy_problems"]
            if row["rule"] == "dm007_exact_legacy_edge_drift"
        )
        assert any("useDeepResearch" in row for row in dynamic_drift["added"])

        write(
            root / "frontend/src/hooks/useBuild.ts",
            "const target = './useDeepResearch';\n"
            "export async function useBuild() { return import(target); }\n",
        )
        nonliteral = typescript_scan.scan_typescript(root)
        assert any(
            row["rule"] == "typescript_nonliteral_dynamic_import"
            and row["path"].endswith("useBuild.ts")
            for row in nonliteral["frontend_context_policy_problems"]
        )

        write(
            root / "frontend/src/hooks/useBuild.ts",
            "export async function useBuild() {\n"
            "  return import('./useDeepResearch');\n"
            "}\n",
        )
        write_and_track(
            root,
            "frontend/src/api/moved.ts",
            "export const moved = 2;\n",
        )
        write(
            root / "frontend/src/components/Legacy.tsx",
            'import { moved } from "../api/moved";\n'
            "export const legacy = moved;\n",
        )
        moved = typescript_scan.scan_typescript(root)
        moved_drift = next(
            row
            for row in moved["frontend_context_policy_problems"]
            if row["rule"] == "dm007_exact_legacy_edge_drift"
        )
        assert any("api/dummy" in row for row in moved_drift["deleted"])
        assert any("api/moved" in row for row in moved_drift["added"])
        tmp.cleanup()

    def test_ts_context_cycle_detected(self) -> None:
        """A TS context cycle (A imports B imports A) must be detected.

        The frontend bounded contexts must not form import cycles. The TS
        scanner must detect cycles using the pinned compiler's import graph.
        """
        tmp = make_temp_repo()
        root = Path(tmp.name)
        # Two modules that import each other — a cycle
        write_and_track(
            root,
            "frontend/src/modA.ts",
            "import { b } from './modB';\nexport const a = 1;\n",
        )
        write_and_track(
            root,
            "frontend/src/modB.ts",
            "import { a } from './modA';\nexport const b = 2;\n",
        )
        write_context_authority(
            root,
            extra_contexts={
                "context_a": {"modules": ["modA"]},
                "context_b": {"modules": ["modB"]},
            },
        )
        git_commit(root)
        result = typescript_scan.scan_typescript(root)
        assert result["frontend_context_cycles"]
        assert any(
            row["rule"] == "dm007_exact_legacy_cycle_drift"
            for row in result["frontend_context_policy_problems"]
        )
        tmp.cleanup()

    def test_same_feature_import_passes(self) -> None:
        """An import within the same feature (build -> build) must pass."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        # Two build-feature modules importing each other is same-feature
        write_and_track(
            root,
            "frontend/src/hooks/useBuild.ts",
            "import { useBuildStream } from './useBuildStream';\n"
            "export function useBuild() { return 1; }\n",
        )
        write_and_track(
            root,
            "frontend/src/hooks/useBuildStream.ts",
            "export function useBuildStream() { return 2; }\n",
        )
        write_context_authority(
            root,
            extra_contexts={
                "build": {
                    "modules": ["hooks/useBuild", "hooks/useBuildStream"]
                }
            },
        )
        git_commit(root)
        result = typescript_scan.scan_typescript(root)
        assert result["frontend_context_policy_problems"] == []
        tmp.cleanup()
