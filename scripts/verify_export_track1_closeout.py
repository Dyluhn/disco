#!/usr/bin/env python3
"""verify_export_track1_closeout — the evidence-producing, non-bypass acceptance
verifier for the Export Track-1 Closeout (WO-C0, plan §1.2–§1.4 / §3 / §4).

It runs the campaign gates from a CLEAN checkout of one exact candidate SHA and
writes a machine-readable evidence directory OUTSIDE the checkout (CI uploads it as
an artifact on success AND failure). Exit code — never summary text — is truth.

Lanes (all wired; each is honestly recorded, none is ``not_wired_yet``):

  * ``python-nonlive`` — the full non-integration suite (plan §3.2, first command).
  * ``python-closeout`` — the focused closeout lane (plan §3.2, second command).
    JUnit is parsed and ANY nonzero failed/errors/skipped (xfail/xpass surface as
    those) is rejected; a fresh ``--collect-only`` node-ID inventory is compared with
    the FROZEN inventory read FROM THE MANIFEST JSON, so changing pytest discovery
    cannot hide a test.
  * ``frontend`` — from ``frontend/``: the frozen closeout vitest (``--reporter=json``),
    ``npm run typecheck:build``, and ``npx vite build`` (plan §3.3). Green iff vitest
    has zero failed/pending/todo AND both typecheck and build exit 0 AND the discovered
    test-file set equals the frozen ``frontend_closeout_inventory``. The Playwright
    candidate/needs_review/not_web e2e proofs cannot run offline; they are frozen and
    RECORDED honestly, never faked, and never block ``passed`` here (see LIMITATION).
  * ``live-docker`` — the live clean-room lane (plan §3.4). On a host WITHOUT a real
    Docker engine the tests FAIL (never skip). Green iff exit 0 with zero
    failed/errors/skipped.
  * ``anti-bypass-scan`` — a real AST/lexical scan (plan §4.4 / §4.9) proving the
    frozen tests seam the system only at the allowlisted config/env seams and that
    production source carries no closeout test-switch. Green iff zero violations.

Non-bypass guarantees preserved: it refuses a dirty checkout UNLESS ``--author``;
``--author`` can NEVER emit ``passed: true``; the frozen-file SHA-256 manifest is a
byte-fresh tamper check; ``evidence.json`` is written outside the checkout.
``passed: true`` requires a clean, non-author run where the frozen manifest verified
and EVERY lane is green.

Usage:
    uv run python scripts/verify_export_track1_closeout.py --all \\
        --evidence-dir /path/outside/checkout
    uv run python scripts/verify_export_track1_closeout.py --author \\
        --evidence-dir /tmp/evidence            # dirty tree OK; passed always false
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import gen_closeout_acceptance_manifest as manifest_mod

# ---- evidence files a real Docker-host live run produces (plan §1.4). At authoring
# / on a host with no Docker engine they are HONESTLY absent — recorded, never faked.
_DOCKER_HOST_ARTIFACTS = (
    "bundle-digests.json",
    "docker-inspect-sanitized.json",
    "compose-ps.json",
    "cleanup.txt",
)

# ---- anti-bypass scanner: production-source rules (plan §4.9) ------------------
# Closeout-specific tokens that must NEVER appear in production source (any hit is a
# violation): the seed env, the frozen fixture dir names, and the C8 secret/build
# sentinels (values mirrored from _closeout_live_support.py).
_CLOSEOUT_TOKENS: dict[str, str] = {
    "CLOSEOUT_SEED": "closeout_seed_reference",
    "export_track1_closeout": "closeout_fixture_reference",
    "export-track1-closeout": "closeout_fixture_reference",
    "DISCO-C8-RUNTIME-SECRET-9d1f7a3e": "closeout_secret_sentinel",
    "disco-c8-public-banner-6b2c9e": "closeout_build_sentinel",
    "DISCO-C8-BUILD-SECRET-4f8a1d2c": "closeout_build_sentinel",
}
# A test-only success switch that forces a release verdict (plan §4.9).
_FORCE_VERDICT_RE = re.compile(
    r"FORCE_VERIFIED|FORCE_SELF_HOST|FORCE_READY|DISCO_ACCEPTANCE"
    r"|ACCEPTANCE_SENTINEL|CLOSEOUT_FORCE"
)
# ``PYTEST_CURRENT_TEST`` is a general test-mode signal that PREDATES this campaign in
# the auth/workflow layer (NOT the release-verdict path). Those pre-existing sites are
# ratified here by (repo-relative file, stripped source line) so line-number drift as
# C1–C8 add code does not break them; ANY other production reference — especially in
# the release/export subsystem — is a violation. Keyed on text, not line number.
_PYTEST_CURRENT_TEST_BASELINE: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "packages/agent-server/src/disco/agent_server/auth.py",
            'if not os.environ.get("PYTEST_CURRENT_TEST"):',
        ),
        (
            "packages/agent-server/src/disco/agent_server/auth.py",
            'os.environ.get("PYTEST_CURRENT_TEST") and server_host in {"testserver", "test", "t"}',
        ),
        (
            "packages/agent-server/src/disco/agent_server/routes/workflows.py",
            'if os.environ.get("PYTEST_CURRENT_TEST"):',
        ),
        (
            "packages/app-server/src/disco/app_server/auth.py",
            'if not os.environ.get("PYTEST_CURRENT_TEST"):',
        ),
    }
)

# ---- anti-bypass scanner: frozen-test rules (plan §4.4) -----------------------
# Python: mock imports are forbidden entirely; MagicMock/create_autospec are forbidden
# as ANY use; Mock(/patch( are forbidden as calls; mock.patch as an attribute; and
# monkeypatch.setattr is allowed ONLY against cfg_store.load (the config seam).
_PY_MOCK_CALL_NAMES = frozenset({"Mock", "patch"})
_PY_MOCK_ANY_USE_NAMES = frozenset({"MagicMock", "create_autospec"})
_MONKEYPATCH_FIXTURE_NAMES = frozenset({"monkeypatch", "mp"})
# Frontend: module-replacement helpers are forbidden outright.
_FRONTEND_FORBIDDEN = (
    ("vi.mock(", "forbidden_vi_mock"),
    ("vi.spyOn(", "forbidden_vi_spyOn"),
    ("vi.stubGlobal(", "forbidden_vi_stubGlobal"),
    ("vi.stubEnv(", "forbidden_vi_stubEnv"),
    ("jest.mock(", "forbidden_jest_mock"),
    ("jest.spyOn(", "forbidden_jest_spyOn"),
)
_VI_FN_ASSIGN_RE = re.compile(r"(?<![=!<>])=\s*vi\.fn\s*\(")
_DECL_TAIL_RE = re.compile(r"\b(?:const|let|var)\b[^=]*$")
_TS_IMPORT_RE = re.compile(r"""^\s*import\s+(?P<body>.+?)\s+from\s+["']""")


