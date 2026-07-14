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
    test-file set equals the frozen ``frontend_closeout_inventory`` AND, per file, the
    EXECUTED (passed|failed) leaf-title set equals that file's frozen ``tests`` list
    (a dropped/renamed/skipped/unreported/extra title makes the lane non-green — filename
    parity alone is insufficient). The Playwright
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

# ---- evidence-hygiene scan: registered planted-credential sentinels (WO-B) -----
# LABEL -> distinctive MARKER substring. Every written TEXT evidence artifact is scanned
# for these markers; any hit fails acceptance CLOSED (folds into `passed`), making the
# ABSENCE of a planted credential from the evidence enforceable and regression-proof.
# We register the MARKER (a unique NON-secret substring of the planted token) and NOT the
# full credential literal, so this verifier source never itself re-plants a real secret
# value — yet the marker is a substring of the literal, so scanning for it still catches
# the literal wherever it lands. ``G02POSCRED`` is the positional-credential sentinel
# planted in ``tests/export_track1_closeout/test_g02_positional_credential.py``.
_EVIDENCE_HYGIENE_SENTINELS: dict[str, str] = {
    "G02_POSITIONAL_CRED": "G02POSCRED",
}
# The TEXT evidence artifacts the hygiene scan reads (each scanned only if it exists).
# NOT ``evidence.json`` — it is written AFTER this scan runs and would otherwise, by
# recording violations, scan itself.
_HYGIENE_SCAN_FILES: tuple[str, ...] = (
    "pytest-nonlive.xml",
    "pytest-closeout.xml",
    "pytest-live.xml",
    "frontend-vitest.json",
    # R6: the structured Firefox e2e report is a written text artifact too — a planted
    # credential sentinel here must likewise force passed:false (plan §9.4 / criterion 6).
    "frontend-e2e.json",
    "g11-typecheck.txt",
    "anti-bypass-scan.json",
    "docker-versions.txt",
    *_DOCKER_HOST_ARTIFACTS,
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
# Frontend: module-replacement helpers are forbidden outright. Detection is
# alias-aware (see `_vi_local_names`) and covers both attribute (`vi.mock(`) and
# subscript (`vi['mock']`) forms, so aliasing/bracketing `vi` cannot evade it.
_FORBIDDEN_VI_METHODS: tuple[tuple[str, str], ...] = (
    ("mock", "forbidden_vi_mock"),
    ("spyOn", "forbidden_vi_spyOn"),
    ("stubGlobal", "forbidden_vi_stubGlobal"),
    ("stubEnv", "forbidden_vi_stubEnv"),
)
# jest.* is never aliased in this codebase; flag its literal module-replacement forms.
_FRONTEND_FORBIDDEN = (
    ("jest.mock(", "forbidden_jest_mock"),
    ("jest.spyOn(", "forbidden_jest_spyOn"),
)
_VI_FN_ASSIGN_RE = re.compile(r"(?<![=!<>])=\s*vi\.fn\s*\(")
_DECL_TAIL_RE = re.compile(r"\b(?:const|let|var)\b[^=]*$")
_TS_IMPORT_RE = re.compile(r"""^\s*import\s+(?P<body>.+?)\s+from\s+["']""")
# Whole-source (DOTALL) form so multi-line `import { … } from "…"` blocks are parsed.
_TS_IMPORT_BLOCK_RE = re.compile(r"""import\s+(?P<body>.+?)\s+from\s+["']""", re.DOTALL)
# `const v = vi` / `let v = vi` / `v = vi` — a local aliased to the vi test runner.
_VI_ALIAS_RE = re.compile(r"\b(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*vi\s*[;\n]")

# ---- G18 no-new-suppression: campaign-diff scanner rules (plan §9.4 / criterion 4) ----
# Each pattern matches the ACTUAL directive SYNTAX (a trailing comment directive, a
# decorator, a real call, or a YAML key), NOT a bare prose substring — so an explanatory
# comment ("no continue-on-error / skip path") or a docstring mentioning "xfail" is not
# flagged. ``@ts-expect-error`` is deliberately NOT matched: it is a negative type
# ASSERTION (tsc errors TS2578 if the expected error is absent), the inverse of a
# suppression, so it can never hide a real error — and the frozen g11 guard contract
# relies on that exclusion. ``@ts-nocheck`` IS matched: unlike ``@ts-expect-error`` it
# disables type-checking for the WHOLE file, so it can hide real errors on the
# typecheck:build + g11 tsc lanes. The module-level ``pytestmark`` global is matched only
# when it assigns a ``mark.skip``/``mark.skipif``/``mark.xfail`` (a whole-module skip the
# per-test decorator patterns miss); a legitimate ``pytestmark = pytest.mark.integration``
# / ``mark.export_track1_closeout`` marker is NOT a suppression and is not flagged.
# Out of scope by design: config-file ignore lists (e.g. ``[tool.ruff] ignore`` in
# pyproject.toml) — a distinct, human-reviewed class backstopped by the plan §4.4
# independent diff review, not this token scan.
_G18_SUPPRESSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("noqa", re.compile(r"#\s*noqa")),
    ("type_ignore", re.compile(r"#\s*type:\s*ignore")),
    ("pyright_ignore", re.compile(r"#\s*pyright:\s*ignore")),
    ("ruff_noqa", re.compile(r"#\s*ruff:\s*noqa")),
    ("eslint_disable", re.compile(r"eslint-disable")),
    ("ts_ignore", re.compile(r"//\s*@ts-ignore")),
    ("ts_nocheck", re.compile(r"//\s*@ts-nocheck")),
    ("continue_on_error", re.compile(r"continue-on-error\s*:")),
    ("pytest_skip", re.compile(r"@pytest\.mark\.skip|@pytest\.mark\.skipif|pytest\.skip\s*\(")),
    ("pytest_xfail", re.compile(r"@pytest\.mark\.xfail|pytest\.xfail\s*\(")),
    (
        "pytestmark_skip",
        re.compile(r"\bpytestmark\b\s*=.*\bmark\.(?:skipif|skip|xfail)\b"),
    ),
    ("js_skip", re.compile(r"\b(?:describe|test|it)\s*\.\s*skip\s*\(")),
    ("js_only", re.compile(r"\b(?:describe|test|it)\s*\.\s*only\s*\(")),
)
# Authoritative source of a hunk's destination file: the ``diff --git a/… b/…`` header.
# Parsing this (not solely ``+++ b/``) keeps the scan anchored even when a runner's git is
# configured with ``diff.noprefix=true`` and emits ``+++ x`` (no ``b/`` prefix) — which
# would otherwise leave ``cur_file`` None and silently no-op the ENTIRE scan (fail-open).
# _run_scanner_lane additionally forces ``--src-prefix=a/ --dst-prefix=b/`` at the diff
# invocation so prefixes are always present regardless of the host's git config.
_DIFF_GIT_HEADER_RE: re.Pattern[str] = re.compile(r"^diff --git a/.+ b/(?P<dst>.+)$")
# The diff scan covers real code/config only.
_G18_DIFF_SCAN_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".yml", ".yaml")
# Exempt the two gate-definition scripts (they DEFINE these patterns as detection rules)
# and the release_remediation meta-test dir (its mutation tests embed suppression tokens
# as fixtures). Both are byte-hashed by the manifest and reviewed; excluding them from the
# token scan is what stops the gate from flagging its own definition. Docs/JSON/manifest
# are excluded by suffix.
_G18_DIFF_EXEMPT_PREFIXES = (
    "scripts/verify_export_track1_closeout.py",
    "scripts/gen_closeout_acceptance_manifest.py",
    "packages/agent-server/tests/release_remediation/",
)


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


