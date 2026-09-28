#!/usr/bin/env python3
"""Read a candidate change as Git data; invoke this file from a trusted base only."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

SENSITIVE_PREFIXES = (
    ".github/workflows/", ".github/actions/", ".claude/", "development/scripts/", "development/governance/",
    "development/architecture/", "development/harness/", "development/changes/", "deploy/",
    "frontend/src/test/", "frontend/vite.config.", "frontend/playwright",
)
SENSITIVE_FILES = {
    "pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "Makefile",
    ".github/CODEOWNERS", ".github/PULL_REQUEST_TEMPLATE.md", "CODEOWNERS", ".importlinter", "uv.lock",
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


def body_record(body):
    require(isinstance(body, str) and len(body.encode("utf-8")) <= 65536,
            "PR body must be bounded text")
    lines = body.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "```disco-change"]
    require(len(starts) == 1, "Exactly one disco-change JSON block is required")
    start = starts[0] + 1
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == "```"), None)
    require(end is not None, "Unclosed disco-change block")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads("\n".join(lines[start:end]), object_pairs_hook=unique)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise Invalid("Invalid record JSON") from error


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
            or PurePosixPath(filename).name == "conftest.py"
            or filename.startswith(("frontend/tsconfig", "frontend/eslint.config")))


def validate(repo, base, head, body):
    sha(base); sha(head)
    ancestor(repo, base, head)
    delta = changed(repo, base, head)
    require(delta, "Candidate has no changes")
    data = body_record(body)
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
    require(tested == head, "Stale evidence: tested commit must equal the exact PR head")
    require(git(repo, "rev-parse", tested + "^{tree}").decode().strip() == evidence["tree"],
            "Evidence tree does not match its commit")
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
    review = sorted(p for p in delta if sensitive(p, p in existing))
    return {"base": base, "head": head, "record_source": "pull_request.body",
            "changed_paths": sorted(delta),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "evidence_commit": tested, "requires_independent_review": review,
            "status": "requires_independent_review" if review else "scope_consistent",
            "limitation": "Validates declarations and Git binding, not execution authenticity, review approval or quality."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "base", "head"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--event", required=True, help="Trusted runner event JSON; PR body is data")
    args = parser.parse_args()
    try:
        event_bytes = Path(args.event).read_bytes()
        require(len(event_bytes) <= 2 * 1024 * 1024, "Event payload exceeds 2MiB")
        event = json.loads(event_bytes)
        require(isinstance(event, dict) and isinstance(event.get("pull_request"), dict),
                "Missing pull_request event")
        result = validate(args.repo, args.base, args.head, event["pull_request"].get("body"))
    except (Invalid, subprocess.TimeoutExpired, UnicodeError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}))
        return 1
    print(json.dumps(result, indent=2))
    return 2 if result["requires_independent_review"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