@dataclass
class LaneResult:
    name: str
    status: str  # "ran"
    green: bool | None = None
    exit_code: int | None = None
    junit: dict[str, int] | None = None
    junit_path: str | None = None
    rejected: bool | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    detail: dict[str, object] = field(default_factory=dict)


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a fixed-argv command (no shell), capturing output. Never raises on nonzero
    — the CALLER reads returncode (plan §1.2: exit code captured, not inferred)."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.strip()


def _is_clean(repo: Path) -> bool:
    return _git(repo, "status", "--porcelain") == ""


def _tool_versions(repo: Path) -> dict[str, str]:
    versions: dict[str, str] = {}

    def _probe(key: str, cmd: list[str]) -> None:
        if shutil.which(cmd[0]) is None:
            versions[key] = "unavailable"
            return
        proc = _run(cmd, cwd=repo)
        line = (proc.stdout or proc.stderr).strip().splitlines()
        versions[key] = line[0] if line else "unknown"

    _probe("python", [sys.executable, "--version"])
    _probe("pytest", [sys.executable, "-m", "pytest", "--version"])
    _probe("git", ["git", "--version"])
    _probe("node", ["node", "--version"])
    _probe("npm", ["npm", "--version"])
    _probe("docker", ["docker", "--version"])
    return versions


def _parse_junit(path: Path) -> dict[str, int]:
    """Aggregate counts from a JUnit XML, summed across testsuites."""
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if not path.is_file():
        return counts
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, "0") or "0")
    return counts


# ---- pytest lanes -------------------------------------------------------------


