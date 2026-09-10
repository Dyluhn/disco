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
                (``development/notes/export-track1-recovery-inventory.json``). Scope is derived from
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
from pathlib import Path

# Proof-suite gating and execution live in a private sibling package so this
# entry point stays under the module size budget.  ``development/scripts/`` is this file's
# own directory, so the import resolves under the real invocation
# (``python development/scripts/export_track1_candidate_receipt.py``), which puts it on
# ``sys.path[0]``.
from track1_receipt_parts._proof_suites import Check, check_proof_suite_binding, run_proof_tests

REPO = Path(__file__).resolve().parent.parent
INVENTORY_REL = "development/notes/export-track1-recovery-inventory.json"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
#
# ``Check`` — the receipt-line primitive — lives in ``track1_receipt_parts``
# alongside the proof-suite gate and execution step that construct most of
# them; imported above so it still resolves as
# ``export_track1_candidate_receipt.Check`` everywhere it always has.


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


def check_base_sha(repo: Path, base_sha: str) -> Check:
    """The scope base must be a REAL, exact, 40-hex commit distinct from HEAD.

    Independent verification demonstrated a GREEN bypass: a symbolic base (``HEAD``)
    makes the git-derived range empty, so an empty inventory 'covers' it while
    binding NOTHING. Fail closed on anything but a resolvable full sha that is not
    HEAD itself."""
    want = base_sha.strip().lower()
    if len(want) != 40 or not all(c in "0123456789abcdef" for c in want):
        return Check(
            "scope base is an exact 40-hex commit",
            False,
            f"base_sha must be a full 40-hex sha (symbolic refs are a bypass); got {base_sha!r}",
            {"base_sha": base_sha},
        )
    try:
        resolved = git(repo, "rev-parse", f"{want}^{{commit}}").lower()
    except subprocess.CalledProcessError:
        return Check(
            "scope base is an exact 40-hex commit",
            False,
            f"base_sha does not resolve to a commit: {want}",
            {"base_sha": want},
        )
    head = git(repo, "rev-parse", "HEAD").lower()
    ok = resolved == want and resolved != head
    return Check(
        "scope base is an exact 40-hex commit",
        ok,
        f"base={want} resolved={resolved} head={head}"
        + ("" if ok else " — base must resolve to itself and differ from HEAD"),
        {"base_sha": want, "resolved": resolved, "head": head},
    )


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
    """The recovery's on-disk file scope, derived from GIT (``base..HEAD``).

    ``--diff-filter=d`` drops DELETIONS (a deleted file has no on-disk bytes to bind);
    deletions are bound SEPARATELY by ``recovery_deletions`` + a tombstone list, so an
    undeclared deletion can never slip through the inventory silently (C9-06). Deriving
    scope from Git is what makes ADDITIONAL detection real: the inventory is checked
    against the repository's own account of what the recovery changed, so it cannot
    silently under-declare.
    """
    out = git(repo, "diff", "--name-only", "--diff-filter=d", f"{base_sha}..HEAD")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def recovery_deletions(repo: Path, base_sha: str) -> set[str]:
    """The paths DELETED between ``base`` and HEAD (``--diff-filter=D``)."""
    out = git(repo, "diff", "--name-only", "--diff-filter=D", f"{base_sha}..HEAD")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def interpreter_identity() -> dict[str, object]:
    """The ACTUAL interpreter running this receipt — path, realpath, version, and (when
    readable) SHA-256 (C9-06: no ``--python`` override, so the recorded interpreter is
    the one that runs the proof suites)."""
    exe = Path(sys.executable)
    real = exe.resolve()
    sha = None
    try:
        sha = hashlib.sha256(real.read_bytes()).hexdigest()
    except OSError:
        sha = None
    return {
        "executable": str(exe),
        "realpath": str(real),
        "version": sys.version.split()[0],
        "sha256": sha,
    }


def check_base_ancestry(repo: Path, base_sha: str) -> Check:
    """The scope base must be an ANCESTOR of HEAD (C9-06): a base that is not an
    ancestor cannot define a meaningful ``base..HEAD`` scope."""
    try:
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", base_sha, "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return Check(
            "scope base is an ancestor of HEAD",
            False,
            f"base {base_sha} is not an ancestor of HEAD",
            {"base_sha": base_sha},
        )
    return Check(
        "scope base is an ancestor of HEAD", True, f"{base_sha} is an ancestor of HEAD", {}
    )


