#!/usr/bin/env python3
"""Candidate receipt — an enforceable, re-runnable certification that one EXACT,
operator-named, clean commit carries the Export Track-1 recovery work.

SALVAGED (owner-adjudicated 2026-07-17) from the failed campaign's repaired receipt.
The corrections it keeps:

1. IDENTITY   — the operator supplies ``--expect-head``; ``git rev-parse HEAD`` must
                match it exactly. NO commit hash is hardcoded anywhere in this script
                (a receipt cannot embed the SHA of the commit that carries it).
2. PRISTINE   — the worktree must be empty of BOTH tracked modifications AND untracked
                files (``--untracked-files=all``), so the commit IS what is on disk.
3. BINDING    — every recovery file is bound by a COMPLETE hash inventory
                (``docs/export-track1-recovery-inventory.json``). Scope is derived from
                GIT (``base..HEAD``), never a hand-kept list; MISSING / ADDITIONAL /
                STALE / CHANGED entries are all rejected.
4. EXTERNAL   — external evidence files (e.g. the 104-case adversarial probe) are
                STRICTLY OPTIONAL and NON-AUTHORITATIVE (owner policy: the probe is
                discovery evidence, not a certification oracle). When the inventory
                declares them, their bytes are SHA-256 verified so the evidence trail
                is tamper-evident — but this receipt NEVER executes them and holds NO
                expected result matrix for them.
5. ORDERING   — every gate fails CLOSED and all gates run BEFORE the proof suites
                execute; a tripped gate yields an explicit NOT-RUN record.
6. EVIDENCE   — a structured JSON receipt (``--evidence-dir``) carries the exact
                candidate SHA and per-check results.

The receipt PROVES a candidate; it does not define product policy.

Exit: 0 iff every check passes; 1 otherwise. No network. Read-only apart from the
optional evidence JSON and ``--emit-inventory`` (an authoring aid, not a proof — the
protection is human review of the inventory diff plus the operator-supplied HEAD).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INVENTORY_REL = "docs/export-track1-recovery-inventory.json"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One receipt line: a named, pass/fail assertion with human detail and the
    structured data that backs it (so evidence is not prose-only)."""

    name: str
    ok: bool
    detail: str
    data: dict[str, object] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo``, returning stripped stdout. Raises on nonzero."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def load_inventory(repo: Path) -> dict[str, object]:
    return json.loads((repo / INVENTORY_REL).read_text())


# ---------------------------------------------------------------------------
# Gate 1 — identity: HEAD is EXACTLY the operator-named commit
# ---------------------------------------------------------------------------


def check_head(repo: Path, expect_head: str) -> Check:
    """The operator names the candidate; Git is the truth. No hash is hardcoded here."""
    want = expect_head.strip().lower()
    if len(want) != 40 or not all(c in "0123456789abcdef" for c in want):
        return Check(
            "HEAD is the operator-named candidate commit",
            False,
            f"--expect-head must be a full 40-hex sha; got {expect_head!r}",
            {"expected": expect_head, "actual": None},
        )
    head = git(repo, "rev-parse", "HEAD").lower()
    return Check(
        "HEAD is the operator-named candidate commit",
        head == want,
        f"HEAD={head} (operator expected {want})",
        {"expected": want, "actual": head},
    )


# ---------------------------------------------------------------------------
# Gate 2 — pristine: no tracked modifications AND no untracked files
# ---------------------------------------------------------------------------


def check_worktree_pristine(repo: Path) -> Check:
    """A commit only certifies what is on disk if the worktree adds nothing to it.

    ``--untracked-files=all`` lists untracked files individually (not collapsed to a
    directory), so an untracked file cannot hide inside an untracked directory.
    """
    porcelain = git(repo, "status", "--porcelain", "--untracked-files=all")
    entries = [ln for ln in porcelain.splitlines() if ln.strip()]
    tracked = [ln for ln in entries if not ln.startswith("??")]
    untracked = [ln for ln in entries if ln.startswith("??")]
    return Check(
        "worktree is pristine (no tracked edits, no untracked files)",
        not entries,
        f"{len(tracked)} tracked change(s), {len(untracked)} untracked file(s)"
        + (f"; first: {entries[0]!r}" if entries else ""),
        {
            "tracked_changes": tracked,
            "untracked_files": untracked,
            "porcelain_sha256": hashlib.sha256(porcelain.encode()).hexdigest(),
        },
    )


