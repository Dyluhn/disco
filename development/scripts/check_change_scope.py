#!/usr/bin/env python3
"""Read a candidate change as Git data; invoke this file from a trusted base only."""

import argparse
import hashlib
import json
from pathlib import PurePosixPath
import re
import subprocess

RECORD_PREFIX = "development/changes/"
SENSITIVE_PREFIXES = (
    ".github/workflows/", ".github/actions/", ".claude/", "development/scripts/", "development/governance/",
    "development/architecture/", "development/harness/", "deploy/",
    "frontend/src/test/", "frontend/vite.config.", "frontend/playwright",
)
SENSITIVE_FILES = {
    "pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "Makefile",
    ".github/CODEOWNERS", "CODEOWNERS", ".importlinter", "uv.lock",
    "frontend/e2e-full/full.config.ts", "frontend/live-smoke.config.ts",
    "pytest.ini", "tox.ini", "setup.cfg", "ruff.toml", ".ruff.toml", ".pre-commit-config.yaml",
}
FIELDS = {"version", "id", "issue", "purpose", "reproduction", "base", "paths", "risks",
          "provider", "evidence", "ui", "quality"}
RISK_FIELDS = {"context_limits", "budgets", "retries", "fallbacks", "success_criteria", "compatibility"}
SHA = re.compile(r"[0-9a-f]{40}\Z")


