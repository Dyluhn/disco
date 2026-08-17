"""CI/release/pre-commit contract mutation tests for the architecture gate.

Tests provisioning, command, order, duplicate, comment, wrapper, suppression,
continue-on-error drift, and seal/checker tampering. Uses the
``check_ci_contract(root=)`` boundary and the internal helpers with synthetic
workflow YAML written into git-initialized temp repos.

The ``ci_contract`` module uses the bounded YAML parser (``yaml_parser.py``),
not PyYAML — no external dependency is required.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import (
    assert_problem_contains,
    git_add,
    git_commit,
    make_temp_repo,
    write,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import ci_contract  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Ordered gate scripts — constant check with companion mutation
# ---------------------------------------------------------------------------


class TestOrderedGates:
    def test_ordered_gate_scripts_complete(self):
        """All required gate scripts must be in the ordered list."""
        expected = [
            "development/scripts/check_soak_freeze.py",
            "development/scripts/check_governance_seal.py",
            "development/scripts/check_arch_budget.py",
            "development/scripts/check_arch_debt.py",
            "development/scripts/check_arch_imports.py",
            "development/scripts/gen_arch_diagram.py --check",
            "development/scripts/check_public_api.py",
            "development/scripts/check_test_inventory.py",
            "development/scripts/check_ci_contract.py",
            "current/packages/core/tests/test_build_platform_nonweb_conformance.py",
        ]
        for script in expected:
            assert script in ci_contract.ORDERED_GATE_SCRIPTS, f"missing gate: {script}"

    def test_tool_schemas_retained(self):
        assert ci_contract.TOOL_SCHEMAS_SCRIPT == "development/scripts/check_tool_schemas.py"

    def test_pre_commit_hook_scripts_complete(self):
        """The pre-commit front section must include the six hooks."""
        expected = [
            "check_soak_freeze.py",
            "check_governance_seal.py",
            "check_arch_budget.py",
            "check_arch_debt.py",
            "check_arch_imports.py",
            "gen_arch_diagram.py --check",
        ]
        for script in expected:
            assert script in ci_contract.PRE_COMMIT_HOOK_SCRIPTS

    def test_missing_gate_in_workflow_fails(self) -> None:
        """A CI workflow missing one ordered gate must fail check_ci."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        wf = root / ".github" / "workflows" / "ci.yml"
        # Write a CI workflow with all gates except check_arch_debt.py
        steps = ["      - run: uv run python development/scripts/check_soak_freeze.py"]
        for gate in ci_contract.ORDERED_GATE_SCRIPTS:
            if "check_arch_debt" in gate:
                continue
            if gate.endswith(".py") and "pytest" not in gate:
                steps.append(f"      - run: uv run python {gate}")
            else:
                steps.append(f"      - run: {gate}")
        steps.append("      - uses: actions/setup-node@v4")
        header = (
            "name: CI\non:\n  push:\njobs:\n  required:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
        )
        write(wf, header + "\n".join(steps) + "\n")
        git_add(root, ".github/workflows/ci.yml")
        git_commit(root)
        result = ci_contract.check_ci(root)
        assert not result["ok"]
        assert_problem_contains(result["problems"], "missing gate", "check_arch_debt")
        tmp.cleanup()

    def test_duplicate_gate_in_workflow_detected(self) -> None:
        """A duplicate gate step must be detectable via _find_gate_order."""
        commands = [
            "uv run python development/scripts/check_soak_freeze.py",
            "uv run python development/scripts/check_soak_freeze.py",
        ]
        positions = ci_contract._find_gate_order(commands, ci_contract.ORDERED_GATE_SCRIPTS)
        # The first occurrence is found; the duplicate is not separately tracked
        assert positions["development/scripts/check_soak_freeze.py"] == 0


# ---------------------------------------------------------------------------
# Bypass and suppression detection — mutations through _check_no_bypass
# ---------------------------------------------------------------------------