# ---------------------------------------------------------------------------
# Gate 3 — binding: a COMPLETE hash inventory over the Git-derived recovery scope
# ---------------------------------------------------------------------------


def recovery_scope(repo: Path, base_sha: str) -> set[str]:
    """The recovery's file scope, derived from GIT (``base..HEAD``), never hand-kept.

    ``--diff-filter=d`` drops deletions (a deleted file has no on-disk bytes to bind).
    Deriving scope from Git is what makes ADDITIONAL detection real: the inventory is
    checked against the repository's own account of what the recovery changed, so it
    cannot silently under-declare.
    """
    out = git(repo, "diff", "--name-only", "--diff-filter=d", f"{base_sha}..HEAD")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def check_inventory_binding(repo: Path, inventory: dict[str, object], base_sha: str) -> list[Check]:
    """Reject MISSING, ADDITIONAL, STALE, or CHANGED entries.

    The inventory necessarily EXCLUDES ITSELF (a file cannot state its own hash). That
    is sound here because Gates 1+2 already bind every tracked byte — including the
    inventory's — to the operator-named commit.
    """
    checks: list[Check] = []
    files_obj = inventory.get("files")
    assert isinstance(files_obj, dict), "inventory 'files' must be an object"
    declared: dict[str, str] = {str(k): str(v) for k, v in files_obj.items()}

    try:
        scope = recovery_scope(repo, base_sha)
    except subprocess.CalledProcessError as exc:
        return [
            Check(
                "recovery scope derived from git",
                False,
                f"cannot derive scope from {base_sha}..HEAD: {exc.stderr.strip()}",
                {"base_sha": base_sha},
            )
        ]

    scope.discard(INVENTORY_REL)  # excludes-self (documented above)

    additional = sorted(scope - set(declared))
    stale = sorted(set(declared) - scope)
    checks.append(
        Check(
            "inventory covers the git-derived recovery scope exactly",
            not additional and not stale,
            f"{len(declared)} declared vs {len(scope)} in scope; "
            f"additional(unbound)={additional}; stale(not-in-scope)={stale}",
            {
                "declared_count": len(declared),
                "scope_count": len(scope),
                "additional_unbound": additional,
                "stale_not_in_scope": stale,
                "base_sha": base_sha,
            },
        )
    )

    missing: list[str] = []
    changed: list[dict[str, str]] = []
    for rel, expected in sorted(declared.items()):
        path = repo / rel
        if not path.is_file():
            missing.append(rel)
            continue
        actual = sha256_file(path)
        if actual != expected:
            changed.append({"path": rel, "expected": expected, "actual": actual})
    checks.append(
        Check(
            "every inventory entry exists and is byte-unchanged",
            not missing and not changed,
            f"{len(declared) - len(missing) - len(changed)}/{len(declared)} verified; "
            f"missing={missing}; changed={[c['path'] for c in changed]}",
            {"missing": missing, "changed": changed, "verified": len(declared)},
        )
    )
    return checks


# ---------------------------------------------------------------------------
# Gate 4 — OPTIONAL external evidence: SHA-256 pinned, NEVER executed
# ---------------------------------------------------------------------------


def check_external_evidence(inventory: dict[str, object]) -> list[Check]:
    """External evidence files (the 104-case probe among them) are OPTIONAL and
    NON-AUTHORITATIVE (owner policy). When the inventory declares them, their bytes
    are verified so the evidence trail is tamper-evident; when it declares none,
    that is a recorded fact, not a failure. This receipt NEVER executes external
    evidence and holds no expected result matrix for it."""
    declared = inventory.get("external_evidence")
    if not declared:
        return [
            Check(
                "external evidence (optional, non-authoritative)",
                True,
                "none declared — external evidence is optional by owner policy",
                {"declared": []},
            )
        ]
    assert isinstance(declared, list), "inventory 'external_evidence' must be a list"
    checks: list[Check] = []
    for entry in declared:
        assert isinstance(entry, dict)
        path = Path(str(entry["path"]))
        expected = str(entry["sha256"])
        if not path.is_file():
            checks.append(
                Check(
                    f"external evidence bytes pinned: {path.name}",
                    False,
                    f"declared evidence missing: {path}",
                    {"path": str(path), "expected": expected, "actual": None},
                )
            )
            continue
        actual = sha256_file(path)
        checks.append(
            Check(
                f"external evidence bytes pinned: {path.name}",
                actual == expected,
                f"{actual} (expected {expected}); NEVER executed by this receipt",
                {"path": str(path), "expected": expected, "actual": actual},
            )
        )
    return checks