def check_no_undeclared_deletions(repo: Path, inventory: dict[str, object], base_sha: str) -> Check:
    """Every path deleted in ``base..HEAD`` must be declared as an explicit tombstone in
    the inventory's ``deletions`` list (C9-06): an undeclared deletion — a file the
    recovery removed without recording it — fails closed."""
    declared = inventory.get("deletions", [])
    declared_set = {str(x) for x in declared} if isinstance(declared, list) else set()
    actual = recovery_deletions(repo, base_sha)
    undeclared = sorted(actual - declared_set)
    stale = sorted(declared_set - actual)
    ok = not undeclared and not stale
    return Check(
        "deletions are declared exactly (no undeclared/stale tombstones)",
        ok,
        f"deleted={sorted(actual)}; undeclared={undeclared}; stale_tombstones={stale}",
        {"deleted": sorted(actual), "undeclared": undeclared, "stale": stale},
    )


def check_evidence_dir_outside(evidence_dir: Path | None, repo: Path) -> Check:
    """``--evidence-dir`` must be an ABSOLUTE path OUTSIDE the checkout (C9-06): writing
    the receipt inside the tree would dirty the very worktree the receipt certifies."""
    if evidence_dir is None:
        return Check(
            "evidence dir is absolute and outside the checkout",
            True,
            "no --evidence-dir supplied (nothing written into the tree)",
            {},
        )
    resolved = evidence_dir.resolve()
    repo_resolved = repo.resolve()
    inside = resolved == repo_resolved or repo_resolved in resolved.parents
    # Reject a SYMLINK evidence dir (or a symlinked ancestor): a link checked as external
    # here can be retargeted inside the repo before the write (TOCTOU). The receipt writes
    # to the RESOLVED real path captured at gate time, and refuses a link outright (C9-06
    # P5-2).
    is_link = evidence_dir.is_symlink() or any(p.is_symlink() for p in evidence_dir.parents)
    ok = evidence_dir.is_absolute() and not inside and not is_link
    return Check(
        "evidence dir is absolute, outside the checkout, and not a symlink",
        ok,
        f"evidence_dir={evidence_dir} "
        f"(absolute={evidence_dir.is_absolute()}, inside_repo={inside}, symlink={is_link})",
        {"evidence_dir": str(evidence_dir), "inside_repo": inside, "symlink": is_link},
    )


def check_interpreter_trust(
    inventory: dict[str, object],
    interpreter: dict[str, object],
    cli_trusted: str | None,
) -> Check:
    """A trust root MUST be supplied and the ACTUAL running interpreter's realpath sha256
    MUST equal it (C9-06 P5-3, owner round): recording whatever interpreter ran the
    receipt is not proof — an attacker-controlled launching interpreter can fabricate
    proof output. The trust root is the operator-supplied ``--trusted-interpreter-sha256``
    (preferred) or the inventory's ``trusted_interpreter_sha256``. With NEITHER supplied,
    the certification FAILS CLOSED (no more green-with-untrusted-interpreter)."""
    expected = cli_trusted or inventory.get("trusted_interpreter_sha256")
    actual = interpreter.get("sha256")
    if not expected:
        return Check(
            "interpreter matches an explicit operator trust root",
            False,
            "NO trust root supplied — pass --trusted-interpreter-sha256 <hex> (or declare "
            "trusted_interpreter_sha256 in the inventory). Recording the interpreter is "
            f"not trust; the running realpath is {interpreter.get('realpath')} "
            f"(sha256={actual}).",
            {"interpreter": interpreter, "trust_root": None},
        )
    ok = isinstance(expected, str) and bool(actual) and actual == expected
    return Check(
        "interpreter matches an explicit operator trust root",
        ok,
        f"actual={actual} expected={expected}"
        + ("" if ok else " — the running interpreter is NOT the trusted one"),
        {"interpreter": interpreter, "trust_root": expected},
    )


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

    checks.append(
        Check(
            "recovery scope is non-empty",
            bool(scope),
            f"{len(scope)} file(s) in {base_sha}..HEAD"
            + ("" if scope else " — an empty scope binds nothing and certifies nothing"),
            {"scope_count": len(scope)},
        )
    )
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
        if not path.is_absolute():
            checks.append(
                Check(
                    f"external evidence bytes pinned: {path.name}",
                    False,
                    f"external evidence must be an ABSOLUTE path (it lives outside the "
                    f"repo by definition); got {path}",
                    {"path": str(path), "expected": expected, "actual": None},
                )
            )
            continue
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