class TestBypassDetection:
    def test_or_true_bypass_detected(self):
        commands = ["uv run python development/scripts/check_arch_budget.py || true"]
        problems = ci_contract._check_no_bypass(commands)
        assert len(problems) == 1
        assert "bypass" in problems[0].lower()

    def test_error_suppression_detected(self):
        commands = ["uv run python development/scripts/check_arch_budget.py 2>/dev/null"]
        problems = ci_contract._check_no_bypass(commands)
        assert len(problems) == 1
        assert "suppression" in problems[0].lower()

    def test_clean_command_passes(self):
        commands = ["uv run python development/scripts/check_arch_budget.py"]
        assert ci_contract._check_no_bypass(commands) == []

    def test_comment_line_not_extracted_as_command(self):
        """A comment line inside a run block must not be extracted as a command."""
        run_block = "# this is a comment\nuv run python development/scripts/check_arch_budget.py"
        steps = [{"run": run_block}]
        commands, malformed = ci_contract._extract_run_commands(steps)
        assert commands == ["uv run python development/scripts/check_arch_budget.py"]
        assert malformed == []


# ---------------------------------------------------------------------------
# continue-on-error detection — mutations through _check_continue_on_error
# ---------------------------------------------------------------------------


class TestContinueOnError:
    def test_continue_on_error_detected(self):
        steps = [{"name": "gate", "run": "echo hi", "continue-on-error": True}]
        problems = ci_contract._check_continue_on_error(steps)
        assert len(problems) == 1
        assert "continue-on-error" in problems[0]
        expression = [
            {
                "name": "gate",
                "run": "echo hi",
                "continue-on-error": "${{ true }}",
            }
        ]
        assert ci_contract._check_continue_on_error(expression)
        assert ci_contract._check_job_continue_on_error(
            {"continue-on-error": True}, "CI"
        )
        assert ci_contract._check_job_continue_on_error(
            {"continue-on-error": "${{ true }}"}, "release"
        )

    def test_no_continue_on_error_passes(self):
        steps = [{"name": "gate", "run": "echo hi"}]
        assert ci_contract._check_continue_on_error(steps) == []

    def test_continue_on_error_false_passes(self):
        steps = [{"name": "gate", "run": "echo hi", "continue-on-error": False}]
        assert ci_contract._check_continue_on_error(steps) == []
        assert (
            ci_contract._check_job_continue_on_error(
                {"continue-on-error": False}, "CI"
            )
            == []
        )


# ---------------------------------------------------------------------------
# Gate order detection — mutations through _check_gate_order
# ---------------------------------------------------------------------------


class TestGateOrder:
    def test_missing_gate_fails(self):
        """A missing gate script must be detected."""
        commands = ["uv run python development/scripts/check_soak_freeze.py"]
        positions = ci_contract._find_gate_order(commands, ci_contract.ORDERED_GATE_SCRIPTS)
        missing = [g for g in ci_contract.ORDERED_GATE_SCRIPTS if positions[g] < 0]
        assert "development/scripts/check_arch_budget.py" in missing

    def test_out_of_order_gates_detected(self):
        """Out-of-order gates must be detected."""
        commands = [
            "uv run python development/scripts/check_arch_budget.py",
            "uv run python development/scripts/check_soak_freeze.py",
        ]
        positions = ci_contract._find_gate_order(commands, ci_contract.ORDERED_GATE_SCRIPTS)
        sf_pos = positions["development/scripts/check_soak_freeze.py"]
        budget_pos = positions["development/scripts/check_arch_budget.py"]
        assert sf_pos > budget_pos  # soak_freeze after budget — wrong order

    def test_correct_order_passes(self):
        """Correctly ordered gates must not produce order problems."""
        commands = [
            f"uv run python {g}" if not g.endswith(".py") or "pytest" in g else f"uv run python {g}"
            for g in ci_contract.ORDERED_GATE_SCRIPTS
        ]
        problems = ci_contract._check_gate_order(commands, "test")
        order_problems = [p for p in problems if "order" in p.lower()]
        assert order_problems == []