class Invalid(ValueError):
    """Untrusted candidate data failed validation."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def git(repo, *args):
    result = subprocess.run(
        ["git", "--no-pager", "-C", str(repo), *args], capture_output=True, check=False,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
        timeout=30,
    )
    require(result.returncode == 0, "Git object operation failed")
    return result.stdout


def sha(value):
    require(isinstance(value, str) and SHA.fullmatch(value), "Expected a full commit SHA")
    return value


def text(value, field):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 12000,
            f"Missing or invalid {field}")


def path(value):
    require(isinstance(value, str) and 0 < len(value) <= 500, "Invalid declared path")
    p = PurePosixPath(value)
    require(not p.is_absolute() and str(p) == value and ".." not in p.parts
            and ".git" not in p.parts and not any(ord(c) < 32 or c in "*?[]" for c in value),
            "Paths must be exact repository-relative files")
    return value


def fields(value, expected, name):
    require(isinstance(value, dict) and set(value) == expected, f"Invalid {name} fields")


def blob(repo, commit, filename):
    entry = git(repo, "ls-tree", "-z", commit, "--", filename).split(b"\0")[0]
    require(entry.startswith((b"100644 blob ", b"100755 blob ")), "Expected a regular Git file")
    data = git(repo, "show", f"{commit}:{filename}")
    require(len(data) <= 65536, "Change record exceeds 64KiB")
    return data


def changed(repo, before, after):
    raw = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
              "--name-only", "-z", before, after)
    return {x.decode("utf-8", errors="strict") for x in raw.split(b"\0") if x}


def ancestor(repo, before, after):
    git(repo, "merge-base", "--is-ancestor", before, after)


def sensitive(filename, existed):
    # Existing test edits require independent scrutiny: lexical heuristics cannot
    # distinguish a useful strengthening from removal of assertions/exclusions.
    parts = PurePosixPath(filename).parts
    test = "tests" in parts or filename.endswith((".test.ts", ".test.tsx", ".spec.ts"))
    return (filename.startswith(SENSITIVE_PREFIXES) or filename in SENSITIVE_FILES
            or (existed and test) or "pytest" in filename or "vitest.config" in filename
            or filename.startswith(RECORD_PREFIX) or PurePosixPath(filename).name == "conftest.py"
            or filename.startswith(("frontend/tsconfig", "frontend/eslint.config")))


def validate(repo, base, head, record=None):
    sha(base); sha(head)
    ancestor(repo, base, head)
    delta = changed(repo, base, head)
    records = {p for p in delta if p.startswith(RECORD_PREFIX) and p.endswith(".json")}
    require(len(records) == 1, "Exactly one changed JSON change record is required")
    record = record if record is not None else next(iter(records))
    path(record)
    require(records == {record}, "Wrong change record selected")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "Duplicate JSON key")
                result[key] = value
            return result
        data = json.loads(blob(repo, head, record), object_pairs_hook=unique)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise Invalid("Invalid record JSON") from error
    fields(data, FIELDS, "record")
    require(type(data["version"]) is int and data["version"] == 1, "Unsupported record version")
    for key in ("id", "issue", "purpose", "reproduction"):
        text(data[key], key)
    require(data["base"] == base, "Record base does not match the current PR base")
    declared = data["paths"]
    require(isinstance(declared, list) and declared, "Declare exact changed paths")
    declared = [path(p) for p in declared]
    require(len(declared) == len(set(declared)), "Duplicate declared path")
    require(set(declared) == delta, "Declared paths do not equal the actual base/head diff")
    fields(data["risks"], RISK_FIELDS, "risk")
    for key, value in data["risks"].items():
        text(value, "risk " + key)
    provider = data["provider"]
    fields(provider, {"applicable", "reason", "variant_tests"}, "provider")
    require(type(provider["applicable"]) is bool, "Provider applicability must be boolean")
    text(provider["reason"], "provider reason")
    require(isinstance(provider["variant_tests"], list), "Variant tests must be a list")
    provider_paths = ("packages/core/src/disco/core/llm/",
                      "packages/retrieval/src/disco/retrieval/deep_research/")
    require(provider["applicable"] or not any(p.startswith(provider_paths) for p in delta),
            "LLM/research paths require provider applicability and variant-test evidence")
    if provider["applicable"]:
        require(provider["variant_tests"], "Provider-affecting changes need variant-test evidence")
    for test in provider["variant_tests"]:
        text(test, "variant test")
    evidence = data["evidence"]
    fields(evidence, {"commit", "tree", "checks"}, "evidence")
    tested = sha(evidence["commit"])
    sha(evidence["tree"])
    ancestor(repo, base, tested); ancestor(repo, tested, head)
    require(git(repo, "rev-parse", tested + "^{tree}").decode().strip() == evidence["tree"],
            "Evidence tree does not match its commit")
    require(changed(repo, tested, head) <= {record}, "Stale evidence: source changed after tested commit")
    checks = evidence["checks"]
    require(isinstance(checks, list) and checks, "Execution checks are required")
    executed_tests = set()
    for check in checks:
        fields(check, {"command", "exit", "artifact_sha256", "tests"}, "check")
        text(check["command"], "check command")
        require(type(check["exit"]) is int and check["exit"] == 0, "Required check did not pass")
        require(isinstance(check["artifact_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", check["artifact_sha256"]), "Invalid artifact hash")
        require(isinstance(check["tests"], list), "Check tests must be a list")
        for test in check["tests"]:
            text(test, "executed test")
            executed_tests.add(test)
    require(set(provider["variant_tests"]) <= executed_tests, "Variant tests lack execution evidence")
    for field in ("ui", "quality"):
        fields(data[field], {"status", "evidence"}, field)
        require(data[field]["status"] in {"not_applicable", "untested", "failed", "passed_scoped"},
                "Invalid manual evidence status")
        text(data[field]["evidence"], field + " evidence")
    existing = {p.decode() for p in git(repo, "ls-tree", "-r", "--name-only", "-z", base).split(b"\0") if p}
    review = sorted(p for p in delta - {record} if sensitive(p, p in existing))
    return {"base": base, "head": head, "record": record, "changed_paths": sorted(delta),
            "record_sha256": hashlib.sha256(blob(repo, head, record)).hexdigest(),
            "evidence_commit": tested, "requires_independent_review": review,
            "status": "requires_independent_review" if review else "scope_consistent",
            "limitation": "Validates declarations and Git binding, not execution authenticity, review approval or quality."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "base", "head"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--record")
    args = parser.parse_args()
    try:
        result = validate(args.repo, args.base, args.head, args.record)
    except (Invalid, subprocess.TimeoutExpired, UnicodeError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}))
        return 1
    print(json.dumps(result, indent=2))
    return 2 if result["requires_independent_review"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