def _run_pytest_lane(
    repo: Path,
    evidence_dir: Path,
    *,
    name: str,
    paths: tuple[str, ...],
    marker: str,
    junit_filename: str,
) -> LaneResult:
    junit_path = evidence_dir / junit_filename
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-o",
        "addopts=",
        "-m",
        marker,
        f"--junitxml={junit_path}",
    ]
    proc = _run(cmd, cwd=repo)
    return LaneResult(
        name=name,
        status="ran",
        exit_code=proc.returncode,
        junit=_parse_junit(junit_path),
        junit_path=str(junit_path),
    )


def _evaluate_closeout(lane: LaneResult, inventory_ok: bool, inventory_note: str) -> None:
    """Apply the closeout-lane rejection rules (plan §1.2/§3.2) in place."""
    reasons: list[str] = []
    if lane.exit_code != 0:
        reasons.append(f"closeout pytest exit code {lane.exit_code} (expected 0)")
    junit = lane.junit or {}
    for bad in ("failures", "errors", "skipped"):
        if junit.get(bad, 0) > 0:
            reasons.append(f"closeout lane has {junit[bad]} {bad} (must be 0)")
    if junit.get("tests", 0) == 0:
        reasons.append("closeout lane collected zero tests")
    if not inventory_ok:
        reasons.append(f"frozen node-ID inventory mismatch: {inventory_note}")
    lane.rejected = bool(reasons)
    lane.rejection_reasons = reasons
    lane.green = not lane.rejected


# ---- frozen manifest tamper check ---------------------------------------------


def _load_manifest(repo: Path) -> tuple[dict[str, object] | None, str]:
    manifest_path = repo / manifest_mod.MANIFEST_REL
    if not manifest_path.is_file():
        return None, "acceptance manifest is absent (run gen_closeout_acceptance_manifest.py)"
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"acceptance manifest is not valid JSON: {exc}"
    if not isinstance(stored, dict):
        return None, "acceptance manifest is not a JSON object"
    return stored, "loaded"


def _verify_frozen_manifest(
    repo: Path, stored: dict[str, object] | None, note: str
) -> tuple[bool, str]:
    if stored is None:
        return False, note
    stored_files = stored.get("files", {})
    if not isinstance(stored_files, dict):
        return False, "manifest 'files' is not a JSON object"
    fresh_files, missing = manifest_mod._iter_frozen_files(repo)
    if missing:
        return False, f"frozen paths missing from reality: {missing[:6]}"
    if stored_files != fresh_files:
        changed = sorted(
            set(stored_files) ^ set(fresh_files)
            | {k for k in set(stored_files) & set(fresh_files) if stored_files[k] != fresh_files[k]}
        )
        return False, f"frozen file hash drift on: {changed[:8]}"
    return True, "frozen files match the manifest"


# ---- frontend lane (plan §3.3) ------------------------------------------------


def _parse_vitest(path: Path) -> tuple[dict[str, int], set[str], bool]:
    """Return ((counts), test-file basenames, parsed-ok). Vitest ``--reporter=json``
    is Jest-shaped: numTotalTests / numFailedTests / numPendingTests / numTodoTests and
    testResults[].name (absolute file path)."""
    counts = {"total": -1, "passed": -1, "failed": -1, "pending": -1, "todo": -1}
    if not path.is_file():
        return counts, set(), False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return counts, set(), False
    if not isinstance(data, dict):
        return counts, set(), False
    counts = {
        "total": int(data.get("numTotalTests", 0)),
        "passed": int(data.get("numPassedTests", 0)),
        "failed": int(data.get("numFailedTests", 0)),
        "pending": int(data.get("numPendingTests", 0)),
        "todo": int(data.get("numTodoTests", 0)),
    }
    files: set[str] = set()
    results = data.get("testResults", [])
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str):
                    files.add(Path(name).name)
    return counts, files, True