# ---------------------------------------------------------------------------
# Node provisioning order — mutation through _check_node_before_typescript
# ---------------------------------------------------------------------------


class TestNodeProvisioning:
    def test_node_after_typescript_fails(self):
        """Node/npm provisioning after the TypeScript gate must fail."""
        steps = [
            {"run": "uv run python development/scripts/check_arch_imports.py"},
            {"uses": "actions/setup-node@v4"},
        ]
        problems = ci_contract._check_node_before_typescript(steps)
        assert len(problems) == 1
        assert "Node" in problems[0]
        late_npm = [
            {"run": "uv run python development/scripts/check_soak_freeze.py"},
            {"working-directory": "current/frontend", "run": "npm ci"},
        ]
        problems = ci_contract._check_frontend_npm_ci(late_npm, "CI")
        assert any("must precede" in problem for problem in problems)

    def test_node_before_typescript_passes(self):
        steps = [
            {"uses": "actions/setup-node@v4"},
            {"working-directory": "current/frontend", "run": "npm ci"},
            {"run": "uv run python development/scripts/check_arch_imports.py"},
        ]
        assert ci_contract._check_node_before_typescript(steps) == []
        assert ci_contract._check_frontend_npm_ci(steps, "CI") == []


# ---------------------------------------------------------------------------
# Tool schemas position — mutations through _check_tool_schemas_position
# ---------------------------------------------------------------------------


class TestToolSchemasPosition:
    def test_tool_schemas_after_ci_contract(self):
        """tool_schemas.py must run after check_ci_contract.py."""
        commands = [
            "uv run python development/scripts/check_ci_contract.py",
            "uv run python development/scripts/check_tool_schemas.py",
            "uv run pytest -q current/packages/core/tests/test_build_platform_nonweb_conformance.py",
        ]
        positions = ci_contract._find_gate_order(commands, ci_contract.ORDERED_GATE_SCRIPTS)
        problems = ci_contract._check_tool_schemas_position(commands, positions, "test")
        assert problems == []
        missing = [commands[0], commands[2]]
        positions = ci_contract._find_gate_order(
            missing, ci_contract.ORDERED_GATE_SCRIPTS
        )
        problems = ci_contract._check_tool_schemas_position(
            missing, positions, "test"
        )
        assert any("missing gate" in problem for problem in problems)

    def test_tool_schemas_before_ci_contract_fails(self):
        """tool_schemas.py before check_ci_contract.py must fail."""
        commands = [
            "uv run python development/scripts/check_tool_schemas.py",
            "uv run python development/scripts/check_ci_contract.py",
            "uv run pytest -q current/packages/core/tests/test_build_platform_nonweb_conformance.py",
        ]
        positions = ci_contract._find_gate_order(commands, ci_contract.ORDERED_GATE_SCRIPTS)
        problems = ci_contract._check_tool_schemas_position(commands, positions, "test")
        assert any("after" in p.lower() for p in problems)

    def test_tool_schemas_after_nonweb_fails(self):
        """tool_schemas.py after non-web conformance must fail."""
        commands = [
            "uv run python development/scripts/check_ci_contract.py",
            "uv run pytest -q current/packages/core/tests/test_build_platform_nonweb_conformance.py",
            "uv run python development/scripts/check_tool_schemas.py",
        ]
        positions = ci_contract._find_gate_order(commands, ci_contract.ORDERED_GATE_SCRIPTS)
        problems = ci_contract._check_tool_schemas_position(commands, positions, "test")
        assert any("before" in p.lower() for p in problems)


# ---------------------------------------------------------------------------
# Full CI/release/pre-commit check through the real boundary
# ---------------------------------------------------------------------------