def _parse_vitest(
    path: Path,
) -> tuple[dict[str, int], set[str], dict[str, set[str]], bool]:
    """Return ``(counts, test-file basenames, per-file executed leaf titles, parsed-ok)``.
    Vitest ``--reporter=json`` is Jest-shaped: numTotalTests / numFailedTests /
    numPendingTests / numTodoTests, and ``testResults[].name`` (absolute file path) with
    ``testResults[].assertionResults[]`` leaf nodes (each ``.title`` + ``.status``).

    The per-file title map records ONLY EXECUTED leaves — ``status`` in
    {"passed", "failed"}. A skipped / pending / todo leaf is EXCLUDED, so it reads
    downstream as an UNREPORTED (missing) title and cannot silently satisfy the frozen
    inventory. The leaf ``.title`` (NOT ``.fullName``) is used so it matches the
    source-parsed ``it(...)`` string in ``frontend_closeout_inventory``. A file that
    reports any leaf gets a set entry even when that set ends up empty (all leaves
    skipped), so a wholly-skipped file still surfaces as missing titles."""
    counts = {"total": -1, "passed": -1, "failed": -1, "pending": -1, "todo": -1}
    if not path.is_file():
        return counts, set(), {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return counts, set(), {}, False
    if not isinstance(data, dict):
        return counts, set(), {}, False
    counts = {
        "total": int(data.get("numTotalTests", 0)),
        "passed": int(data.get("numPassedTests", 0)),
        "failed": int(data.get("numFailedTests", 0)),
        "pending": int(data.get("numPendingTests", 0)),
        "todo": int(data.get("numTodoTests", 0)),
    }
    files: set[str] = set()
    titles: dict[str, set[str]] = {}
    results = data.get("testResults", [])
    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            basename = Path(name).name
            files.add(basename)
            executed = titles.setdefault(basename, set())
            assertions = entry.get("assertionResults", [])
            if isinstance(assertions, list):
                for assertion in assertions:
                    if not isinstance(assertion, dict):
                        continue
                    title = assertion.get("title")
                    status = assertion.get("status")
                    if isinstance(title, str) and status in {"passed", "failed"}:
                        executed.add(title)
    return counts, files, titles, True


def _diff_frontend_titles(
    frozen_inventory: dict[str, object], observed_titles: dict[str, set[str]]
) -> tuple[bool, dict[str, object]]:
    """Compare the FROZEN per-file ``it(...)`` title inventory against the per-file set of
    EXECUTED (passed|failed) leaf titles vitest reported. Returns ``(ok, detail)``:
    ``ok`` is False if ANY frozen file has a missing / renamed / skipped / unreported
    title, or reports an extra/unexpected executed title. This is the SINGLE SOURCE OF
    TRUTH the frontend lane uses, and is pure/importable so the committed regression can
    call it WITHOUT running vitest.

    File-SET drift (a frozen file absent from vitest, or a surprise extra file) is the
    caller's basename comparison; here titles are compared per frozen file (a missing
    file yields an empty observed set, so its titles read as missing too — a belt-and-
    braces overlap that only ever makes a lane HARDER to green). A frozen entry WITHOUT a
    ``tests`` list carries NO title constraint: the real manifest always emits one, so
    this only leaves a synthetic inventory that omits it (e.g. the G13 isolation mirror)
    unconstrained. Titles are plain human-readable ``it(...)`` strings, never credential
    material, so echoing mismatches is safe."""
    per_file: dict[str, dict[str, list[str]]] = {}
    ok = True
    for basename, meta in frozen_inventory.items():
        if not isinstance(meta, dict) or "tests" not in meta:
            continue
        raw_tests = meta.get("tests")
        if not isinstance(raw_tests, list):
            continue
        expected = {str(title) for title in raw_tests}
        observed = observed_titles.get(basename, set())
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        if missing or extra:
            ok = False
            per_file[basename] = {"missing": missing, "extra": extra}
    detail: dict[str, object] = {"title_mismatches": per_file}
    return ok, detail


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
    counts, discovered, observed_titles, parsed_ok = _parse_vitest(vitest_json)
    frozen_files = set(frozen_inventory.keys())
    missing_files = sorted(frozen_files - discovered)
    extra_files = sorted(discovered - frozen_files)
    file_set_ok = parsed_ok and not missing_files and not extra_files
    # Per-file EXECUTED-title comparison (single source of truth: _diff_frontend_titles).
    # Filename-set equality is NOT sufficient — a renamed/dropped/skipped/extra title
    # inside a frozen file must make the lane non-green even when the file set matches.
    titles_ok, titles_detail = _diff_frontend_titles(frozen_inventory, observed_titles)
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
        "titles_ok": titles_ok,
        "title_mismatches": titles_detail["title_mismatches"],
    }

    tproc = _run(["npm", "run", "typecheck:build"], cwd=frontend_dir)
    detail["typecheck"] = {"exit_code": tproc.returncode}

    bproc = _run(["npx", "vite", "build"], cwd=frontend_dir)
    detail["build"] = {"exit_code": bproc.returncode}

    # BROWSER e2e GATE (G13(b) / G19 / plan §9.1-§9.3). The Firefox lane runs SEPARATELY
    # in _run_browser_e2e (called by main BEFORE this lane) and writes its structured JSON
    # report to evidence_dir/frontend-e2e.json. Here we only PARSE + GATE on that report:
    #   * absent report  -> status "not_executed_offline", browser_ok False (the frozen
    #     G13(b) call hits exactly this: a fresh empty evidence_dir has no report);
    #   * present report -> green browser only if it EXECUTED and expected>0, unexpected==0,
    #     flaky==0, skipped==0 AND the executed spec set equals the frozen e2e inventory
    #     (all frontend/e2e/export-track1-closeout/*.spec.ts).
    # browser_ok is folded into lane.green, so an un-run / failed / partial browser proof
    # can never be a green, gating input to passed:true.
    e2e_dir = repo / manifest_mod.FRONTEND_E2E_DIR
    expected_specs = {p.name for p in e2e_dir.glob("*.spec.ts")} if e2e_dir.exists() else set()
    browser_ok, browser_detail = _parse_playwright_e2e(
        evidence_dir / "frontend-e2e.json", expected_specs
    )
    detail["playwright_e2e"] = browser_detail

    lane.green = bool(
        vitest_ok
        and file_set_ok
        and titles_ok
        and tproc.returncode == 0
        and bproc.returncode == 0
        and browser_ok
    )
    return lane


