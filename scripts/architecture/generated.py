"""Fail-closed proof for generated/declarative budget exceptions."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .policy import REPO_ROOT, load_json

MAX_WRAPPER_LINES = 300
TEMP_OUTPUT_PLACEHOLDER = "{TEMP_OUTPUT}"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PACKAGE = re.compile(r"PKG-\d{2}-[A-Z0-9-]+")
_SHELL_TOKENS = ("&&", "||", ";", "|", "$(", "`", ">", "<")
_URL_PREFIXES = ("http://", "https://", "ws://", "wss://")
_FORBIDDEN_DECLARATIVE_KEYS = {
    "branch",
    "branch_selector",
    "exec",
    "executable",
    "python_class",
    "python_module",
    "service",
    "service_factory",
    "service_locator",
}
REQUIRED_FIELDS = frozenset(
    {
        "id",
        "generator_path",
        "regeneration_command",
        "expected_sha256",
        "content_scan_policy",
        "owner_package",
        "regeneration_test",
        "output_path",
        "temp_output_placeholder",
        "source_inputs",
        "parser_compiler_versions",
        "removal_duty",
        "regeneration_test_node_id",
    }
)


def load_generated(root: Path | None = None) -> dict[str, Any]:
    """Load only the requested root's generated registry."""
    resolved_root = REPO_ROOT if root is None else root
    return load_json(resolved_root / "architecture" / "generated.json")


def _entry_id(entry: dict[str, Any]) -> str:
    return str(entry.get("id", "?"))