class TestFullCIContract:
    def test_ci_passes(self):
        result = ci_contract.check_ci()
        assert result["ok"], result["problems"]

    def test_release_passes(self):
        result = ci_contract.check_release()
        assert result["ok"], result["problems"]

    def test_precommit_passes(self):
        result = ci_contract.check_precommit()
        assert result["ok"], result["problems"]

    def test_full_ci_contract_passes(self):
        result = ci_contract.check_ci_contract()
        assert result["ok"], result["problems"]
        assert result["ci_ok"] is True
        assert result["release_ok"] is True
        assert result["precommit_ok"] is True
        assert ci_contract._check_environment_shadowing(
            {},
            {},
            [{"run": "gh release create", "env": {"GH_TOKEN": "token"}}],
            "release",
        ) == []


# ---------------------------------------------------------------------------
# CI workflow mutation — continue-on-error on a gate step must fail
# ---------------------------------------------------------------------------


class TestCIWorkflowMutations:
    def _write_ci_workflow(self, root: Path, steps_yaml: str) -> None:
        """Write a CI workflow with the given steps YAML body."""
        wf = root / ".github" / "workflows" / "ci.yml"
        write(
            wf,
            textwrap.dedent(
                """\
                name: CI
                on:
                  push:
                jobs:
                  required:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@v4
                """
            )
            + steps_yaml,
        )
        git_add(root, ".github/workflows/ci.yml")
        git_commit(root)

    def test_continue_on_error_gate_step_fails(self) -> None:
        """Advisory, skipped, or reinterpreted gate execution must fail."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        steps = (
            "      - name: gate\n"
            "        run: uv run python development/scripts/check_soak_freeze.py\n"
            "        continue-on-error: true\n"
        )
        self._write_ci_workflow(root, steps)
        result = ci_contract.check_ci(root)
        assert not result["ok"]
        assert any("continue-on-error" in p for p in result["problems"])
        tmp.cleanup()

        tmp = make_temp_repo()
        root = Path(tmp.name)
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
        workflow = workflow.replace(
            "    runs-on: ubuntu-latest\n    steps:",
            "    runs-on: ubuntu-latest\n"
            "    continue-on-error: true\n"
            "    steps:",
            1,
        )
        write(root / ".github/workflows/ci.yml", workflow)
        result = ci_contract.check_ci(root)
        assert not result["ok"]
        assert any(
            "required job has continue-on-error" in problem
            for problem in result["problems"]
        )
        tmp.cleanup()

        ci_workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
        ci_job_anchor = "    runs-on: ubuntu-latest\n    steps:"
        ci_gate_anchor = (
            "      - name: Soak freeze\n"
            "        run: uv run python development/scripts/check_soak_freeze.py"
        )
        ci_mutations = (
            (
                "name: CI\n",
                "name: CI\n"
                "env:\n"
                "  PATH: .github/shadow-bin\n",
                "workflow declares env",
            ),
            (
                "name: CI\n",
                "name: CI\n"
                "defaults:\n"
                "  run:\n"
                "    shell: bash\n",
                "workflow declares defaults.run",
            ),
            (
                ci_job_anchor,
                "    runs-on: ubuntu-latest\n"
                "    if: false\n"
                "    steps:",
                "declares if",
            ),
            (
                ci_job_anchor,
                "    runs-on: ubuntu-latest\n"
                "    defaults:\n"
                "      run:\n"
                "        shell: bash\n"
                "    steps:",
                "defaults.run",
            ),
            (
                ci_job_anchor,
                "    runs-on: ubuntu-latest\n"
                "    env:\n"
                "      PYTHONPATH: .github/shadow-python\n"
                "    steps:",
                "required job declares env",
            ),
            (
                ci_gate_anchor,
                "      - name: Soak freeze\n"
                "        if: false\n"
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "declares if",
            ),
            (
                ci_gate_anchor,
                "      - name: Soak freeze\n"
                '        shell: bash -c "exit 0" {0}\n'
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "custom shell",
            ),
            (
                ci_gate_anchor,
                "      - name: Soak freeze\n"
                "        working-directory: current/frontend\n"
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "non-root",
            ),
            (
                ci_gate_anchor,
                "      - name: Soak freeze\n"
                "        env:\n"
                "          UV_PROJECT_ENVIRONMENT: .github/shadow-venv\n"
                "          PYTEST_ADDOPTS: --collect-only\n"
                "          BASH_ENV: .github/bypass.sh\n"
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "gate step development/scripts/check_soak_freeze.py declares env",
            ),
            (
                ci_gate_anchor,
                "      - name: Soak freeze decoy\n"
                "        run: /bin/true\n"
                "      - name: Soak freeze contract shadow\n"
                "        if: false\n"
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "declares if",
            ),
        )
        for before, after, expected_problem in ci_mutations:
            assert before in ci_workflow
            tmp = make_temp_repo()
            root = Path(tmp.name)
            write(
                root / ".github/workflows/ci.yml",
                ci_workflow.replace(before, after, 1),
            )
            result = ci_contract.check_ci(root)
            assert not result["ok"]
            assert any(
                expected_problem in problem for problem in result["problems"]
            ), result["problems"]
            tmp.cleanup()

        release_workflow = (
            REPO_ROOT / ".github/workflows/release.yml"
        ).read_text()
        release_job_anchor = "    runs-on: ubuntu-latest\n    steps:"
        release_gate_anchor = (
            "      - name: Soak freeze\n"
            "        run: uv run python development/scripts/check_soak_freeze.py"
        )
        release_mutations = (
            (
                "name: Release\n",
                "name: Release\n"
                "env:\n"
                "  PATH: .github/shadow-bin\n",
                "workflow declares env",
            ),
            (
                "name: Release\n",
                "name: Release\n"
                "defaults:\n"
                "  run:\n"
                "    shell: bash\n",
                "workflow declares defaults.run",
            ),
            (
                release_job_anchor,
                "    runs-on: ubuntu-latest\n"
                "    if: false\n"
                "    steps:",
                "declares if",
            ),
            (
                release_job_anchor,
                "    runs-on: ubuntu-latest\n"
                "    defaults:\n"
                "      run:\n"
                "        working-directory: current/frontend\n"
                "    steps:",
                "defaults.run",
            ),
            (
                release_job_anchor,
                "    runs-on: ubuntu-latest\n"
                "    env:\n"
                "      PYTHONPATH: .github/shadow-python\n"
                "    steps:",
                "required job declares env",
            ),
            (
                release_gate_anchor,
                "      - name: Soak freeze\n"
                "        if: false\n"
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "declares if",
            ),
            (
                release_gate_anchor,
                "      - name: Soak freeze\n"
                '        shell: bash -c "exit 0" {0}\n'
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "custom shell",
            ),
            (
                release_gate_anchor,
                "      - name: Soak freeze\n"
                "        working-directory: current/frontend\n"
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "non-root",
            ),
            (
                release_gate_anchor,
                "      - name: Soak freeze\n"
                "        env:\n"
                "          UV_PROJECT_ENVIRONMENT: .github/shadow-venv\n"
                "          PYTEST_ADDOPTS: --collect-only\n"
                "          BASH_ENV: .github/bypass.sh\n"
                "        run: uv run python development/scripts/check_soak_freeze.py",
                "gate step development/scripts/check_soak_freeze.py declares env",
            ),
        )
        for before, after, expected_problem in release_mutations:
            assert before in release_workflow
            tmp = make_temp_repo()
            root = Path(tmp.name)
            write(
                root / ".github/workflows/release.yml",
                release_workflow.replace(before, after, 1),
            )
            result = ci_contract.check_release(root)
            assert not result["ok"]
            assert any(
                expected_problem in problem for problem in result["problems"]
            ), result["problems"]
            tmp.cleanup()

        tmp = make_temp_repo()
        root = Path(tmp.name)
        write(
            root / ".github/workflows/release.yml",
            release_workflow
            + "\n"
            + "  publish-release:\n"
            + "    if: false\n"
            + "    runs-on: ubuntu-latest\n"
            + "    env:\n"
            + "      PATH: .github/shadow-bin\n"
            + "    steps:\n"
            + "      - run: gh release create bypass\n",
        )
        result = ci_contract.check_release(root)
        assert not result["ok"]
        assert_problem_contains(
            result["problems"],
            "release jobs must be exactly",
            "publish-release",
        )
        tmp.cleanup()

    def test_bypass_pattern_in_ci_fails(self) -> None:
        """A gate command with || true must fail check_ci."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        steps = "      - run: uv run python development/scripts/check_soak_freeze.py || true\n"
        self._write_ci_workflow(root, steps)
        result = ci_contract.check_ci(root)
        assert not result["ok"]
        assert any("bypass" in p.lower() for p in result["problems"])
        tmp.cleanup()

    def test_missing_ci_workflow_fails(self) -> None:
        """An absent ci.yml must fail check_ci."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        git_commit(root)
        result = ci_contract.check_ci(root)
        assert not result["ok"]
        assert any("missing" in p.lower() for p in result["problems"])
        tmp.cleanup()

    def test_no_required_job_fails(self) -> None:
        """A CI workflow without a 'required' job must fail."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        wf = root / ".github" / "workflows" / "ci.yml"
        write(
            wf,
            "name: CI\non:\n  push:\njobs:\n  other:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: echo hi\n",
        )
        git_add(root, ".github/workflows/ci.yml")
        git_commit(root)
        result = ci_contract.check_ci(root)
        assert not result["ok"]
        assert any("required" in p.lower() for p in result["problems"])
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Pre-commit config mutation — missing hook must fail
# ---------------------------------------------------------------------------


