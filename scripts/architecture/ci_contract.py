"""Fail-closed structural CI, release, and pre-commit contract enforcement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ci_contract_sealed import (
    TOOL_SCHEMAS_SCRIPT,
    _check_tool_schemas_position,
    _find_gate_order,
    check_sealed_ordered_contract,
    enforced_ordered_commands,
)
from .policy import REPO_ROOT
from .yaml_parser import load_yaml

# The exact ordered gate scripts that must appear in both CI and release.
# Each entry is the script path (plus any required arguments) that the
# gate command must invoke exactly — no substring, no rename, no extra args.
ORDERED_GATE_SCRIPTS = [
    "scripts/check_soak_freeze.py",
    "scripts/check_governance_seal.py",
    "scripts/check_arch_budget.py",
    "scripts/check_arch_debt.py",
    "scripts/check_arch_imports.py",
    "scripts/gen_arch_diagram.py --check",
    "scripts/check_public_api.py",
    "scripts/check_test_inventory.py",
    "scripts/check_ci_contract.py",
    "packages/core/tests/test_build_platform_nonweb_conformance.py",
]

# The pre-commit front section must include these hooks (same structural order).
PRE_COMMIT_HOOK_SCRIPTS = [
    "check_soak_freeze.py",
    "check_governance_seal.py",
    "check_arch_budget.py",
    "check_arch_debt.py",
    "check_arch_imports.py",
    "gen_arch_diagram.py --check",
]

# The exact argv that each pre-commit hook entry must produce after
# tokenization. Matching is by exact token sequence, never by substring
# or ``endswith``.
PRE_COMMIT_ARGV: dict[str, list[str]] = {
    "check_soak_freeze.py": [
        "python", "scripts/check_soak_freeze.py",
    ],
    "check_governance_seal.py": [
        "python", "scripts/check_governance_seal.py",
    ],
    "check_arch_budget.py": [
        "python", "scripts/check_arch_budget.py",
    ],
    "check_arch_debt.py": [
        "python", "scripts/check_arch_debt.py",
    ],
    "check_arch_imports.py": [
        "python", "scripts/check_arch_imports.py",
    ],
    "gen_arch_diagram.py --check": [
        "python", "scripts/gen_arch_diagram.py", "--check",
    ],
}

# The exact argv that each gate command must produce after tokenization.
# Matching is by exact token sequence, never by substring.
GATE_ARGV: dict[str, list[str]] = {
    "scripts/check_soak_freeze.py": [
        "uv", "run", "python", "scripts/check_soak_freeze.py",
    ],
    "scripts/check_governance_seal.py": [
        "uv", "run", "python", "scripts/check_governance_seal.py",
    ],
    "scripts/check_arch_budget.py": [
        "uv", "run", "python", "scripts/check_arch_budget.py",
    ],
    "scripts/check_arch_debt.py": [
        "uv", "run", "python", "scripts/check_arch_debt.py",
    ],
    "scripts/check_arch_imports.py": [
        "uv", "run", "python", "scripts/check_arch_imports.py",
    ],
    "scripts/gen_arch_diagram.py --check": [
        "uv", "run", "python", "scripts/gen_arch_diagram.py", "--check",
    ],
    "scripts/check_public_api.py": [
        "uv", "run", "python", "scripts/check_public_api.py",
    ],
    "scripts/check_test_inventory.py": [
        "uv", "run", "python", "scripts/check_test_inventory.py",
    ],
    "scripts/check_ci_contract.py": [
        "uv", "run", "python", "scripts/check_ci_contract.py",
    ],
    "scripts/check_tool_schemas.py": [
        "uv", "run", "python", "scripts/check_tool_schemas.py",
    ],
    "packages/core/tests/test_build_platform_nonweb_conformance.py": [
        "uv", "run", "pytest", "-q",
        "packages/core/tests/test_build_platform_nonweb_conformance.py",
    ],
}

# The exact provisioning command that must appear before the gates in both
# CI and release.
PROVISIONING_ARGV = ["uv", "sync", "--all-packages", "--frozen"]
RELEASE_JOB = "draft-release"

# Shell metacharacters that indicate non-standalone commands (chaining,
# redirect, suppression, background). A gate command must contain none.
SHELL_METACHARACTERS = (
    "||", "&&", "|", ">", "<", ";", "&", "`", "$(",
    "2>/dev/null", ">>",
)


def _load_yaml(path: Path) -> Any:
    """Load a YAML file structurally with the bounded parser."""
    if not path.is_file():
        return None
    return load_yaml(path)


def _tokenize(command: str) -> tuple[list[str], str]:
    """Tokenize exact argv without shell expansion; reject comments/quotes."""
    tokens: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if in_single:
            if ch == "'":
                in_single = False
            else:
                current.append(ch)
        elif in_double:
            if ch == '"':
                in_double = False
            else:
                current.append(ch)
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "#":
            # Comment: REJECTED, not stripped. A gate command must not
            # contain comments.
            return [], f"comment in command: {command}"
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1
    # Unmatched quotes are REJECTED.
    if in_single:
        return [], f"unmatched single quote in command: {command}"
    if in_double:
        return [], f"unmatched double quote in command: {command}"
    if current:
        tokens.append("".join(current))
    return tokens, ""


def _extract_run_commands(steps: list[Any]) -> tuple[list[str], list[str]]:
    """Extract run commands and reject gates hidden in multiline blocks."""
    commands: list[str] = []
    multiline_problems: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if not run:
            continue
        lines = run.splitlines()
        non_empty = [
            line
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        if len(non_empty) > 1:
            # Multi-line run: check if any line is a gate command
            for line in non_empty:
                line = line.strip()
                actual_argv, _ = _tokenize(line)
                for gate, expected_argv in GATE_ARGV.items():
                    if actual_argv == expected_argv:
                        multiline_problems.append(
                            f"gate {gate} must be a standalone "
                            f"command, not inside a multi-line run block"
                        )
                        break
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            commands.append(line)
    return commands, multiline_problems


def _check_no_bypass(commands: list[str]) -> list[str]:
    """Reject chaining or suppression in commands that reference a gate."""
    problems: list[str] = []
    gate_scripts = [
        g.split()[0] for g in GATE_ARGV
    ]
    for cmd in commands:
        # Only check commands that reference a gate script
        is_gate = any(script in cmd for script in gate_scripts)
        if not is_gate:
            continue
        if "|| true" in cmd or "||true" in cmd:
            problems.append(f"bypass pattern '|| true' in gate command: {cmd}")
            continue
        if "2>/dev/null" in cmd:
            problems.append(f"error suppression in gate command: {cmd}")
            continue
        for metachar in SHELL_METACHARACTERS:
            if metachar in cmd:
                problems.append(
                    f"bypass pattern {metachar!r} in gate command: {cmd}"
                )
                break
    return problems


def _check_continue_on_error(steps: list[Any]) -> list[str]:
    problems: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        value = step.get("continue-on-error")
        if value is not None and value is not False:
            name = step.get("name", step.get("run", "?"))
            problems.append(f"continue-on-error on required step: {name}")
        commands, _ = _extract_run_commands([step])
        gate = next((
            name for name, expected in GATE_ARGV.items()
            if any(_tokenize(command)[0] == expected for command in commands)
        ), None)
        if gate is None:
            continue
        if "if" in step:
            problems.append(f"gate step {gate} declares if and can be skipped")
        if "shell" in step:
            problems.append(f"gate step {gate} declares a custom shell")
        working_directory = step.get("working-directory")
        if working_directory not in (None, ".", "./"):
            problems.append(
                f"gate {gate} uses non-root directory: {working_directory!r}"
            )
    return problems


def _check_job_continue_on_error(job: dict[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    value = job.get("continue-on-error")
    if value is not None and value is not False:
        problems.append(f"{label} required job has continue-on-error: {value!r}")
    if "if" in job:
        problems.append(f"{label} required job declares if and can be skipped")
    defaults = job.get("defaults")
    if isinstance(defaults, dict) and "run" in defaults:
        problems.append(f"{label} required job declares defaults.run")
    return problems


def _check_environment_shadowing(
    workflow: dict[str, Any], job: dict[str, Any], steps: list[Any], label: str,
) -> list[str]:
    problems: list[str] = []
    if "env" in workflow:
        problems.append(
            f"{label} workflow declares env and can shadow required commands"
        )
    defaults = workflow.get("defaults")
    if isinstance(defaults, dict) and "run" in defaults:
        problems.append(
            f"{label} workflow declares defaults.run and can reinterpret "
            "required commands"
        )
    if "env" in job:
        problems.append(
            f"{label} required job declares env and can shadow required commands"
        )

    for step in steps:
        if not isinstance(step, dict) or "env" not in step:
            continue
        commands, _ = _extract_run_commands([step])
        gate = next((
            name for name, expected in GATE_ARGV.items()
            if any(_tokenize(command)[0] == expected for command in commands)
        ), None)
        if gate is not None:
            problems.append(
                f"{label} gate step {gate} declares env and can shadow "
                "the exact command"
            )
    return problems


def _check_node_before_typescript(steps: list[Any]) -> list[str]:
    """Check that Node/npm provisioning precedes the TypeScript scanner/import gate."""
    problems: list[str] = []
    node_pos = -1
    ts_pos = -1
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        uses = step.get("uses", "")
        if "actions/setup-node" in str(uses):
            node_pos = i
        run = step.get("run", "")
        if "check_arch_imports" in str(run):
            ts_pos = i
    if node_pos >= 0 and ts_pos >= 0 and node_pos > ts_pos:
        problems.append(
            "Node/npm provisioning must precede the TypeScript scanner/import gate"
        )
    return problems


def _check_gate_order(commands: list[str], label: str) -> list[str]:
    """Check that all ordered gates are present and in the correct order."""
    problems: list[str] = []
    positions = _find_gate_order(commands, ORDERED_GATE_SCRIPTS)

    for gate in ORDERED_GATE_SCRIPTS:
        if positions[gate] < 0:
            problems.append(f"{label} missing gate: {gate}")

    found_gates = [
        (gate, positions[gate])
        for gate in ORDERED_GATE_SCRIPTS
        if positions[gate] >= 0
    ]
    for i in range(len(found_gates) - 1):
        if found_gates[i][1] >= found_gates[i + 1][1]:
            problems.append(
                f"{label} order: {found_gates[i][0]} "
                f"(pos {found_gates[i][1]}) must precede "
                f"{found_gates[i + 1][0]} (pos {found_gates[i + 1][1]})"
            )
    return problems


def _check_provisioning(commands: list[str], label: str) -> list[str]:
    """Require exact frozen provisioning before the first gate."""
    problems: list[str] = []
    provision_pos = -1
    for i, cmd in enumerate(commands):
        argv, err = _tokenize(cmd)
        if err:
            continue
        if argv == list(PROVISIONING_ARGV):
            provision_pos = i
            break
    if provision_pos < 0:
        problems.append(
            f"{label} missing provisioning: "
            f"expected exact command {' '.join(PROVISIONING_ARGV)}"
        )
        return problems
    # Provisioning must precede the first gate
    positions = _find_gate_order(commands, ORDERED_GATE_SCRIPTS)
    first_gate_pos = min(
        (p for p in positions.values() if p >= 0), default=-1
    )
    if first_gate_pos >= 0 and provision_pos > first_gate_pos:
        problems.append(
            f"{label} provisioning must precede the first gate"
        )
    return problems


def _check_frontend_npm_ci(steps: list[Any], label: str) -> list[str]:
    """Require one frontend ``npm ci`` step before every architecture gate."""
    problems: list[str] = []
    npm_positions: list[int] = []
    gate_positions: list[int] = []
    gate_argv = list(GATE_ARGV.values())
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        run = step.get("run", "")
        argv, err = _tokenize(str(run))
        if err:
            continue
        if argv == ["npm", "ci"]:
            npm_positions.append(index)
            if step.get("working-directory") != "frontend":
                problems.append(
                    f"{label} frontend provisioning must run in frontend/"
                )
        if argv in gate_argv:
            gate_positions.append(index)
    if not npm_positions:
        problems.append(f"{label} missing frontend provisioning: expected 'npm ci'")
        return problems
    if len(npm_positions) != 1:
        problems.append(
            f"{label} frontend provisioning must appear exactly once"
        )
    if gate_positions and npm_positions[0] > min(gate_positions):
        problems.append(
            f"{label} frontend npm ci must precede every architecture gate"
        )
    return problems


def _check_no_duplicate_gates(commands: list[str], label: str) -> list[str]:
    """Check that each gate appears exactly once."""
    problems: list[str] = []
    for gate in ORDERED_GATE_SCRIPTS:
        expected_argv = GATE_ARGV.get(gate)
        if expected_argv is None:
            continue
        count = 0
        for cmd in commands:
            argv, err = _tokenize(cmd)
            if err:
                continue
            if argv == expected_argv:
                count += 1
        if count > 1:
            problems.append(
                f"{label} duplicate gate: {gate} appears {count} times"
            )
    # tool_schemas also exactly once
    ts_argv = GATE_ARGV.get(TOOL_SCHEMAS_SCRIPT)
    if ts_argv is not None:
        count = 0
        for cmd in commands:
            argv, err = _tokenize(cmd)
            if err:
                continue
            if argv == ts_argv:
                count += 1
        if count > 1:
            problems.append(
                f"{label} duplicate gate: {TOOL_SCHEMAS_SCRIPT} appears {count} times"
            )
    return problems


def _check_no_extra_argv(commands: list[str], label: str) -> list[str]:
    """Reject any gate command with arguments beyond its exact argv."""
    problems: list[str] = []
    for cmd in commands:
        actual_argv, err = _tokenize(cmd)
        if err:
            continue
        for gate, expected_argv in GATE_ARGV.items():
            if actual_argv == expected_argv:
                continue  # exact match, fine
            # Check if actual starts with expected but has extra tokens
            if (
                len(actual_argv) > len(expected_argv)
                and actual_argv[:len(expected_argv)] == expected_argv
            ):
                problems.append(
                    f"{label} gate {gate} has extra arguments: "
                    f"expected {expected_argv}, got {actual_argv}"
                )
    return problems


def _check_no_tokenize_errors(commands: list[str], label: str) -> list[str]:
    """Check that no gate command has a tokenization error (comment, unmatched quote)."""
    problems: list[str] = []
    gate_scripts = [g.split()[0] for g in GATE_ARGV]
    for cmd in commands:
        is_gate = any(script in cmd for script in gate_scripts)
        if not is_gate:
            continue
        _, err = _tokenize(cmd)
        if err:
            problems.append(f"{label} gate command tokenization error: {err}")
    return problems


def check_ci(root: Path | None = None) -> dict[str, Any]:
    """Check the CI workflow for the exact ordered gates."""
    if root is None:
        root = REPO_ROOT
    ci_path = root / ".github" / "workflows" / "ci.yml"
    data = _load_yaml(ci_path)
    problems: list[str] = []

    if data is None:
        return {
            "ok": False,
            "problems": ["ci.yml missing or empty"],
            "path": str(ci_path),
        }

    jobs = data.get("jobs", {})
    required = jobs.get("required")
    if not required:
        return {
            "ok": False,
            "problems": ["ci.yml has no 'required' job"],
            "path": str(ci_path.relative_to(root)),
        }

    steps = required.get("steps", [])
    commands, multiline_problems = _extract_run_commands(steps)
    problems.extend(multiline_problems)

    problems.extend(_check_environment_shadowing(data, required, steps, "CI"))
    problems.extend(_check_job_continue_on_error(required, "CI"))
    problems.extend(_check_no_tokenize_errors(commands, "CI"))
    problems.extend(_check_no_bypass(commands))
    problems.extend(_check_continue_on_error(steps))
    problems.extend(_check_node_before_typescript(steps))
    problems.extend(_check_gate_order(commands, "CI"))
    problems.extend(_check_no_duplicate_gates(commands, "CI"))
    problems.extend(_check_no_extra_argv(commands, "CI"))
    problems.extend(_check_provisioning(commands, "CI"))
    problems.extend(_check_frontend_npm_ci(steps, "CI"))

    positions = _find_gate_order(commands, ORDERED_GATE_SCRIPTS)
    problems.extend(_check_tool_schemas_position(commands, positions, "CI"))
    problems.extend(
        check_sealed_ordered_contract(
            root,
            enforced_ordered_commands(ORDERED_GATE_SCRIPTS, TOOL_SCHEMAS_SCRIPT, GATE_ARGV),
        )
    )

    # Check that the advisory job is retained with continue-on-error
    advisory = jobs.get("advisory")
    if not advisory:
        problems.append("CI missing advisory job")
    elif advisory.get("continue-on-error") is not True:
        problems.append("CI advisory job must have continue-on-error: true")

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "path": str(ci_path.relative_to(root)),
    }


def check_release(root: Path | None = None) -> dict[str, Any]:
    """Check the release workflow for the exact ordered gates."""
    if root is None:
        root = REPO_ROOT
    release_path = root / ".github" / "workflows" / "release.yml"
    data = _load_yaml(release_path)
    problems: list[str] = []

    if data is None:
        return {
            "ok": False,
            "problems": ["release.yml missing or empty"],
            "path": str(release_path),
        }

    jobs = data.get("jobs")
    job = jobs.get(RELEASE_JOB) if isinstance(jobs, dict) else None
    if not isinstance(jobs, dict) or set(jobs) != {RELEASE_JOB}:
        found = sorted(jobs) if isinstance(jobs, dict) else type(jobs).__name__
        problems.append(
            f"release jobs must be exactly [{RELEASE_JOB!r}]; found {found}"
        )
    if not isinstance(job, dict):
        return {
            "ok": False,
            "problems": problems + [f"release.yml has no {RELEASE_JOB!r} job"],
            "path": str(release_path.relative_to(root)),
        }

    steps = job.get("steps", [])
    commands, multiline_problems = _extract_run_commands(steps)
    problems.extend(multiline_problems)

    problems.extend(_check_environment_shadowing(data, job, steps, "release"))
    problems.extend(_check_job_continue_on_error(job, "release"))
    problems.extend(_check_no_tokenize_errors(commands, "release"))
    problems.extend(_check_no_bypass(commands))
    problems.extend(_check_continue_on_error(steps))
    problems.extend(_check_node_before_typescript(steps))
    problems.extend(_check_gate_order(commands, "release"))
    problems.extend(_check_no_duplicate_gates(commands, "release"))
    problems.extend(_check_no_extra_argv(commands, "release"))
    problems.extend(_check_provisioning(commands, "release"))
    problems.extend(_check_frontend_npm_ci(steps, "release"))

    positions = _find_gate_order(commands, ORDERED_GATE_SCRIPTS)
    problems.extend(_check_tool_schemas_position(commands, positions, "release"))

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "path": str(release_path.relative_to(root)),
    }


def _collect_precommit_entries(repos: list[Any]) -> list[str]:
    entries: list[str] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        for hook in repo.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            entries.append(hook.get("entry", ""))
    return entries


def _precommit_positions(entries: list[str]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for hook in PRE_COMMIT_HOOK_SCRIPTS:
        positions[hook] = -1
        expected_argv = PRE_COMMIT_ARGV.get(hook)
        if expected_argv is None:
            continue
        for index, entry in enumerate(entries):
            argv, err = _tokenize(entry)
            if not err and argv == expected_argv:
                positions[hook] = index
                break
    return positions


def _precommit_order_problems(positions: dict[str, int]) -> list[str]:
    """Check presence and relative order of all required hooks."""
    problems = [
        f"pre-commit missing hook: {hook}"
        for hook in PRE_COMMIT_HOOK_SCRIPTS
        if positions[hook] < 0
    ]
    found_hooks = [
        (hook, positions[hook])
        for hook in PRE_COMMIT_HOOK_SCRIPTS
        if positions[hook] >= 0
    ]
    for i in range(len(found_hooks) - 1):
        if found_hooks[i][1] >= found_hooks[i + 1][1]:
            problems.append(
                f"pre-commit order: {found_hooks[i][0]} "
                f"(pos {found_hooks[i][1]}) must precede "
                f"{found_hooks[i + 1][0]} (pos {found_hooks[i + 1][1]})"
            )
    return problems


def _precommit_duplicate_problems(entries: list[str]) -> list[str]:
    """Reject duplicate exact required hook commands."""
    problems: list[str] = []
    for hook in PRE_COMMIT_HOOK_SCRIPTS:
        expected_argv = PRE_COMMIT_ARGV.get(hook)
        if expected_argv is None:
            continue
        parsed = [_tokenize(entry) for entry in entries]
        count = sum(not err and argv == expected_argv for argv, err in parsed)
        if count > 1:
            problems.append(
                f"pre-commit duplicate hook: {hook} appears {count} times"
            )
    return problems


def _precommit_entry_problems(entries: list[str]) -> list[str]:
    """Reject malformed entries and required-command prefix/suffix bypasses."""
    problems: list[str] = []
    for entry in entries:
        argv, err = _tokenize(entry)
        if err:
            problems.append(f"pre-commit entry is not exact argv: {err}")
            continue
        for hook, expected_argv in PRE_COMMIT_ARGV.items():
            if argv == expected_argv:
                continue
            if (
                len(argv) > len(expected_argv)
                and argv[:len(expected_argv)] == expected_argv
            ):
                problems.append(
                    f"pre-commit hook {hook} has extra arguments: "
                    f"expected {expected_argv}, got {argv}"
                )
    return problems


def _precommit_stage_problems(data: dict[str, Any], repos: list[Any]) -> list[str]:
    """Require governance hooks to remain active during ordinary pre-commit."""
    problems: list[str] = []
    default_stages = data.get("default_stages")
    if default_stages is not None and (
        not isinstance(default_stages, list)
        or "pre-commit" not in default_stages
    ):
        problems.append(
            "pre-commit default_stages must include pre-commit"
        )
    required_argv = list(PRE_COMMIT_ARGV.values())
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        for hook in repo.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            argv, error = _tokenize(str(hook.get("entry", "")))
            if error or argv not in required_argv:
                continue
            stages = hook.get("stages")
            if stages is not None and (
                not isinstance(stages, list)
                or "pre-commit" not in stages
            ):
                problems.append(
                    f"pre-commit required hook {hook.get('id', '?')} "
                    "is manual-only or excludes the pre-commit stage"
                )
    return problems


def check_precommit(root: Path | None = None) -> dict[str, Any]:
    """Check exact pre-commit commands, uniqueness, and structural order."""
    if root is None:
        root = REPO_ROOT
    pc_path = root / ".pre-commit-config.yaml"
    data = _load_yaml(pc_path)
    if data is None:
        return {
            "ok": False,
            "problems": [".pre-commit-config.yaml missing or empty"],
            "path": str(pc_path),
        }
    repos = data.get("repos", [])
    if not repos:
        return {
            "ok": False,
            "problems": [".pre-commit-config.yaml has no repos"],
            "path": str(pc_path.relative_to(root)),
        }
    entries = _collect_precommit_entries(repos)
    positions = _precommit_positions(entries)
    problems = _precommit_order_problems(positions)
    problems.extend(_precommit_duplicate_problems(entries))
    problems.extend(_precommit_entry_problems(entries))
    problems.extend(_precommit_stage_problems(data, repos))

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "path": str(pc_path.relative_to(root)),
    }


def check_ci_contract(root: Path | None = None) -> dict[str, Any]:
    """Run the full CI/release/pre-commit contract check."""
    ci = check_ci(root)
    release = check_release(root)
    precommit = check_precommit(root)

    problems = ci["problems"] + release["problems"] + precommit["problems"]

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "ci_ok": ci["ok"],
        "release_ok": release["ok"],
        "precommit_ok": precommit["ok"],
    }