def _run_frontend_lane(
    repo: Path, evidence_dir: Path, frozen_inventory: dict[str, object]
) -> LaneResult:
    frontend_dir = repo / "frontend"
    detail: dict[str, object] = {}
    lane = LaneResult(name="frontend", status="ran", detail=detail)

    if shutil.which("npx") is None or shutil.which("npm") is None:
        detail["npx_available"] = False
        detail["note"] = "node/npm/npx unavailable on this host; frontend lane cannot run"
        lane.green = False
        return lane
    detail["npx_available"] = True

    vitest_json = evidence_dir / "frontend-vitest.json"
    vproc = _run(
        [
            "npx",
            "vitest",
            "run",
            manifest_mod.FRONTEND_VITEST_DIR.split("/", 1)[1],
            "--reporter=json",
            f"--outputFile={vitest_json}",
        ],
        cwd=frontend_dir,
    )
    counts, discovered, parsed_ok = _parse_vitest(vitest_json)
    frozen_files = set(frozen_inventory.keys())
    missing_files = sorted(frozen_files - discovered)
    extra_files = sorted(discovered - frozen_files)
    file_set_ok = parsed_ok and not missing_files and not extra_files
    vitest_ok = (
        parsed_ok
        and counts["failed"] == 0
        and counts["pending"] == 0
        and counts["todo"] == 0
        and counts["total"] > 0
    )
    detail["vitest"] = {
        "exit_code": vproc.returncode,
        "counts": counts,
        "parsed_ok": parsed_ok,
        "json": str(vitest_json),
        "file_set_ok": file_set_ok,
        "missing_files": missing_files,
        "extra_files": extra_files,
    }

    tproc = _run(["npm", "run", "typecheck:build"], cwd=frontend_dir)
    detail["typecheck"] = {"exit_code": tproc.returncode}

    bproc = _run(["npx", "vite", "build"], cwd=frontend_dir)
    detail["build"] = {"exit_code": bproc.returncode}

    e2e_dir = repo / manifest_mod.FRONTEND_E2E_DIR
    e2e_specs = sorted(p.name for p in e2e_dir.glob("*.spec.ts")) if e2e_dir.exists() else []
    detail["playwright_e2e"] = {
        "status": "not_executed_offline",
        "reason": (
            "the frozen candidate/needs_review/not_web Firefox proofs need a "
            "fixture-mode server + a browser and cannot run in this offline verifier; "
            "they are frozen (hashed) and do NOT block `passed` here (LIMITATION)"
        ),
        "frozen_specs": e2e_specs,
    }

    lane.green = bool(vitest_ok and file_set_ok and tproc.returncode == 0 and bproc.returncode == 0)
    return lane


# ---- live Docker lane (plan §3.4) ---------------------------------------------


def _write_docker_versions(repo: Path, evidence_dir: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    if shutil.which("docker") is None:
        versions["docker"] = "unavailable"
        versions["compose"] = "unavailable"
    else:
        dproc = _run(["docker", "--version"], cwd=repo)
        versions["docker"] = (dproc.stdout or dproc.stderr).strip() or "unknown"
        cproc = _run(["docker", "compose", "version"], cwd=repo)
        versions["compose"] = (cproc.stdout or cproc.stderr).strip() or "unknown"
    lines = [f"docker: {versions['docker']}", f"compose: {versions['compose']}", ""]
    (evidence_dir / "docker-versions.txt").write_text("\n".join(lines), encoding="utf-8")
    return versions


def _run_live_lane(repo: Path, evidence_dir: Path) -> LaneResult:
    docker_versions = _write_docker_versions(repo, evidence_dir)
    junit_path = evidence_dir / "pytest-live.xml"
    proc = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            manifest_mod.LIVE_TEST_FILE,
            "-o",
            "addopts=",
            "-m",
            manifest_mod.CLOSEOUT_MARKER_LIVE,
            "-ra",
            f"--junitxml={junit_path}",
        ],
        cwd=repo,
    )
    junit = _parse_junit(junit_path)
    green = (
        proc.returncode == 0
        and junit.get("tests", 0) > 0
        and junit.get("failures", 0) == 0
        and junit.get("errors", 0) == 0
        and junit.get("skipped", 0) == 0
    )
    return LaneResult(
        name="live-docker",
        status="ran",
        green=green,
        exit_code=proc.returncode,
        junit=junit,
        junit_path=str(junit_path),
        detail={"docker_versions": docker_versions},
    )


# ---- anti-bypass scanner (plan §4.4 / §4.9) -----------------------------------