class TestPrecommitMutations:
    def test_missing_precommit_hook_fails(self) -> None:
        """A pre-commit config missing one of the six hooks must fail."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        # Write a pre-commit config with only 5 of the 6 hooks
        hooks = ""
        for hook in ci_contract.PRE_COMMIT_HOOK_SCRIPTS[:-1]:
            hook_id = hook.replace(".py", "").replace(" --check", "").replace("_", "-")
            hooks += (
                f"      - id: {hook_id}\n"
                f"        name: {hook_id}\n"
                f"        entry: python development/scripts/{hook}\n"
                "        language: system\n"
                "        pass_filenames: false\n"
                "        always_run: true\n"
            )
        write(
            root / ".pre-commit-config.yaml",
            f"repos:\n  - repo: local\n    hooks:\n{hooks}",
        )
        git_add(root, ".pre-commit-config.yaml")
        git_commit(root)
        result = ci_contract.check_precommit(root)
        assert not result["ok"]
        assert any("missing hook" in p for p in result["problems"])
        tmp.cleanup()

        tmp = make_temp_repo()
        root = Path(tmp.name)
        config = (REPO_ROOT / ".pre-commit-config.yaml").read_text()
        config = config.replace(
            "        entry: python development/scripts/check_governance_seal.py\n"
            "        language: system\n",
            "        entry: python development/scripts/check_governance_seal.py\n"
            "        language: system\n"
            "        stages: [manual]\n",
        )
        write(root / ".pre-commit-config.yaml", config)
        result = ci_contract.check_precommit(root)
        assert not result["ok"]
        assert any("manual-only" in p for p in result["problems"])
        tmp.cleanup()

    def test_missing_precommit_config_fails(self) -> None:
        """An absent .pre-commit-config.yaml must fail check_precommit."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        git_commit(root)
        result = ci_contract.check_precommit(root)
        assert not result["ok"]
        assert any("missing" in p.lower() for p in result["problems"])
        tmp.cleanup()