# ---- browser e2e (Firefox) lane (plan §9.1-§9.3) ------------------------------


def _playwright_spec_basenames(data: dict[str, object]) -> set[str]:
    """Every ``*.spec.ts`` file basename that appears in a Playwright JSON report's suite
    tree — the set of spec files that actually EXECUTED. Walks nested suites/specs so a
    dropped or extra spec file surfaces as a spec-set mismatch."""
    names: set[str] = set()

    def _walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        file_val = node.get("file")
        if isinstance(file_val, str) and file_val.endswith(".spec.ts"):
            names.add(Path(file_val).name)
        for key in ("suites", "specs"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    _walk(child)

    top = data.get("suites")
    if isinstance(top, list):
        for suite in top:
            _walk(suite)
    return names


def _parse_playwright_e2e(path: Path, expected_specs: set[str]) -> tuple[bool, dict[str, object]]:
    """Parse the structured Firefox report at ``path`` and decide ``browser_ok``. Pure +
    importable so the committed mutation tests drive it WITHOUT running a browser.

    Returns ``(browser_ok, detail)``. ``browser_ok`` is True ONLY when the run executed and
    Playwright's ``stats`` show ``expected > 0`` with ``unexpected == flaky == skipped == 0``
    AND the executed spec-file basenames equal ``expected_specs`` (the frozen e2e
    inventory). An ABSENT report is recorded as ``not_executed_offline`` (the frozen G13(b)
    state); an unparseable report or one _run_browser_e2e marked ``browser_run_failed`` /
    ``not_executed_offline`` is likewise not green. Fail-closed: missing counts default to a
    failing value."""
    if not path.is_file():
        return False, {
            "status": "not_executed_offline",
            "browser_ok": False,
            "reason": (
                "no frontend-e2e.json in the evidence dir — the Firefox e2e lane did not "
                "execute (the frozen candidate/needs_review Firefox proofs need a "
                "fixture-mode server + a browser); an un-run browser proof cannot be green"
            ),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, {"status": "unparseable", "browser_ok": False}
    if not isinstance(data, dict):
        return False, {"status": "unparseable", "browser_ok": False}
    marked = data.get("status")
    if marked in {"browser_run_failed", "not_executed_offline"}:
        return False, {
            "status": str(marked),
            "browser_ok": False,
            "exit_code": data.get("exit_code"),
        }
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}

    def _stat(key: str, absent_default: int) -> int:
        raw = stats.get(key, absent_default) if isinstance(stats, dict) else absent_default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return absent_default

    expected = _stat("expected", 0)
    unexpected = _stat("unexpected", 1)
    flaky = _stat("flaky", 1)
    skipped = _stat("skipped", 1)
    executed_specs = _playwright_spec_basenames(data)
    spec_set_ok = executed_specs == expected_specs and bool(expected_specs)
    browser_ok = bool(
        expected > 0 and unexpected == 0 and flaky == 0 and skipped == 0 and spec_set_ok
    )
    return browser_ok, {
        "status": "executed",
        "browser_ok": browser_ok,
        "counts": {
            "expected": expected,
            "unexpected": unexpected,
            "flaky": flaky,
            "skipped": skipped,
        },
        "executed_specs": sorted(executed_specs),
        "expected_specs": sorted(expected_specs),
        "spec_set_ok": spec_set_ok,
    }


def _run_browser_e2e(repo: Path, evidence_dir: Path) -> dict[str, object]:
    """RUN the frozen Firefox e2e proofs (``npx playwright test <FRONTEND_E2E_DIR>
    --reporter=json --project=firefox``, fixture mode) and write the structured report to
    ``evidence_dir/frontend-e2e.json`` (plan §9.1/§9.3). Kept SEPARATE from
    _run_frontend_lane (which only parses+gates) so the frozen G13(b) test — which calls
    _run_frontend_lane alone against a fresh empty evidence_dir — records
    ``not_executed_offline``. Called by main() BEFORE the frontend lane.

    On a host without ``npx`` no report is written (the frontend lane then records
    not_executed_offline). When Playwright emits no parseable JSON (e.g. the fixture server
    failed to boot), a truthful ``browser_run_failed`` record is written — NEVER a
    success-shaped report — so the frontend lane's browser gate is non-green."""
    frontend_dir = repo / "frontend"
    out_path = evidence_dir / "frontend-e2e.json"
    summary: dict[str, object] = {"command": manifest_mod.COMMAND_INVENTORY["browser_e2e"]}
    if shutil.which("npx") is None:
        summary["status"] = "not_executed_offline"
        summary["reason"] = "npx unavailable on this host; browser e2e lane cannot run"
        return summary
    e2e_rel = manifest_mod.FRONTEND_E2E_DIR.split("/", 1)[1]
    proc = _run(
        ["npx", "playwright", "test", e2e_rel, "--reporter=json", "--project=firefox"],
        cwd=frontend_dir,
    )
    summary["exit_code"] = proc.returncode
    report: object = None
    stdout = (proc.stdout or "").strip()
    if stdout:
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError:
            report = None
    if isinstance(report, dict):
        out_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        summary["status"] = "executed"
    else:
        # Truthful failure record: Playwright produced no parseable JSON report (missing
        # browser, fixture-server boot failure, crash). NEVER a success-shaped artifact.
        failure = {
            "status": "browser_run_failed",
            "exit_code": proc.returncode,
            "note": (
                "playwright did not emit a parseable JSON report on stdout; the Firefox "
                "e2e lane did not complete — recorded as failed, not faked"
            ),
        }
        out_path.write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        summary["status"] = "browser_run_failed"
    return summary


# ---- G11 dedicated compile lane (plan §9.1) -----------------------------------


def _run_g11_compile_lane(repo: Path, evidence_dir: Path) -> LaneResult:
    """The dedicated G11 nullable-binding compile gate: ``npx tsc -p
    tsconfig.closeout-g11.json --noEmit`` (plan §9.1). A separate top-level lane (folded
    into all_lanes_green) so it never perturbs the frozen G13(b) _run_frontend_lane
    behavior. Green iff tsc exits 0. Firefox/vitest transpile without type-checking, so
    this dedicated compile is the only proof of the version_seq/tree_digest nullability
    contract."""
    frontend_dir = repo / "frontend"
    lane = LaneResult(name="g11-typecheck", status="ran")
    artifact = evidence_dir / "g11-typecheck.txt"
    if shutil.which("npx") is None:
        lane.green = False
        lane.detail = {
            "npx_available": False,
            "note": "npx unavailable; G11 compile lane cannot run",
        }
        artifact.write_text("npx unavailable; G11 compile lane did not run\n", encoding="utf-8")
        return lane
    proc = _run(
        ["npx", "tsc", "-p", "tsconfig.closeout-g11.json", "--noEmit"],
        cwd=frontend_dir,
    )
    lane.exit_code = proc.returncode
    lane.green = proc.returncode == 0
    lane.detail = {
        "npx_available": True,
        "command": manifest_mod.COMMAND_INVENTORY["g11_typecheck"],
        "exit_code": proc.returncode,
    }
    artifact.write_text(
        f"exit_code: {proc.returncode}\n\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n",
        encoding="utf-8",
    )
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


def _is_monkeypatch_source(call: ast.Call) -> bool:
    """True if a call yields a monkeypatch object: `MonkeyPatch(...)` or
    `<x>.getfixturevalue("monkeypatch")`."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "MonkeyPatch":
        return True
    if isinstance(func, ast.Attribute):
        if func.attr == "MonkeyPatch":
            return True
        if func.attr == "getfixturevalue" and call.args:
            arg0 = call.args[0]
            return isinstance(arg0, ast.Constant) and arg0.value == "monkeypatch"
    return False


def _monkeypatch_roots(tree: ast.Module) -> set[str]:
    """Names that refer to a monkeypatch fixture — the fixture params plus any local
    aliased to one (`m = monkeypatch`), a constructed `MonkeyPatch()`, or
    `request.getfixturevalue("monkeypatch")`, transitively — so aliasing cannot
    smuggle a `setattr` past the config-seam restriction."""
    roots = set(_MONKEYPATCH_FIXTURE_NAMES)
    changed = True
    while changed:  # fixpoint: catches chained aliases (m = monkeypatch; n = m)
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id in roots:
                continue
            value = node.value
            aliased = (isinstance(value, ast.Name) and value.id in roots) or (
                isinstance(value, ast.Call) and _is_monkeypatch_source(value)
            )
            if aliased:
                roots.add(target.id)
                changed = True
    return roots


def _scan_python_test(source: str, rel: str) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []

    def add(line: int, rule: str, detail: str) -> None:
        violations.append({"file": rel, "line": line, "rule": rule, "detail": detail})

    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        add(exc.lineno or 0, "python_parse_error", str(exc))
        return violations

    monkeypatch_roots = _monkeypatch_roots(tree)
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
                if func.attr == "setattr" and _attr_root_name(func.value) in monkeypatch_roots:
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
    """Local names introduced by `import … from "…"`, including multi-line blocks and
    `x as y` aliases (the alias is the local binding)."""
    names: set[str] = set()
    for match in _TS_IMPORT_BLOCK_RE.finditer(source):
        body = match.group("body")
        for brace in re.findall(r"\{([^}]*)\}", body, re.DOTALL):
            for part in brace.split(","):
                token = part.strip().split(" as ")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", token):
                    names.add(token)
        outside = re.sub(r"\{[^}]*\}", "", body, flags=re.DOTALL)
        for part in outside.split(","):
            token = part.strip()
            star = re.match(r"\*\s+as\s+([A-Za-z_$][\w$]*)", token)
            if star:
                names.add(star.group(1))
            elif re.fullmatch(r"[A-Za-z_$][\w$]*", token):
                names.add(token)
    return names


def _vi_local_names(source: str) -> set[str]:
    """Every local name that refers to the `vi` test runner: `vi` itself, `import
    { vi as X }`, and `const X = vi`. Used to make the module-replacement scan
    alias-proof."""
    names: set[str] = {"vi"}
    for match in _TS_IMPORT_BLOCK_RE.finditer(source):
        for brace in re.findall(r"\{([^}]*)\}", match.group("body"), re.DOTALL):
            for part in brace.split(","):
                bits = [b.strip() for b in part.split(" as ")]
                if bits[0] == "vi" and len(bits) == 2 and bits[1]:
                    names.add(bits[1])
    for match in _VI_ALIAS_RE.finditer(source):
        names.add(match.group(1))
    return names


def _scan_frontend_test(source: str, rel: str) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    imported = _ts_imported_names(source)
    vi_names = _vi_local_names(source)
    # `<vi>.method(` and `<vi>['method'](` for every alias of vi and every forbidden
    # module-replacement method — attribute and subscript forms alike.
    vi_alt = "|".join(re.escape(n) for n in sorted(vi_names))
    forbidden_vi_res = [
        (
            re.compile(rf"(?<![\w$])(?:{vi_alt})\s*(?:\.\s*{m}\s*\(|\[\s*['\"]{m}['\"]\s*\])"),
            rule,
            m,
        )
        for m, rule in _FORBIDDEN_VI_METHODS
    ]
    for lineno, line in enumerate(source.splitlines(), start=1):
        for token, rule in _FRONTEND_FORBIDDEN:
            if token in line:
                violations.append({"file": rel, "line": lineno, "rule": rule, "detail": token})
        for pattern, rule, method in forbidden_vi_res:
            if pattern.search(line):
                detail = f"vi.{method} (attr/subscript, alias-aware)"
                violations.append({"file": rel, "line": lineno, "rule": rule, "detail": detail})
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


def _g18_scanned_path(path: str) -> bool:
    """A campaign-diff path the G18 no-new-suppression scan inspects: real code/config,
    excluding the two gate-definition scripts and the release_remediation meta-test dir
    (which legitimately embed suppression tokens). Docs/JSON are excluded by suffix."""
    if not path.endswith(_G18_DIFF_SCAN_SUFFIXES):
        return False
    return not any(path.startswith(prefix) for prefix in _G18_DIFF_EXEMPT_PREFIXES)


def _load_suppression_baseline(repo: Path) -> tuple[set[tuple[str, str]], str]:
    """Load the owner-approved suppression baseline (plan §9.4). Returns
    ``(entries, note)`` where entries is a set of ``(repo-relative-file, stripped-line)``
    pairs. An absent/unparseable baseline yields an EMPTY set (fail-closed: any new
    suppression then trips the scan) with an explanatory note."""
    path = repo / manifest_mod.SUPPRESSION_BASELINE_REL
    if not path.is_file():
        return set(), f"suppression baseline absent ({manifest_mod.SUPPRESSION_BASELINE_REL})"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return set(), f"suppression baseline is not valid JSON: {exc}"
    entries: set[tuple[str, str]] = set()
    approved = data.get("approved", []) if isinstance(data, dict) else []
    if isinstance(approved, list):
        for entry in approved:
            if not isinstance(entry, dict):
                continue
            file_val = entry.get("file")
            line_val = entry.get("line")
            if isinstance(file_val, str) and isinstance(line_val, str):
                entries.add((file_val, line_val.strip()))
    return entries, "loaded"


def _scan_campaign_diff_suppressions(
    diff_text: str, baseline: set[tuple[str, str]]
) -> list[dict[str, object]]:
    """Scan a unified ``git diff`` for NEWLY-ADDED suppression directives (plan §9.4 /
    criterion 4). For every ADDED line ('+') in a scanned file (see ``_g18_scanned_path``)
    whose text matches a suppression pattern, emit a violation UNLESS its
    ``(file, stripped-line)`` pair is in the owner-approved ``baseline``. Pure/importable
    so the committed mutation tests drive it with synthetic hunks. New-file line numbers
    are tracked from the ``@@`` hunk headers for locability; matching is by text, never by
    line number, so drift cannot break a baseline entry."""
    violations: list[dict[str, object]] = []
    cur_file: str | None = None
    new_lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            # AUTHORITATIVE per-file anchor: take the destination path from the
            # ``diff --git a/… b/…`` header. This is what makes the scan robust to a
            # no-prefix diff (``+++ x``) — cur_file is already set here, so a ``+++`` line
            # lacking a ``b/`` prefix cannot blank it and vacuously pass the whole gate.
            header = _DIFF_GIT_HEADER_RE.match(raw)
            cur_file = header.group("dst") if header is not None else None
            new_lineno = 0
            continue
        if raw.startswith("+++ b/"):
            cur_file = raw[6:]  # unambiguous refinement when a b/ prefix is present
            continue
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("diff "):
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            new_lineno = int(match.group(1)) if match else 0
            continue
        if raw.startswith("+"):
            added = raw[1:]
            if cur_file is not None and _g18_scanned_path(cur_file):
                stripped = added.strip()
                if (cur_file, stripped) not in baseline:
                    for kind, pattern in _G18_SUPPRESSION_PATTERNS:
                        if pattern.search(added):
                            violations.append(
                                {
                                    "file": cur_file,
                                    "line": new_lineno,
                                    "rule": f"new_suppression_{kind}",
                                    "detail": stripped[:200],
                                }
                            )
                            break
            new_lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            new_lineno += 1
    return violations


# ---- command inventory gate (G19 / plan §9 criterion 3) -----------------------


def _observed_command_ids(repo: Path) -> set[str]:
    """The set of command IDs this verifier actually dispatches on this host. The Python
    lanes always run; the frontend/browser/G11 commands run only when node/npm/npx are
    present. On a real acceptance host (all tools present) this equals
    ``manifest_mod.REQUIRED_COMMAND_IDS``; a host missing a toolchain OMITS commands and
    the inventory gate then fails (an acceptance run cannot omit a required command)."""
    ids = {"python_nonlive", "python_closeout", "python_closeout_collect", "live_docker"}
    if shutil.which("npx") is not None and shutil.which("npm") is not None:
        ids |= {
            "frontend_vitest",
            "frontend_typecheck",
            "frontend_build",
            "g11_typecheck",
            "browser_e2e",
        }
    return ids


def _check_command_inventory(
    observed_ids: set[str], required_ids: frozenset[str]
) -> tuple[bool, dict[str, object]]:
    """Gate the observed command inventory against the FROZEN required inventory: it must
    EXACTLY equal it — an omitted command OR an additional/substitute command is rejected
    (plan §9 criterion 3). Pure/importable so the mutation tests drive it directly."""
    missing = sorted(required_ids - observed_ids)
    extra = sorted(observed_ids - required_ids)
    ok = not missing and not extra
    return ok, {
        "ok": ok,
        "missing": missing,
        "extra": extra,
        "required_count": len(required_ids),
        "observed_count": len(observed_ids),
    }


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

    # G18 (plan §9.4 / criterion 4): scan the campaign diff (BASELINE_SHA..HEAD) for
    # NEWLY-ADDED suppression directives and reject any not in the owner-approved baseline.
    suppression_baseline, baseline_note = _load_suppression_baseline(repo)
    # Force a/ b/ prefixes so the diff carries them even if this host's git has
    # diff.noprefix=true (or a noprefix alias); a prefix-less diff would otherwise leave
    # the parser's cur_file None and silently no-op the whole scan (fail-open). The parser
    # is also hardened to anchor on the `diff --git` header (see _DIFF_GIT_HEADER_RE).
    diff_text = _run(
        [
            "git",
            "diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            f"{manifest_mod.BASELINE_SHA}..HEAD",
        ],
        cwd=repo,
    ).stdout
    diff_violations = _scan_campaign_diff_suppressions(diff_text, suppression_baseline)
    violations.extend(diff_violations)

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
        "campaign_diff_suppressions": {
            "baseline_note": baseline_note,
            "baseline_rel": manifest_mod.SUPPRESSION_BASELINE_REL,
            "baseline_ratified_count": len(suppression_baseline),
            "diff_range": f"{manifest_mod.BASELINE_SHA}..HEAD",
            "new_suppression_violations": len(diff_violations),
            "scan_note": (
                "every ADDED line in the campaign diff (real code/config only; the two "
                "gate-definition scripts and the release_remediation meta-tests are "
                "exempt) is scanned for new suppressions; a hit not in the owner-approved "
                "baseline fails this lane (plan §9.4 / criterion 4)"
            ),
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
            "new_suppression_violations": len(diff_violations),
            "scanned": report["scanned"],
            "artifact": str(evidence_dir / "anti-bypass-scan.json"),
        },
    )


# ---- Docker-host-only evidence placeholders (honest, never faked) -------------


def _write_docker_host_artifacts(
    evidence_dir: Path, docker_available: bool, *, live_lifecycle_ok: bool = False
) -> dict[str, str]:
    """Write the Docker-host evidence artifacts with a TRUTHFUL status tied to the ACTUAL
    live lifecycle (G13(a) / plan §9.2). The live-production claim
    ``produced_by_live_lane_on_docker_host`` is stamped ONLY when a Docker engine is
    present AND the live lane completed a clean lifecycle (``live_lifecycle_ok``); with no
    engine the status is ``absent_no_docker_engine``, and with an engine but a failed lane
    it is ``live_lane_failed``. ``live_lifecycle_ok`` is keyword-only and defaults to a
    NOT-live value, so the frozen G13(a) call ``_write_docker_host_artifacts(dir,
    docker_available=False)`` can never emit the live claim."""
    if docker_available and live_lifecycle_ok:
        status = "produced_by_live_lane_on_docker_host"
        note = (
            "This artifact was produced by the live-docker lane on a real Docker host that "
            "completed a clean lifecycle (plan §12) — a genuine live-production record."
        )
    elif docker_available:
        status = "live_lane_failed"
        note = (
            "A Docker engine is present but the live-docker lane did NOT complete a clean "
            "lifecycle; this artifact records that failure honestly and is NOT a "
            "live-production claim."
        )
    else:
        status = "absent_no_docker_engine"
        note = (
            "No Docker engine is available on this host, so the live-docker lane could not "
            "run; this artifact is honestly marked absent, not faked, and carries no "
            "live-production claim."
        )
    written: dict[str, str] = {}
    for name in _DOCKER_HOST_ARTIFACTS:
        payload = {
            "status": status,
            "available": docker_available,
            "live_lifecycle_ok": live_lifecycle_ok,
            "note": note,
        }
        (evidence_dir / name).write_text(
            json.dumps(payload, indent=2) + "\n" if name.endswith(".json") else note + "\n",
            encoding="utf-8",
        )
        written[name] = status
    return written


# ---- evidence-hygiene scan (WO-B) ---------------------------------------------


def _scan_evidence_hygiene(evidence_dir: Path) -> tuple[bool, list[dict[str, object]]]:
    """Scan every written TEXT evidence artifact for any REGISTERED planted-credential
    sentinel and return ``(ok, violations)``. Records a typed violation — the evidence
    file NAME + the 1-based line number + the sentinel LABEL ONLY, and NEVER the
    surrounding text or the matched value — for each hit, so a leak is located without
    the report itself re-emitting the credential. Runs AFTER the evidence files are
    written and folds into ``passed`` exactly like ``frozen_ok``/``clean``, which makes
    the credential's absence enforceable and regression-proof. Reads leniently: a
    sentinel is ASCII, so a ``replace``-errors decode cannot hide it and a non-UTF-8
    artifact cannot crash the gate."""
    violations: list[dict[str, object]] = []
    for name in _HYGIENE_SCAN_FILES:
        path = evidence_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, marker in _EVIDENCE_HYGIENE_SENTINELS.items():
                if marker in line:
                    violations.append({"file": name, "line": lineno, "sentinel": label})

    def _sort_key(v: dict[str, object]) -> tuple[str, int, str]:
        line = v["line"]
        return (str(v["file"]), line if isinstance(line, int) else 0, str(v["sentinel"]))

    violations.sort(key=_sort_key)
    return (not violations), violations


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


def _final_verdict(
    *,
    frozen_ok: bool,
    all_lanes_green: bool,
    clean: bool,
    author: bool,
    hygiene_ok: bool,
    command_inventory_ok: bool = True,
) -> bool:
    """The single release-verdict conjunction: ``passed`` is true iff the frozen manifest
    verified, EVERY lane is green, the checkout is clean, this is NOT an ``--author`` run,
    the evidence-hygiene scan found no planted credential, AND the observed command
    inventory exactly equals the frozen required inventory (G19 / plan §9 criterion 3). Any
    one false forces ``passed`` false; ``--author`` can never pass."""
    return bool(
        frozen_ok
        and all_lanes_green
        and clean
        and not author
        and hygiene_ok
        and command_inventory_ok
    )


def _not_passed_reasons(
    *,
    author: bool,
    clean: bool,
    frozen_ok: bool,
    lanes: list[LaneResult],
    hygiene_ok: bool,
    command_inventory_ok: bool = True,
    command_inventory_detail: dict[str, object] | None = None,
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
    if not hygiene_ok:
        reasons.append(
            "evidence hygiene: a registered planted-credential sentinel appears in a "
            "written evidence artifact (see evidence_hygiene.violations)"
        )
    if not command_inventory_ok:
        detail = command_inventory_detail or {}
        reasons.append(
            "command inventory does not equal the frozen required inventory "
            f"(missing={detail.get('missing')} extra={detail.get('extra')})"
        )
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

    # BROWSER e2e (Firefox) runs BEFORE the frontend lane so the structured report exists
    # in evidence_dir for _run_frontend_lane to parse and gate on (plan §9.1-§9.3).
    browser_summary = _run_browser_e2e(repo, evidence_dir)

    frontend = _run_frontend_lane(repo, evidence_dir, frontend_inventory)
    lanes.append(frontend)

    # G11 dedicated compile lane (plan §9.1): a SEPARATE top-level lane so it never
    # perturbs the frozen G13(b) _run_frontend_lane behavior.
    g11 = _run_g11_compile_lane(repo, evidence_dir)
    lanes.append(g11)

    live = _run_live_lane(repo, evidence_dir)
    lanes.append(live)

    scanner = _run_scanner_lane(repo, evidence_dir)
    lanes.append(scanner)

    docker_available = tool_versions.get("docker", "unavailable") != "unavailable"
    # G13(a) / §9.2: the Docker-host artifact status is tied to the ACTUAL live lifecycle,
    # so no success-sounding status is written without a successful live lane.
    docker_host_artifacts = _write_docker_host_artifacts(
        evidence_dir, docker_available, live_lifecycle_ok=live.green is True
    )

    # G19 / §9 criterion 3: the observed command inventory must EXACTLY equal the frozen
    # required inventory (omitted OR substitute/extra commands rejected).
    observed_command_ids = _observed_command_ids(repo)
    command_inventory_ok, command_inventory_detail = _check_command_inventory(
        observed_command_ids, manifest_mod.REQUIRED_COMMAND_IDS
    )

    # EVIDENCE HYGIENE (WO-B): after every text artifact is written and BEFORE `passed`
    # is computed, prove no registered planted-credential sentinel reached the evidence.
    # Fails CLOSED and folds into `passed` exactly like `frozen_ok`/`clean` — never
    # weakening an existing gate (it can only make passing harder).
    hygiene_ok, hygiene_violations = _scan_evidence_hygiene(evidence_dir)

    all_lanes_green = all(lane.green is True for lane in lanes)
    passed = _final_verdict(
        frozen_ok=frozen_ok,
        all_lanes_green=all_lanes_green,
        clean=clean,
        author=args.author,
        hygiene_ok=hygiene_ok,
        command_inventory_ok=command_inventory_ok,
    )

    browser_written = browser_summary.get("status") in {"executed", "browser_run_failed"}
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
        "command_inventory": {
            **command_inventory_detail,
            "observed": sorted(observed_command_ids),
            "required": sorted(manifest_mod.REQUIRED_COMMAND_IDS),
            "descriptors": manifest_mod.COMMAND_INVENTORY,
        },
        "browser_e2e": browser_summary,
        "evidence_hygiene": {"ok": hygiene_ok, "violations": hygiene_violations},
        "lanes": [asdict(lane) for lane in lanes],
        "evidence_files": {
            "pytest-nonlive.xml": "written",
            "pytest-closeout.xml": "written",
            "pytest-live.xml": "written",
            "frontend-vitest.json": "written" if frontend.detail.get("npx_available") else "absent",
            "frontend-e2e.json": "written" if browser_written else "absent",
            "g11-typecheck.txt": "written",
            "docker-versions.txt": "written",
            "anti-bypass-scan.json": "written",
            **docker_host_artifacts,
        },
        "not_passed_reasons": _not_passed_reasons(
            author=args.author,
            clean=clean,
            frozen_ok=frozen_ok,
            lanes=lanes,
            hygiene_ok=hygiene_ok,
            command_inventory_ok=command_inventory_ok,
            command_inventory_detail=command_inventory_detail,
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