def gate_checks(
    repo: Path, expect_head: str, inventory: dict[str, object], base_sha: str
) -> list[Check]:
    """Every fail-closed gate, evaluated BEFORE any execution step."""
    checks = [check_head(repo, expect_head), check_worktree_pristine(repo)]
    checks.extend(check_inventory_binding(repo, inventory, base_sha))
    checks.extend(check_external_evidence(inventory))
    return checks


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
    proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
    root = ET.parse(junit).getroot()
    ts = root if root.tag == "testsuite" else root.find("testsuite")
    assert ts is not None
    junit.unlink(missing_ok=True)
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


# ---------------------------------------------------------------------------
# Inventory authoring (convenience; NOT a proof — see module docstring)
# ---------------------------------------------------------------------------


def emit_inventory(repo: Path, base_sha: str, template: dict[str, object]) -> dict[str, object]:
    scope = sorted(recovery_scope(repo, base_sha) - {INVENTORY_REL})
    out = dict(template)
    out["base_sha"] = base_sha
    out["excludes_self"] = INVENTORY_REL
    out["files"] = {rel: sha256_file(repo / rel) for rel in scope}
    return out


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def render(checks: list[Check], head: str) -> bool:
    print("=" * 78)
    print("CANDIDATE RECEIPT — Export Track-1 recovery")
    print(f"CANDIDATE COMMIT: {head}")
    print("=" * 78)
    all_ok = True
    for c in checks:
        all_ok &= c.ok
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}\n         {c.detail}")
    print("=" * 78)
    print(f"RECEIPT: {'GREEN — all checks exact' if all_ok else 'RED — one or more checks failed'}")
    return all_ok


def write_evidence(path: Path, head: str, base_sha: str, checks: list[Check], green: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "export-track1-recovery-receipt/v1",
                "candidate_sha": head,
                "base_sha": base_sha,
                "green": green,
                "checks": [
                    {"name": c.name, "ok": c.ok, "detail": c.detail, "data": c.data} for c in checks
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main(argv: list[str] | None = None, repo: Path | None = None) -> int:
    """``repo`` is injectable so the mutation suite can drive the REAL gate logic against
    a synthetic repository — no patching of this module's globals, no test-only branch."""
    repo = repo or REPO
    parser = argparse.ArgumentParser(description="Export Track-1 recovery candidate receipt.")
    parser.add_argument(
        "--expect-head",
        required=True,
        help="REQUIRED: the exact 40-hex candidate commit the operator intends to certify. "
        "No SHA is hardcoded in this script; Git is compared against this value.",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Recovery base for GIT-DERIVED scope (default: inventory 'base_sha'). This is "
        "NOT an expected HEAD — it only defines which files the recovery touched.",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Interpreter for the proof suites (default: <repo>/.venv/bin/python3).",
    )
    parser.add_argument(
        "--evidence-dir", default=None, help="Write a structured JSON receipt here."
    )
    parser.add_argument(
        "--emit-inventory",
        action="store_true",
        help="Regenerate the inventory from the tree (authoring aid, not a proof).",
    )
    args = parser.parse_args(argv)

    inventory = load_inventory(repo)
    base_sha = args.base or str(inventory["base_sha"])
    python = Path(args.python) if args.python else repo / ".venv" / "bin" / "python3"

    if args.emit_inventory:
        fresh = emit_inventory(repo, base_sha, inventory)
        (repo / INVENTORY_REL).write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        files_map = fresh["files"]
        assert isinstance(files_map, dict)
        print(f"wrote {INVENTORY_REL} ({len(files_map)} files)")
        return 0

    # ---- Gates first: every one fails CLOSED, and none of them execute anything. ----
    checks = gate_checks(repo, args.expect_head, inventory, base_sha)
    head = git(repo, "rev-parse", "HEAD")

    if all(c.ok for c in checks):
        # Gates green -> the tree is the named commit, complete, and any declared
        # external evidence is byte-verified. Only now is it safe to execute.
        checks.append(run_proof_tests(repo, inventory, python))
    else:
        checks.append(
            Check(
                "execution step (proof suites)",
                False,
                "NOT RUN — a fail-closed gate above did not pass; the receipt refuses to "
                "execute (and thus cannot report a green result) on an unverified tree.",
                {"executed": False},
            )
        )

    green = render(checks, head)
    if args.evidence_dir:
        write_evidence(
            Path(args.evidence_dir) / "candidate-receipt.json", head, base_sha, checks, green
        )
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