# ``check_proof_suite_binding`` (the proof-suite half of Gate 4, decomposed to
# clear callable complexity) lives in ``track1_receipt_parts`` alongside
# ``run_proof_tests``, the execution step that runs what it gates; imported
# above.


def gate_checks(
    repo: Path,
    expect_head: str,
    inventory: dict[str, object],
    base_sha: str,
    evidence_dir: Path | None,
    cli_trusted_interpreter: str | None = None,
) -> list[Check]:
    """Every fail-closed gate, evaluated BEFORE any execution step."""
    checks = [
        check_head(repo, expect_head),
        check_base_sha(repo, base_sha),
        check_base_ancestry(repo, base_sha),
        check_worktree_pristine(repo),
        check_evidence_dir_outside(evidence_dir, repo),
        check_interpreter_trust(inventory, interpreter_identity(), cli_trusted_interpreter),
    ]
    checks.extend(check_inventory_binding(repo, inventory, base_sha))
    checks.append(check_no_undeclared_deletions(repo, inventory, base_sha))
    checks.extend(check_external_evidence(inventory))
    checks.append(check_proof_suite_binding(repo, inventory))
    return checks


def post_run_rechecks(
    repo: Path, expect_head: str, inventory: dict[str, object], base_sha: str
) -> list[Check]:
    """After the proof suites execute, RE-VERIFY that nothing shifted underfoot
    (C9-06): HEAD is still the operator-named commit, the worktree is still pristine,
    the inventory bytes are still bound, and the git-derived scope is unchanged. Each is
    named ``post-run: …`` so the receipt records both the pre-run gate and its post-run
    counterpart."""
    checks = [
        Check(
            "post-run: HEAD unchanged",
            check_head(repo, expect_head).ok,
            "re-checked after proof execution",
            {},
        ),
        Check(
            "post-run: worktree still pristine",
            check_worktree_pristine(repo).ok,
            "re-checked after proof execution",
            {},
        ),
    ]
    binding = check_inventory_binding(repo, inventory, base_sha)
    checks.append(
        Check(
            "post-run: inventory bytes + scope unchanged",
            all(c.ok for c in binding),
            "re-checked the complete inventory binding after proof execution",
            {},
        )
    )
    # C9-06 P5-1: re-pin the EXTERNAL evidence too — a hash-bound proof could overwrite it
    # between the pre-run gate and now.
    external = check_external_evidence(inventory)
    checks.append(
        Check(
            "post-run: external evidence still byte-pinned",
            all(c.ok for c in external),
            "re-verified declared external evidence after proof execution",
            {},
        )
    )
    return checks


# ---------------------------------------------------------------------------
# Execution step (reached ONLY when every gate passed)
# ---------------------------------------------------------------------------
#
# ``run_proof_tests`` (junit XML parsing) lives in ``track1_receipt_parts``
# alongside ``check_proof_suite_binding`` — the gate that validates a proof
# suite before it is ever executed here; imported above.


# ---------------------------------------------------------------------------
# Inventory authoring (convenience; NOT a proof — see module docstring)
# ---------------------------------------------------------------------------


def emit_inventory(repo: Path, base_sha: str, template: dict[str, object]) -> dict[str, object]:
    scope = sorted(recovery_scope(repo, base_sha) - {INVENTORY_REL})
    out = dict(template)
    out["base_sha"] = base_sha
    out["excludes_self"] = INVENTORY_REL
    out["files"] = {rel: sha256_file(repo / rel) for rel in scope}
    # Explicit deletion tombstones (C9-06): every path removed in base..HEAD, so the
    # certification's no-undeclared-deletion gate has an exact declared set.
    out["deletions"] = sorted(recovery_deletions(repo, base_sha) - {INVENTORY_REL})
    # Declare the authoring interpreter's realpath sha256 as the trust root (C9-06 P5-3,
    # owner round): the operator reviews this hash and the certification refuses to run
    # green under any other interpreter. The operator may override it with
    # --trusted-interpreter-sha256 at certification time.
    out["trusted_interpreter_sha256"] = interpreter_identity().get("sha256")
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