def _attr_root_name(node: ast.expr) -> str | None:
    """The leftmost identifier of an attribute chain (``a.b.c`` -> ``a``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _setattr_target_ok(call: ast.Call) -> tuple[bool, str]:
    """A ``monkeypatch.setattr`` is allowed ONLY as ``setattr(cfg_store, 'load', ...)``
    (the config seam). Anything else — a code-under-test module or a string target — is
    a violation. Returns (allowed, rendered-target)."""
    if len(call.args) >= 2:
        first, second = call.args[0], call.args[1]
        if (
            isinstance(first, ast.Name)
            and first.id == "cfg_store"
            and isinstance(second, ast.Constant)
            and second.value == "load"
        ):
            return True, "cfg_store, 'load'"
    try:
        rendered = ast.unparse(call.args[0]) if call.args else "<no-target>"
    except (ValueError, AttributeError):
        rendered = "<unrenderable-target>"
    return False, rendered


def _scan_python_test(source: str, rel: str) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []

    def add(line: int, rule: str, detail: str) -> None:
        violations.append({"file": rel, "line": line, "rule": rule, "detail": detail})

    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        add(exc.lineno or 0, "python_parse_error", str(exc))
        return violations

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mock" or alias.name.startswith("unittest.mock"):
                    add(node.lineno, "forbidden_mock_import", f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "unittest.mock" or module.startswith("unittest.mock."):
                add(node.lineno, "forbidden_mock_import", f"from {module} import ...")
            elif module == "unittest" and any(a.name == "mock" for a in node.names):
                add(node.lineno, "forbidden_mock_import", "from unittest import mock")
        elif isinstance(node, ast.Name) and node.id in _PY_MOCK_ANY_USE_NAMES:
            add(node.lineno, "forbidden_mock_usage", f"use of {node.id}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _PY_MOCK_CALL_NAMES:
                add(node.lineno, "forbidden_mock_usage", f"{func.id}(...) call")
            elif isinstance(func, ast.Attribute):
                if func.attr == "patch" and _attr_root_name(func.value) == "mock":
                    add(node.lineno, "forbidden_mock_usage", "mock.patch(...)")
                if (
                    func.attr == "setattr"
                    and _attr_root_name(func.value) in _MONKEYPATCH_FIXTURE_NAMES
                ):
                    ok, target = _setattr_target_ok(node)
                    if not ok:
                        add(
                            node.lineno,
                            "forbidden_monkeypatch_target",
                            f"monkeypatch.setattr({target}, ...) — only cfg_store.load "
                            "(the config seam) is allowed",
                        )
    return violations


def _ts_imported_names(source: str) -> set[str]:
    names: set[str] = set()
    for line in source.splitlines():
        match = _TS_IMPORT_RE.match(line)
        if not match:
            continue
        body = match.group("body")
        for brace in re.findall(r"\{([^}]*)\}", body):
            for part in brace.split(","):
                token = part.strip().split(" as ")[-1].strip()
                if token:
                    names.add(token)
        outside = re.sub(r"\{[^}]*\}", "", body)
        for part in outside.split(","):
            token = part.strip()
            star = re.match(r"\*\s+as\s+([A-Za-z_$][\w$]*)", token)
            if star:
                names.add(star.group(1))
            elif re.fullmatch(r"[A-Za-z_$][\w$]*", token):
                names.add(token)
    return names


def _scan_frontend_test(source: str, rel: str) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    imported = _ts_imported_names(source)
    for lineno, line in enumerate(source.splitlines(), start=1):
        for token, rule in _FRONTEND_FORBIDDEN:
            if token in line:
                violations.append({"file": rel, "line": lineno, "rule": rule, "detail": token})
        match = _VI_FN_ASSIGN_RE.search(line)
        if match:
            lhs = line[: match.start()].rstrip()
            if not _DECL_TAIL_RE.search(lhs):
                target = lhs.split()[-1] if lhs.split() else lhs
                root_match = re.match(r"([A-Za-z_$][\w$]*)", target)
                root = root_match.group(1) if root_match else None
                if root and root in imported:
                    violations.append(
                        {
                            "file": rel,
                            "line": lineno,
                            "rule": "forbidden_vi_fn_module_override",
                            "detail": f"vi.fn() overwrites imported binding '{root}'",
                        }
                    )
    return violations


def _scan_production_line(rel: str, lineno: int, line: str) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for token, rule in _CLOSEOUT_TOKENS.items():
        if token in line:
            hits.append({"file": rel, "line": lineno, "rule": rule, "detail": token})
    if "PYTEST_CURRENT_TEST" in line and (rel, line.strip()) not in _PYTEST_CURRENT_TEST_BASELINE:
        hits.append(
            {
                "file": rel,
                "line": lineno,
                "rule": "production_test_switch",
                "detail": "PYTEST_CURRENT_TEST outside the ratified auth/workflow baseline",
            }
        )
    if _FORCE_VERDICT_RE.search(line):
        hits.append(
            {
                "file": rel,
                "line": lineno,
                "rule": "production_force_verdict_switch",
                "detail": "test-only force-verdict env switch",
            }
        )
    return hits


def _is_test_path(parts: tuple[str, ...], name: str) -> bool:
    if any(seg in {"test", "tests", "__tests__", "e2e"} for seg in parts):
        return True
    return ".test." in name or ".spec." in name


def _frozen_python_test_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for rel in manifest_mod.CLOSEOUT_TEST_DIRS:
        base = repo / rel
        if base.exists():
            files.extend(p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts)
    for rel in (
        manifest_mod.LIVE_TEST_FILE,
        "packages/agent-server/tests/integration/_closeout_live_support.py",
    ):
        p = repo / rel
        if p.is_file():
            files.append(p)
    return files


def _frozen_frontend_test_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for rel in (manifest_mod.FRONTEND_VITEST_DIR, manifest_mod.FRONTEND_E2E_DIR):
        base = repo / rel
        if base.exists():
            files.extend(
                p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix in {".ts", ".tsx"}
            )
    return files


def _production_src_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for src in sorted((repo / "packages").glob("*/src")):
        for p in sorted(src.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel_parts = p.relative_to(src).parts
            if _is_test_path(rel_parts, p.name):
                continue
            files.append(p)
    frontend_src = repo / "frontend" / "src"
    if frontend_src.exists():
        for p in sorted(frontend_src.rglob("*")):
            if not p.is_file() or p.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
                continue
            rel_parts = p.relative_to(frontend_src).parts
            if _is_test_path(rel_parts, p.name):
                continue
            files.append(p)
    return files


def _run_scanner_lane(repo: Path, evidence_dir: Path) -> LaneResult:
    """The anti-bypass static scan (plan §4.4 / §4.9).

    §4.4 operational reading (reviewer-ratifiable interpretation, quoted verbatim by
    the manifest ``note`` and ``anti-bypass-scan.json``):

        A closeout acceptance test may seam the system ONLY at (1) the config loader —
        ``monkeypatch.setattr(cfg_store, 'load', ...)``, the same injection the settings
        PUT performs — and (2) the environment — ``monkeypatch.setenv``/``delenv`` (e.g.
        ``DISCO_DATA_DIR``). No other ``monkeypatch.setattr`` target is permitted; the
        code under test (detect / spec / emit / release-route / tool / store) is never
        patched. ``unittest.mock`` / ``MagicMock`` / ``Mock()`` / ``create_autospec`` /
        ``patch()`` are forbidden entirely. In the frontend, ``vi.fn()`` is allowed ONLY
        as a callback/prop value (e.g. ``onDownload={vi.fn()}``) or a locally-declared
        const passed as one; ``vi.mock`` / ``vi.spyOn`` / ``vi.stubGlobal`` /
        ``vi.stubEnv`` / ``jest.mock`` (module replacement) are forbidden. Production
        source may not reference closeout fixture names, the C8 secret/build sentinels,
        ``CLOSEOUT_SEED``, a force-verdict switch, or ``PYTEST_CURRENT_TEST`` outside the
        ratified pre-existing auth/workflow baseline.

    A lexical/AST scan cannot prove the absence of a semantically-equivalent indirect
    branch; the independent diff review (plan §4.9) is the required backstop. Python is
    AST-parsed (never regex-only); the frontend + production scans are lexical.
    """
    violations: list[dict[str, object]] = []

    py_files = _frozen_python_test_files(repo)
    for path in py_files:
        rel = path.relative_to(repo).as_posix()
        violations.extend(_scan_python_test(path.read_text(encoding="utf-8"), rel))

    fe_files = _frozen_frontend_test_files(repo)
    for path in fe_files:
        rel = path.relative_to(repo).as_posix()
        violations.extend(_scan_frontend_test(path.read_text(encoding="utf-8"), rel))

    prod_files = _production_src_files(repo)
    for path in prod_files:
        rel = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            violations.extend(_scan_production_line(rel, lineno, line))

    def _violation_sort_key(v: dict[str, object]) -> tuple[str, int, str]:
        line = v["line"]
        return (str(v["file"]), line if isinstance(line, int) else 0, str(v["rule"]))

    violations.sort(key=_violation_sort_key)
    clean = not violations
    report = {
        "status": "ran",
        "clean": clean,
        "violation_count": len(violations),
        "operational_reading": manifest_mod.OPERATIONAL_READING,
        "production_scan_note": (
            "PYTEST_CURRENT_TEST is allowlisted only at the ratified pre-existing "
            "auth/workflow sites; any other production reference — especially in the "
            "release/export subsystem — is a violation. A lexical/AST scan cannot prove "
            "the absence of a semantically-equivalent indirect branch; the independent "
            "diff review (plan §4.9) is the required backstop."
        ),
        "scanned": {
            "python_test_files": len(py_files),
            "frontend_test_files": len(fe_files),
            "production_src_files": len(prod_files),
        },
        "violations": violations,
    }
    (evidence_dir / "anti-bypass-scan.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return LaneResult(
        name="anti-bypass-scan",
        status="ran",
        green=clean,
        detail={
            "violation_count": len(violations),
            "scanned": report["scanned"],
            "artifact": str(evidence_dir / "anti-bypass-scan.json"),
        },
    )


# ---- Docker-host-only evidence placeholders (honest, never faked) -------------


def _write_docker_host_artifacts(evidence_dir: Path, docker_available: bool) -> dict[str, str]:
    status = "produced_by_live_lane_on_docker_host"
    note = (
        "This artifact is produced by the live-docker lane on a real Docker host "
        "(plan §12). "
        + (
            "A Docker engine is present but the live lane did not complete a clean lifecycle; "
            if docker_available
            else "No Docker engine is available here; "
        )
        + "it is honestly absent, not faked."
    )
    written: dict[str, str] = {}
    for name in _DOCKER_HOST_ARTIFACTS:
        payload = {"status": status, "available": docker_available, "note": note}
        (evidence_dir / name).write_text(
            json.dumps(payload, indent=2) + "\n" if name.endswith(".json") else note + "\n",
            encoding="utf-8",
        )
        written[name] = status
    return written


# ---- CLI ----------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run every wired lane")
    parser.add_argument(
        "--author",
        action="store_true",
        help="acceptance-authoring mode: allow a dirty tree; NEVER emits passed:true",
    )
    parser.add_argument(
        "--evidence-dir",
        required=True,
        type=Path,
        help="evidence output directory (MUST be OUTSIDE the checkout)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=manifest_mod.repo_root(),
        help="the checkout root (defaults to this script's repository)",
    )
    return parser


def _not_passed_reasons(
    *,
    author: bool,
    clean: bool,
    frozen_ok: bool,
    lanes: list[LaneResult],
) -> list[str]:
    reasons: list[str] = []
    if author:
        reasons.append("author_mode forces passed:false")
    if not clean:
        reasons.append("checkout is dirty")
    if not frozen_ok:
        reasons.append("frozen manifest not verified")
    for lane in lanes:
        if lane.name == "python-closeout" and lane.rejected:
            reasons.extend(lane.rejection_reasons)
        elif lane.green is not True:
            reasons.append(f"{lane.name} lane not green")
    return reasons


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    repo = args.repo_root.resolve()
    evidence_dir = args.evidence_dir.resolve()

    if repo == evidence_dir or repo in evidence_dir.parents:
        print(
            f"ERROR: --evidence-dir {evidence_dir} is inside the checkout {repo}; "
            "it must be outside (plan §1.4).",
            file=sys.stderr,
        )
        return 2
    evidence_dir.mkdir(parents=True, exist_ok=True)

    clean = _is_clean(repo)
    candidate_sha = _git(repo, "rev-parse", "HEAD")

    if not clean and not args.author:
        print(
            "ERROR: refusing to run on a dirty checkout (git status --porcelain is "
            "non-empty). Re-run from a clean checkout, or use --author to produce "
            "authoring evidence (which can never pass).",
            file=sys.stderr,
        )
        evidence = {
            "schema": "export-track1-closeout-evidence/v1",
            "passed": False,
            "refused": "dirty_checkout",
            "candidate_sha": candidate_sha,
            "clean_tree": clean,
            "author_mode": args.author,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        (evidence_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
        return 2

    stored, load_note = _load_manifest(repo)
    frozen_ok, frozen_note = _verify_frozen_manifest(repo, stored, load_note)
    frozen_inventory: list[str] = []
    frontend_inventory: dict[str, object] = {}
    if stored is not None:
        raw_py = stored.get("python_closeout_inventory", [])
        if isinstance(raw_py, list):
            frozen_inventory = [str(x) for x in raw_py]
        raw_fe = stored.get("frontend_closeout_inventory", {})
        if isinstance(raw_fe, dict):
            frontend_inventory = raw_fe

    tool_versions = _tool_versions(repo)
    lanes: list[LaneResult] = []

    nonlive = _run_pytest_lane(
        repo,
        evidence_dir,
        name="python-nonlive",
        paths=manifest_mod.NONLIVE_TEST_PATHS,
        marker=manifest_mod.NONLIVE_MARKER,
        junit_filename="pytest-nonlive.xml",
    )
    nonlive.green = nonlive.exit_code == 0
    lanes.append(nonlive)

    closeout = _run_pytest_lane(
        repo,
        evidence_dir,
        name="python-closeout",
        paths=manifest_mod.CLOSEOUT_TEST_DIRS,
        marker=manifest_mod.CLOSEOUT_MARKER_NONLIVE,
        junit_filename="pytest-closeout.xml",
    )
    observed = manifest_mod.collect_python_closeout_ids(repo)
    expected = sorted(frozen_inventory)
    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    inventory_ok = bool(expected) and not missing and not extra
    inventory_note = f"missing={missing} extra={extra}" if not inventory_ok else "match"
    _evaluate_closeout(closeout, inventory_ok, inventory_note)
    closeout.detail = {
        "inventory_ok": inventory_ok,
        "expected_count": len(expected),
        "observed_count": len(observed),
        "missing": missing,
        "extra": extra,
    }
    lanes.append(closeout)

    frontend = _run_frontend_lane(repo, evidence_dir, frontend_inventory)
    lanes.append(frontend)

    live = _run_live_lane(repo, evidence_dir)
    lanes.append(live)

    scanner = _run_scanner_lane(repo, evidence_dir)
    lanes.append(scanner)

    docker_available = tool_versions.get("docker", "unavailable") != "unavailable"
    docker_host_artifacts = _write_docker_host_artifacts(evidence_dir, docker_available)

    all_lanes_green = all(lane.green is True for lane in lanes)
    passed = bool(frozen_ok and all_lanes_green and clean and not args.author)

    evidence = {
        "schema": "export-track1-closeout-evidence/v1",
        "passed": passed,
        "author_mode": args.author,
        "candidate_sha": candidate_sha,
        "clean_tree": clean,
        "baseline_sha": manifest_mod.BASELINE_SHA,
        "acceptance_tag": manifest_mod.ACCEPTANCE_TAG,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool_versions": tool_versions,
        "frozen_manifest": {"ok": frozen_ok, "note": frozen_note},
        "closeout_inventory": {
            "ok": inventory_ok,
            "expected": expected,
            "observed": observed,
            "missing": missing,
            "extra": extra,
        },
        "lanes": [asdict(lane) for lane in lanes],
        "evidence_files": {
            "pytest-nonlive.xml": "written",
            "pytest-closeout.xml": "written",
            "pytest-live.xml": "written",
            "frontend-vitest.json": "written" if frontend.detail.get("npx_available") else "absent",
            "docker-versions.txt": "written",
            "anti-bypass-scan.json": "written",
            **docker_host_artifacts,
        },
        "not_passed_reasons": _not_passed_reasons(
            author=args.author, clean=clean, frozen_ok=frozen_ok, lanes=lanes
        ),
    }
    (evidence_dir / "evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )

    lane_summary = " ".join(f"{lane.name}={lane.green}" for lane in lanes)
    print(
        f"evidence written to {evidence_dir}: passed={passed} "
        f"(clean={clean} author={args.author} frozen_ok={frozen_ok}) [{lane_summary}]"
    )

    if args.author:
        return 0
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
