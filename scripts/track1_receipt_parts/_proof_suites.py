"""Proof-suite gating and execution — private implementation for
``export_track1_candidate_receipt``.

Everything a proof suite needs, in the order it needs it: the shared ``Check``
receipt-line primitive, the gate that proves a pinned proof suite is
repo-relative, inventory-bound, and never a declared external-evidence path
(``check_proof_suite_binding``), and the execution step that actually runs the
pinned suites and parses their structured JUnit XML output
(``run_proof_tests``). Extracted from ``export_track1_candidate_receipt.py`` to
reduce module size and callable complexity; the parent module re-imports these
names unchanged so every existing module-level name still resolves as an
attribute of it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Check:
    """One receipt line: a named, pass/fail assertion with human detail and the
    structured data that backs it (so evidence is not prose-only)."""

    name: str
    ok: bool
    detail: str
    data: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gate 4b — proof suites are repo-relative, inventory-bound, never evidence
# ---------------------------------------------------------------------------


def _external_evidence_paths(inventory: dict[str, object]) -> set[Path]:
    """Resolved absolute paths of every declared external-evidence entry.

    Best-effort: a malformed entry is skipped here — its own validity is
    checked by ``check_external_evidence``, not by this proof-suite gate.
    """
    paths: set[Path] = set()
    for entry in inventory.get("external_evidence") or []:
        if not isinstance(entry, dict):
            continue
        try:
            paths.add(Path(str(entry["path"])).resolve())
        except OSError:
            pass
    return paths


def _proof_suite_path_problem(
    rel: str,
    *,
    repo: Path,
    repo_root: Path,
    evidence_paths: set[Path],
    declared: set[str],
) -> str | None:
    """The one problem (if any) with a single pinned proof-suite path."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return f"not repo-relative: {rel}"
    resolved = (repo / p).resolve()
    if not resolved.is_relative_to(repo_root):
        return f"escapes the repo: {rel}"
    if resolved in evidence_paths:
        return f"names declared external evidence (never executable): {rel}"
    if not resolved.is_file():
        return f"missing: {rel}"
    if rel not in declared:
        return f"not bound by the inventory: {rel}"
    return None


def check_proof_suite_binding(repo: Path, inventory: dict[str, object]) -> Check:
    """Every pinned proof suite must be a repo-relative, inventory-BOUND file that is
    NOT a declared external-evidence path.

    Independent verification demonstrated a GREEN bypass: ``expected_proof.tests``
    accepted arbitrary paths, so the byte-pinned-but-never-executed external probe
    (or any unpinned out-of-repo file) could be handed to pytest and executed. Proof
    suites must therefore live inside the repo AND inside the hash inventory."""
    expected = inventory.get("expected_proof")
    assert isinstance(expected, dict), "inventory 'expected_proof' must be an object"
    tests_obj = expected.get("tests")
    assert isinstance(tests_obj, list), "expected_proof 'tests' must be a list"
    files_obj = inventory.get("files")
    assert isinstance(files_obj, dict)
    declared = {str(k) for k in files_obj}
    evidence_paths = _external_evidence_paths(inventory)
    repo_root = repo.resolve()
    problems: list[str] = []
    if not tests_obj:
        problems.append("expected_proof.tests is EMPTY — a vacuous proof proves nothing")
    for raw in tests_obj:
        problem = _proof_suite_path_problem(
            str(raw),
            repo=repo,
            repo_root=repo_root,
            evidence_paths=evidence_paths,
            declared=declared,
        )
        if problem is not None:
            problems.append(problem)
    return Check(
        "proof suites are repo-relative, inventory-bound, and never external evidence",
        not problems,
        (
            f"{len(tests_obj)} pinned suite(s); problems={problems}"
            if problems
            else f"{len(tests_obj)} pinned suite(s), all bound"
        ),
        {"problems": problems, "tests": [str(t) for t in tests_obj]},
    )


# ---------------------------------------------------------------------------
# Execution step (reached ONLY when every gate passed)
# ---------------------------------------------------------------------------


def run_proof_tests(repo: Path, inventory: dict[str, object], python: Path) -> Check:
    """Run the pinned proof suites — exact counts required.

    Parse the structured junit XML (``-q`` terminal scraping is unreliable); the exit
    code is checked too — a suite that errors during collection can still emit XML.
    """
    expected = inventory.get("expected_proof")
    assert isinstance(expected, dict)
    tests_obj = expected["tests"]
    assert isinstance(tests_obj, list), "expected_proof 'tests' must be a list"
    tests = [str(t) for t in tests_obj]
    want_passed = int(str(expected["passed"]))
    want_failed = int(str(expected["failed"]))

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as jf:
        junit = Path(jf.name)
    argv = [
        str(python),
        "-m",
        "pytest",
        *tests,
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={junit}",
    ]
    proof_env = os.environ.copy()
    # The receipt certifies the candidate's pinned proof command, not whatever
    # warning policy happened to wrap the receipt itself. In particular, an
    # outer Werror run must not turn a third-party plugin warning in a synthetic
    # proof checkout into a pytest startup crash before JUnit can be written.
    proof_env.pop("PYTHONWARNINGS", None)
    proc = subprocess.run(argv, cwd=repo, env=proof_env, capture_output=True, text=True)
    try:
        root = ET.parse(junit).getroot()
        ts = root if root.tag == "testsuite" else root.find("testsuite")
    except ET.ParseError:
        ts = None
    junit.unlink(missing_ok=True)
    if ts is None:
        # A crashed proof process (SIGSEGV, OOM-kill) leaves no parseable junit; that
        # is a RED check with structured evidence, never an unhandled traceback.
        return Check(
            f"proof suites == {want_passed} passed / {want_failed} failed",
            False,
            f"proof run produced no parseable junit (pytest exit {proc.returncode}); "
            "the run crashed or was killed — refusing to infer any result",
            {"argv": argv[1:], "returncode": proc.returncode, "junit": None},
        )
    total = int(ts.get("tests", "-1"))
    failed = int(ts.get("failures", "-1"))
    errors = int(ts.get("errors", "-1"))
    skipped = int(ts.get("skipped", "-1"))
    passed = total - failed - errors - skipped
    ok = (
        passed == want_passed
        and failed == want_failed
        and errors == 0
        and skipped == 0
        and proc.returncode == 0
    )
    return Check(
        f"proof suites == {want_passed} passed / {want_failed} failed",
        ok,
        f"{passed} passed, {failed} failed, {errors} errors, {skipped} skipped "
        f"(junit tests={total}), pytest exit {proc.returncode}",
        {
            "argv": argv[1:],
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "junit_tests": total,
        },
    )