def write_evidence(
    path: Path,
    head: str,
    base_sha: str,
    checks: list[Check],
    green: bool,
    *,
    interpreter: dict[str, object] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema": "export-track1-recovery-receipt/v2",
        "candidate_sha": head,
        "base_sha": base_sha,
        "green": green,
        "interpreter": interpreter or {},
        "pre_run_checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "data": c.data}
            for c in checks
            if not c.name.startswith("post-run:")
        ],
        "post_run_checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "data": c.data}
            for c in checks
            if c.name.startswith("post-run:")
        ],
        "checks": [
            {"name": c.name, "ok": c.ok, "detail": c.detail, "data": c.data} for c in checks
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
        help="AUTHORING ONLY (with --emit-inventory): the base for git-derived scope. In "
        "the CERTIFICATION path the base is ALWAYS the inventory's frozen 'base_sha' and "
        "this flag is rejected — a candidate cannot re-scope itself.",
    )
    parser.add_argument(
        "--evidence-dir",
        default=None,
        help="Write a structured JSON receipt here (MUST be an absolute path OUTSIDE the "
        "checkout).",
    )
    parser.add_argument(
        "--emit-inventory",
        action="store_true",
        help="AUTHORING: regenerate the inventory from the tree (never emits a receipt).",
    )
    parser.add_argument(
        "--trusted-interpreter-sha256",
        default=None,
        help="REQUIRED for a GREEN certification unless the inventory declares "
        "'trusted_interpreter_sha256': the sha256 of the trusted interpreter's realpath. "
        "The receipt refuses to certify green under any other interpreter (the recorded "
        "identity alone is not trust).",
    )
    args = parser.parse_args(argv)

    inventory = load_inventory(repo)

    if args.emit_inventory:
        # Authoring is VISIBLY separate from certification: it writes the inventory and
        # exits 0 WITHOUT ever running the gates, executing the proof suites, or writing a
        # certification receipt — it can never emit a green certification (C9-06).
        base_sha = args.base or str(inventory["base_sha"])
        fresh = emit_inventory(repo, base_sha, inventory)
        (repo / INVENTORY_REL).write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        files_map = fresh["files"]
        assert isinstance(files_map, dict)
        print(f"wrote {INVENTORY_REL} ({len(files_map)} files) [authoring — NOT a receipt]")
        return 0

    # Certification: the base is the inventory's frozen base — never operator-overridable.
    if args.base is not None:
        print(
            "--base is authoring-only; the certification path always uses the inventory's "
            "frozen base_sha (refusing to re-scope the candidate).",
            file=sys.stderr,
        )
        return 2
    base_sha = str(inventory["base_sha"])
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None
    interpreter = interpreter_identity()
    # Capture the RESOLVED real evidence directory at gate time and check the location
    # gate independently, so the receipt can (a) only ever write to the vetted real path
    # and (b) refuse to write at all when the location gate failed (C9-06 P5-2). The
    # symlink cannot be retargeted between gate and write because we never re-follow it.
    evidence_target = evidence_dir.resolve() if evidence_dir is not None else None
    evidence_location_ok = evidence_dir is None or check_evidence_dir_outside(evidence_dir, repo).ok

    # ---- Gates first: every one fails CLOSED, and none of them execute anything. ----
    checks = gate_checks(
        repo,
        args.expect_head,
        inventory,
        base_sha,
        evidence_dir,
        args.trusted_interpreter_sha256,
    )
    head = git(repo, "rev-parse", "HEAD")

    if all(c.ok for c in checks):
        # Gates green -> the tree is the named commit, complete, and any declared
        # external evidence is byte-verified. Only now is it safe to execute, with the
        # ACTUAL running interpreter (no override).
        checks.append(run_proof_tests(repo, inventory, Path(sys.executable)))
        # Post-run: prove nothing shifted underfoot during execution (C9-06).
        checks.extend(post_run_rechecks(repo, args.expect_head, inventory, base_sha))
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
    # Write evidence ONLY to the vetted resolved real path, and ONLY when the location
    # gate passed — never through a (possibly retargeted) symlink or into the checkout,
    # on the green OR the red path (C9-06 P5-2).
    if evidence_target is not None and evidence_location_ok:
        write_evidence(
            evidence_target / "candidate-receipt.json",
            head,
            base_sha,
            checks,
            green,
            interpreter=interpreter,
        )
    elif evidence_dir is not None:
        print(
            "evidence NOT written: the --evidence-dir location gate failed (symlink or "
            "inside the checkout); refusing to write a receipt there.",
            file=sys.stderr,
        )
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