def _safe_relative(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return None
    return value


def _is_git_tracked(rel: str, root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", rel],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


def _check_tracked_path(
    entry: dict[str, Any],
    root: Path,
    field: str,
    label: str,
) -> str | None:
    rel = _safe_relative(entry.get(field))
    if rel is None:
        return f"generated entry {_entry_id(entry)} has invalid {field}"
    full = root / rel
    if not full.is_file() or not _is_git_tracked(rel, root):
        return f"generated entry {_entry_id(entry)} {label} {rel} is not Git-tracked"
    return None


def _check_tracked_generator(entry: dict[str, Any], root: Path) -> str | None:
    problem = _check_tracked_path(entry, root, "generator_path", "generator")
    if problem:
        return problem
    generator = root / entry["generator_path"]
    try:
        lines = len(generator.read_text(encoding="utf-8").splitlines())
    except UnicodeDecodeError:
        return f"generated entry {_entry_id(entry)} generator is not UTF-8"
    if lines > MAX_WRAPPER_LINES:
        return (
            f"generated entry {_entry_id(entry)} generator is an oversized wrapper "
            f"({lines} > {MAX_WRAPPER_LINES})"
        )
    return None


def _command_runtime(
    command: list[str], generator_path: Any
) -> tuple[str | None, str | None]:
    if command[0] == "python":
        required_version = "python"
        script_index = 1
    elif command[:3] == ["uv", "run", "python"]:
        required_version = "python"
        script_index = 3
    elif command[0] == "node":
        required_version = "node"
        script_index = 1
    else:
        return None, "command must start with python, uv run python, or node"
    if len(command) <= script_index or command[script_index] != generator_path:
        return None, "command must invoke its exact generator_path"
    return required_version, None


def _command_safety(command: list[str]) -> str | None:
    joined = " ".join(command)
    if any(token in joined for token in _SHELL_TOKENS):
        return "command contains shell syntax"
    if any(token.startswith(_URL_PREFIXES) for token in command):
        return "command injects a service URL"
    if any(
        token in {"--branch", "--git-branch", "--ref", "-b"} for token in command
    ):
        return "command injects a branch/ref"
    return None


def _command_tokens(
    entry: dict[str, Any],
) -> tuple[list[str], str | None, str | None]:
    command = entry.get("regeneration_command")
    prefix = f"generated entry {_entry_id(entry)} "
    if not isinstance(command, list) or not command:
        return [], None, (
            f"{prefix}regeneration_command must be a non-empty exact argv "
            "array; strings are forbidden"
        )
    if not all(isinstance(token, str) and token for token in command):
        return [], None, f"{prefix}argv tokens must be strings"
    required_version, problem = _command_runtime(
        command, entry.get("generator_path")
    )
    if problem:
        return [], None, prefix + problem
    problem = _command_safety(command)
    if problem:
        return [], None, prefix + problem
    placeholder = entry.get("temp_output_placeholder")
    if placeholder != TEMP_OUTPUT_PLACEHOLDER:
        return [], None, (
            f"{prefix}temp_output_placeholder must be "
            f"{TEMP_OUTPUT_PLACEHOLDER!r}"
        )
    if command.count(TEMP_OUTPUT_PLACEHOLDER) != 1:
        return [], None, (
            f"{prefix}command must contain exactly one "
            "temporary-output placeholder token"
        )
    if entry.get("output_path") in command:
        return [], None, f"{prefix}command targets the live output"
    return list(command), required_version, None


def _check_regen_command(entry: dict[str, Any]) -> str | None:
    """Prove an exact argv command with one temp-file placeholder."""
    _, _, problem = _command_tokens(entry)
    return problem


def _check_temp_output_placeholder(entry: dict[str, Any]) -> str | None:
    _, _, problem = _command_tokens(entry)
    return problem


def _check_source_inputs_tracked(
    entry: dict[str, Any], root: Path
) -> str | None:
    inputs = entry.get("source_inputs")
    if (
        not isinstance(inputs, list)
        or not inputs
        or not all(_safe_relative(item) for item in inputs)
        or inputs != sorted(inputs)
        or len(inputs) != len(set(inputs))
    ):
        return (
            f"generated entry {_entry_id(entry)} source_inputs must be a "
            "non-empty sorted unique relative-path list"
        )
    if entry.get("generator_path") not in inputs:
        return f"generated entry {_entry_id(entry)} source_inputs omit generator_path"
    if entry.get("output_path") in inputs:
        return (
            f"generated entry {_entry_id(entry)} source_inputs must not include "
            "the live output"
        )
    for rel in inputs:
        full = root / rel
        if full.is_symlink() or not full.is_file() or not _is_git_tracked(rel, root):
            return f"generated entry {_entry_id(entry)} source input {rel} is not Git-tracked"
    return None


def _actual_versions(root: Path) -> dict[str, str]:
    versions = {"python": platform.python_version()}
    node = subprocess.run(
        ["node", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if node.returncode == 0:
        versions["node"] = node.stdout.strip().removeprefix("v")
    typescript = root / "frontend" / "node_modules" / "typescript" / "package.json"
    if typescript.is_file():
        data = json.loads(typescript.read_text(encoding="utf-8"))
        if isinstance(data.get("version"), str):
            versions["typescript"] = data["version"]
    return versions


def _check_parser_compiler_versions(
    entry: dict[str, Any], root: Path | None = None
) -> str | None:
    resolved_root = REPO_ROOT if root is None else root
    versions = entry.get("parser_compiler_versions")
    if (
        not isinstance(versions, dict)
        or not versions
        or not all(
            isinstance(name, str)
            and isinstance(version, str)
            and version
            for name, version in versions.items()
        )
    ):
        return f"generated entry {_entry_id(entry)} has invalid parser/compiler versions"
    _, required, command_problem = _command_tokens(entry)
    if command_problem:
        return command_problem
    if required and required not in versions:
        return (
            f"generated entry {_entry_id(entry)} lacks exact {required} "
            "runtime version"
        )
    actual = _actual_versions(resolved_root)
    unknown = sorted(set(versions) - set(actual))
    mismatched = {
        name: {"expected": version, "actual": actual.get(name)}
        for name, version in versions.items()
        if actual.get(name) != version
    }
    if unknown or mismatched:
        return (
            f"generated entry {_entry_id(entry)} parser/compiler version drift: "
            f"unknown={unknown}, mismatched={mismatched}"
        )
    return None


def _check_removal_duty(entry: dict[str, Any]) -> str | None:
    owner = entry.get("owner_package")
    duty = entry.get("removal_duty")
    if not isinstance(owner, str) or not _PACKAGE.fullmatch(owner):
        return f"generated entry {_entry_id(entry)} has invalid owner_package"
    if not isinstance(duty, str) or not duty.strip():
        return f"generated entry {_entry_id(entry)} has no removal_duty"
    return None


def _check_owner(entry: dict[str, Any]) -> str | None:
    return _check_removal_duty(entry)


def _check_regen_test(entry: dict[str, Any], root: Path) -> str | None:
    return _check_tracked_path(
        entry, root, "regeneration_test", "regeneration test"
    )


def _check_regen_test_node_id(
    entry: dict[str, Any], root: Path
) -> str | None:
    node_id = entry.get("regeneration_test_node_id")
    test_path = entry.get("regeneration_test")
    if (
        not isinstance(node_id, str)
        or not node_id.startswith(f"{test_path}::")
        or any(char in node_id for char in "*?")
    ):
        return f"generated entry {_entry_id(entry)} has invalid regeneration test node ID"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env["PYTEST_ADDOPTS"] = ""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "--collect-only",
                "-q",
                str(test_path),
            ],
            capture_output=True,
            text=True,
            cwd=root,
            env=env,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"generated entry {_entry_id(entry)} regeneration test collection failed: {exc}"
    collected = {
        line.strip()
        for line in result.stdout.splitlines()
        if "::" in line
    }
    if result.returncode or node_id not in collected:
        return (
            f"generated entry {_entry_id(entry)} regeneration test node is not "
            f"collected exactly: {node_id}"
        )
    return None


def _walk_declarative(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_DECLARATIVE_KEYS:
                return str(key)
            found = _walk_declarative(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _walk_declarative(nested)
            if found:
                return found
    return None


def _read_declarative(full: Path, family: str) -> Any:
    content = full.read_text(encoding="utf-8")
    if family == "declarative_json":
        return json.loads(content)
    if family == "declarative_yaml":
        from .yaml_parser import parse_yaml

        return parse_yaml(content)
    raise ValueError(f"unsupported declarative family: {family}")


def _content_authority(
    entry: dict[str, Any], root: Path
) -> tuple[dict[str, Any], Path, str | None]:
    policy = entry.get("content_scan_policy")
    output = _safe_relative(entry.get("output_path"))
    if not isinstance(policy, dict) or output is None:
        return {}, root, (
            f"generated entry {_entry_id(entry)} has invalid content-scan authority"
        )
    full = root / output
    if not full.is_file():
        return {}, full, f"generated entry {_entry_id(entry)} output is missing: {output}"
    markers = policy.get("required_markers")
    if not isinstance(markers, list) or not all(
        isinstance(marker, str) and marker for marker in markers
    ):
        return {}, full, (
            f"generated entry {_entry_id(entry)} required_markers must be a string list"
        )
    return policy, full, None


def _check_declarative_content(
    entry: dict[str, Any], full: Path, family: str
) -> str | None:
    try:
        parsed = _read_declarative(full, family)
    except (json.JSONDecodeError, ValueError) as exc:
        return f"generated entry {_entry_id(entry)} declarative parse failed: {exc}"
    injected = _walk_declarative(parsed)
    if injected:
        return (
            f"generated entry {_entry_id(entry)} declarative service/branch "
            f"injection key: {injected}"
        )
    return None


def _python_node_location(node: ast.AST) -> str:
    return f"{type(node).__name__} at line {getattr(node, 'lineno', '?')}"


def _literal_data_problem(value: ast.expr) -> str | None:
    """Use Python's structural literal evaluator without its empty-set call."""
    if any(isinstance(node, ast.Call) for node in ast.walk(value)):
        return _python_node_location(value)
    try:
        ast.literal_eval(value)
    except (ValueError, TypeError, MemoryError, RecursionError):
        return _python_node_location(value)
    else:
        return None


def _class_declaration_problem(statement: ast.ClassDef) -> str | None:
    context = f"class {statement.name}"
    restrictions = (
        (statement.decorator_list, "decorators"),
        (statement.bases, "bases"),
        (statement.keywords, "class keywords"),
        (getattr(statement, "type_params", ()), "type parameters"),
    )
    for values, label in restrictions:
        if values:
            return f"{context} has {label} at line {statement.lineno}"
    return _declaration_body_problem(statement.body, context=context)


def _declaration_statement_problem(
    statement: ast.stmt,
    *,
    context: str,
    leading: bool,
) -> str | None:
    if isinstance(statement, ast.Expr):
        if (
            leading
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            return None
        return (
            f"{context} expression is not a leading docstring: "
            f"{_python_node_location(statement.value)}"
        )
    if isinstance(statement, ast.Assign):
        if not statement.targets or any(
            not isinstance(target, ast.Name) for target in statement.targets
        ):
            return f"{context} assignment has a non-name target at line {statement.lineno}"
        problem = _literal_data_problem(statement.value)
        return (
            f"{context} assignment is not literal data: {problem}"
            if problem
            else None
        )
    if isinstance(statement, ast.ClassDef):
        return _class_declaration_problem(statement)
    if isinstance(statement, ast.Pass):
        return None
    return (
        f"{context} contains a forbidden statement: "
        f"{_python_node_location(statement)}"
    )


def _declaration_body_problem(
    body: list[ast.stmt], *, context: str
) -> str | None:
    """Validate a module or class body against the declaration allowlist."""
    for index, statement in enumerate(body):
        problem = _declaration_statement_problem(
            statement, context=context, leading=index == 0
        )
        if problem:
            return problem
    return None


def _check_generated_python_content(
    entry: dict[str, Any], content: str
) -> str | None:
    """Require generated Python to contain declarations and literal data only."""
    try:
        tree = ast.parse(content, filename=entry["output_path"])
    except SyntaxError as exc:
        return f"generated entry {_entry_id(entry)} generated Python parse failed: {exc}"
    problem = _declaration_body_problem(tree.body, context="module")
    if problem:
        return (
            f"generated entry {_entry_id(entry)} generated Python is not "
            f"declaration-only: {problem}"
        )
    return None


def _check_content_scan(entry: dict[str, Any], root: Path) -> str | None:
    policy, full, problem = _content_authority(entry, root)
    if problem:
        return problem
    family = policy.get("family")
    markers = policy["required_markers"]
    content = full.read_text(encoding="utf-8")
    if any(marker not in content for marker in markers):
        return f"generated entry {_entry_id(entry)} content marker is missing"
    if family in {"declarative_json", "declarative_yaml"}:
        return _check_declarative_content(entry, full, family)
    if family == "generated_python":
        return _check_generated_python_content(entry, content)
    elif family != "generated_markdown":
        return f"generated entry {_entry_id(entry)} unknown content family: {family}"
    return None


def _live_output(
    entry: dict[str, Any], root: Path
) -> tuple[Path, bytes, str | None]:
    expected = entry.get("expected_sha256")
    output = _safe_relative(entry.get("output_path"))
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        return root, b"", f"generated entry {_entry_id(entry)} has invalid expected_sha256"
    if output is None:
        return root, b"", f"generated entry {_entry_id(entry)} has invalid output_path"
    live = root / output
    if not live.is_file() or not _is_git_tracked(output, root):
        return live, b"", (
            f"generated entry {_entry_id(entry)} output {output} is not Git-tracked"
        )
    live_bytes = live.read_bytes()
    if hashlib.sha256(live_bytes).hexdigest() != expected:
        return live, live_bytes, (
            f"generated entry {_entry_id(entry)} tracked output hash drift"
        )
    return live, live_bytes, None


def _snapshot_workspace(workspace: Path) -> dict[str, bytes]:
    """Return exact non-symlink file bytes for mutation detection."""
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _stage_source_inputs(
    entry: dict[str, Any], root: Path, workspace: Path
) -> str | None:
    """Copy only declared inputs into the isolated regeneration workspace."""
    inputs = entry.get("source_inputs")
    if not isinstance(inputs, list):
        return f"generated entry {_entry_id(entry)} has invalid source_inputs"
    for value in inputs:
        rel = _safe_relative(value)
        if rel is None or rel == entry.get("output_path"):
            return (
                f"generated entry {_entry_id(entry)} has invalid isolated "
                f"source input: {value}"
            )
        source = root / rel
        if source.is_symlink() or not source.is_file():
            return f"generated entry {_entry_id(entry)} source input is unavailable: {rel}"
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return None


def _isolated_argv(tokens: list[str], temp_output: Path) -> list[str]:
    """Resolve the declared runtime before entering the minimal child environment."""
    argv = [
        str(temp_output) if token == TEMP_OUTPUT_PLACEHOLDER else token
        for token in tokens
    ]
    if argv[0] == "python":
        runtime = sys.executable
    elif argv[:3] == ["uv", "run", "python"]:
        runtime = sys.executable
        argv = [runtime, *argv[3:]]
    else:
        runtime = argv[0]
    found = shutil.which(runtime)
    if found is None:
        raise FileNotFoundError(f"runtime executable is unavailable: {runtime}")
    resolved = Path(os.path.abspath(found))
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PermissionError(f"runtime is not executable: {resolved}")
    argv[0] = str(resolved)
    return argv


def _isolated_environment(workspace: Path) -> dict[str, str]:
    """Return the complete deterministic environment exposed to a generator."""
    isolated_path = str(workspace)
    return {
        "HOME": isolated_path,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "",
        "PWD": isolated_path,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": isolated_path,
    }


def _run_isolated_regeneration(
    entry: dict[str, Any],
    root: Path,
    live_bytes: bytes,
    *,
    poison_live_name: bool,
) -> str | None:
    tokens, _, problem = _command_tokens(entry)
    if problem:
        return problem
    with tempfile.TemporaryDirectory(prefix="arch-regen-") as temp_dir:
        isolated_root = Path(temp_dir)
        workspace = isolated_root / "workspace"
        workspace.mkdir()
        problem = _stage_source_inputs(entry, root, workspace)
        if problem:
            return problem
        if poison_live_name:
            output = _safe_relative(entry.get("output_path"))
            if output is None:
                return f"generated entry {_entry_id(entry)} has invalid output_path"
            poison = workspace / output
            poison.parent.mkdir(parents=True, exist_ok=True)
            poison.write_bytes(b"DISCLAUDE_POISONED_LIVE_OUTPUT\n")
        before = _snapshot_workspace(workspace)
        result_dir = isolated_root / "result"
        result_dir.mkdir()
        temp_output = result_dir / "generated-output"
        try:
            argv = _isolated_argv(tokens, temp_output)
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=workspace,
                env=_isolated_environment(workspace),
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"generated entry {_entry_id(entry)} regeneration failed: {exc}"
        if result.returncode:
            return (
                f"generated entry {_entry_id(entry)} regeneration failed "
                f"(exit {result.returncode}): {result.stderr[-200:]}"
            )
        if _snapshot_workspace(workspace) != before:
            return (
                f"generated entry {_entry_id(entry)} regeneration wrote outside "
                "the temporary output"
            )
        if not temp_output.is_file():
            return f"generated entry {_entry_id(entry)} temp output was not created"
        regenerated = temp_output.read_bytes()
        if regenerated != live_bytes:
            return f"generated entry {_entry_id(entry)} regenerated bytes drift"
    return None


def _run_regeneration(
    entry: dict[str, Any],
    root: Path,
    live: Path,
    live_bytes: bytes,
) -> str | None:
    """Regenerate twice without exposing the registered live output as an input."""
    attempts = (
        (
            "live output absent",
            _run_isolated_regeneration(
                entry, root, live_bytes, poison_live_name=False
            ),
        ),
        (
            "live output poisoned",
            _run_isolated_regeneration(
                entry, root, live_bytes, poison_live_name=True
            ),
        ),
    )
    if live.read_bytes() != live_bytes:
        return f"generated entry {_entry_id(entry)} regeneration mutated live output"
    for label, problem in attempts:
        if problem:
            return (
                f"generated entry {_entry_id(entry)} isolated regeneration with "
                f"{label} failed: {problem}"
            )
    return None


def _check_regen_bytes(entry: dict[str, Any], root: Path) -> str | None:
    live, live_bytes, problem = _live_output(entry, root)
    if problem:
        return problem
    return _run_regeneration(entry, root, live, live_bytes)


def _check_entry(entry: dict[str, Any], root: Path) -> list[str]:
    missing = REQUIRED_FIELDS - set(entry)
    if missing:
        return [
            f"generated entry {_entry_id(entry)} missing proof fields: {sorted(missing)}"
        ]
    checks = (
        _check_tracked_generator(entry, root),
        _check_regen_command(entry),
        _check_source_inputs_tracked(entry, root),
        _check_parser_compiler_versions(entry, root),
        _check_regen_bytes(entry, root),
        _check_content_scan(entry, root),
        _check_removal_duty(entry),
        _check_regen_test(entry, root),
        _check_regen_test_node_id(entry, root),
    )
    return [problem for problem in checks if problem]


def _validate_registry_entries(
    registry: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    problems: list[str] = []
    entries = registry.get("entries")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) for entry in entries
    ):
        return [], ["generated entries must be an object list"]
    ids = [entry.get("id") for entry in entries]
    outputs = [entry.get("output_path") for entry in entries]
    if any(not isinstance(item, str) or not item for item in ids):
        problems.append("generated entry IDs must be non-empty strings")
    elif len(ids) != len(set(ids)):
        problems.append("generated entry IDs must be unique")
    if any(not isinstance(item, str) or not item for item in outputs):
        problems.append("generated output paths must be non-empty strings")
    elif len(outputs) != len(set(outputs)):
        problems.append("generated output paths must be unique")
    return entries, problems


def check_generated(root: Path | None = None) -> dict[str, Any]:
    """Verify all six proof facts for every registered exception."""
    resolved_root = REPO_ROOT if root is None else root
    registry = load_generated(resolved_root)
    problems: list[str] = []
    if registry.get("schema") != "disclaude-architecture-generated-proof-v1":
        problems.append("generated registry schema mismatch")
    entries, registry_problems = _validate_registry_entries(registry)
    problems.extend(registry_problems)
    for entry in entries:
        problems.extend(_check_entry(entry, resolved_root))
    return {
        "ok": not problems,
        "problems": problems,
        "entry_count": len(entries),
    }
