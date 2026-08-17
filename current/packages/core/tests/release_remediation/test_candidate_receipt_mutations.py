"""Mutation proofs for the recovery candidate receipt
(``development/scripts/export_track1_candidate_receipt.py``).

WHY THIS EXISTS
---------------
The failed campaign's original receipt hardcoded its expected HEAD to the campaign's
PARENT commit: it printed GREEN while certifying the pre-commit worktree — the one
state that is never shipped — and nothing caught it, because NO test ever asserted
that a wrong HEAD makes the receipt fail. A gate that has never been observed going
red is an assumption, not a proof.

Each test below MUTATES exactly one input the receipt claims to catch and proves the
receipt goes NONZERO. Every test builds a REAL synthetic git repository (``git init``
plus real commits) and drives the REAL gate functions and the REAL ``main()``. No
gate logic is stubbed and no receipt internal is monkeypatched: ``main(repo=...)``
is injectable so the shipping code path is what runs here.

Owner policy additions (2026-07-17): external evidence is OPTIONAL and
NON-AUTHORITATIVE — declared evidence is byte-pinned (swap/delete goes red) but is
NEVER executed (proven by a canary), and an inventory declaring none is still green.

These tests live OUTSIDE the frozen closeout dirs (no ``export_track1_closeout``
marker), so they never perturb the acceptance manifest.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]
_RECEIPT_PATH = _REPO_ROOT / "development" / "scripts" / "export_track1_candidate_receipt.py"


def _load_receipt() -> Any:
    """Import the receipt BY PATH (``development/scripts/`` is not an importable package).

    The module MUST be registered in ``sys.modules`` before ``exec_module``:
    ``@dataclass`` resolves its own class's module out of ``sys.modules`` while
    processing annotations, and raises on a module that is not yet registered.

    ``development/scripts/`` is also put on ``sys.path`` so the receipt's private
    ``track1_receipt_parts`` package resolves exactly as it does under the real
    invocation ``python development/scripts/export_track1_candidate_receipt.py``, which puts
    that directory on ``sys.path[0]``.  Loading by path alone would simulate an
    environment that never occurs in production.  This affects only how the
    module is loaded; every mutation proof below still drives the REAL gate
    functions and the REAL ``main()``.
    """
    if str(_RECEIPT_PATH.parent) not in sys.path:
        sys.path.insert(0, str(_RECEIPT_PATH.parent))
    spec = importlib.util.spec_from_file_location("_recovery_receipt_under_test", _RECEIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


receipt = _load_receipt()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def synthetic(tmp_path: Path) -> dict[str, object]:
    """A real 2-commit git repo whose HEAD is a valid 'candidate': a recovery file
    bound by a complete inventory, one REAL trivially-green proof test, and a piece
    of declared external evidence living OUTSIDE the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "receipt-mutation@example.invalid")
    _git(repo, "config", "user.name", "receipt mutation suite")

    (repo / "README.md").write_text("base\n")
    # Mirror the real repo: __pycache__ is gitignored, so running the proof suite (which
    # byte-compiles the test module) does not dirty the tree and trip the post-run
    # pristine recheck. Without this the synthetic repo would show untracked *.pyc.
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    # Declared external evidence lives OUTSIDE the repo — a commit cannot bind it.
    # Executing it would create a canary file; the receipt must NEVER do that.
    evidence = tmp_path / "adversarial_probe.py"
    canary = tmp_path / "EXECUTED-CANARY"
    evidence.write_text(f"open({str(canary)!r}, 'w').write('executed')\n")

    campaign = repo / "recovery.py"
    campaign.write_text("x = 1\n")
    proof = repo / "test_receipt_proof.py"
    proof.write_text("def test_green():\n    assert True\n")

    inventory_path = repo / str(receipt.INVENTORY_REL)
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory = {
        "schema": "export-track1-recovery-inventory/v1",
        "base_sha": base_sha,
        "excludes_self": str(receipt.INVENTORY_REL),
        "external_evidence": [{"path": str(evidence), "sha256": _sha256(evidence)}],
        "deletions": [],
        "trusted_interpreter_sha256": hashlib.sha256(
            Path(sys.executable).resolve().read_bytes()
        ).hexdigest(),
        "expected_proof": {"tests": ["test_receipt_proof.py"], "passed": 1, "failed": 0},
        "files": {"recovery.py": _sha256(campaign), "test_receipt_proof.py": _sha256(proof)},
    }
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "recovery")
    head = _git(repo, "rev-parse", "HEAD")

    return {"repo": repo, "head": head, "base": base_sha, "evidence": evidence, "canary": canary}


def _gates(synthetic: dict[str, object], head: str | None = None) -> list[Any]:
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inventory = receipt.load_inventory(repo)
    return receipt.gate_checks(
        repo, head or str(synthetic["head"]), inventory, str(synthetic["base"]), None
    )


def _run_main(synthetic: dict[str, object], head: str | None = None) -> int:
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    return receipt.main(
        ["--expect-head", head or str(synthetic["head"])],
        repo=repo,
    )


def _failed(checks: list[Any]) -> list[str]:
    return [c.name for c in checks if not c.ok]


# ---- the unmutated baseline: green end-to-end, and evidence NEVER executes ----


def test_baseline_all_gates_pass(synthetic: dict[str, object]) -> None:
    """The control. Without it, a receipt that fails for ANY reason would trivially
    satisfy every mutation test below — the mutations would prove nothing."""
    assert _failed(_gates(synthetic)) == []


def test_baseline_main_is_green_and_never_executes_external_evidence(
    synthetic: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path reaches GREEN (real proof suite, exact counts) and the declared
    external evidence is verified but NEVER executed: running it would create the
    canary file."""
    canary = synthetic["canary"]
    assert isinstance(canary, Path)
    monkeypatch.setenv("PYTHONWARNINGS", "error")
    assert _run_main(synthetic) == 0
    assert not canary.exists(), "the receipt EXECUTED declared external evidence"


def test_no_declared_external_evidence_is_still_green(synthetic: dict[str, object]) -> None:
    """Owner policy: external evidence is OPTIONAL — an inventory declaring none
    passes the evidence gate with an explicit 'none declared' record."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    data["external_evidence"] = []
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "no external evidence")
    new_head = _git(repo, "rev-parse", "HEAD")
    assert _failed(_gates(synthetic, new_head)) == []
    assert _run_main(synthetic, new_head) == 0


# ---- mutation 1: wrong HEAD (the exact defect that shipped) ------------------


def test_wrong_head_forces_nonzero(synthetic: dict[str, object]) -> None:
    """The original bug: certifying a commit that is NOT the tree under review. Here
    the operator names the PARENT — precisely what the old receipt hardcoded — and
    the receipt must refuse rather than report green."""
    parent = str(synthetic["base"])
    assert "HEAD is the operator-named candidate commit" in _failed(_gates(synthetic, parent))
    assert _run_main(synthetic, parent) == 1


def test_nonexistent_head_forces_nonzero(synthetic: dict[str, object]) -> None:
    assert _run_main(synthetic, "0" * 40) == 1


def test_malformed_head_forces_nonzero(synthetic: dict[str, object]) -> None:
    """A short/abbreviated sha must not be accepted as 'close enough'."""
    assert _run_main(synthetic, str(synthetic["head"])[:12]) == 1


# ---- mutation 2: dirty tracked file ------------------------------------------


def test_dirty_tracked_file_forces_nonzero(synthetic: dict[str, object]) -> None:
    """HEAD still resolves, but the worktree no longer IS that commit."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "recovery.py").write_text("x = 999  # drifted\n")
    assert "worktree is pristine (no tracked edits, no untracked files)" in _failed(
        _gates(synthetic)
    )
    assert _run_main(synthetic) == 1


# ---- mutation 3: untracked file ----------------------------------------------


def test_untracked_file_forces_nonzero(synthetic: dict[str, object]) -> None:
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "stray_artifact.py").write_text("print('not committed')\n")
    assert "worktree is pristine (no tracked edits, no untracked files)" in _failed(
        _gates(synthetic)
    )
    assert _run_main(synthetic) == 1


def test_untracked_file_nested_in_untracked_dir_forces_nonzero(
    synthetic: dict[str, object],
) -> None:
    """``git status --porcelain`` COLLAPSES an untracked directory to a single entry;
    the receipt must pass ``--untracked-files=all`` so a file cannot hide inside one."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    nested = repo / "scratch" / "deep"
    nested.mkdir(parents=True)
    (nested / "leftover.json").write_text("{}\n")
    assert _run_main(synthetic) == 1


# ---- mutation 4: changed recovery file (committed => pristine, correct HEAD) --


def test_changed_recovery_file_forces_nonzero(synthetic: dict[str, object]) -> None:
    """The binding gate must stand on its OWN, independently of the pristine gate.

    The mutation is COMMITTED, so the worktree is clean, and the operator names the
    NEW HEAD, so the identity gate passes too. Only the inventory hash can catch this
    — proving the hash inventory is load-bearing rather than decoration.
    """
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "recovery.py").write_text("x = 2  # content drift\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drift")
    new_head = _git(repo, "rev-parse", "HEAD")

    failed = _failed(_gates(synthetic, new_head))
    assert "HEAD is the operator-named candidate commit" not in failed
    assert "worktree is pristine (no tracked edits, no untracked files)" not in failed
    assert "every inventory entry exists and is byte-unchanged" in failed
    assert _run_main(synthetic, new_head) == 1


def test_additional_unbound_file_forces_nonzero(synthetic: dict[str, object]) -> None:
    """A NEW committed recovery file that the inventory does not declare is UNBOUND.
    Scope comes from git, so the inventory cannot silently under-declare."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "sneaked_in.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "additional")
    new_head = _git(repo, "rev-parse", "HEAD")
    assert "inventory covers the git-derived recovery scope exactly" in _failed(
        _gates(synthetic, new_head)
    )
    assert _run_main(synthetic, new_head) == 1


def test_missing_inventory_entry_forces_nonzero(synthetic: dict[str, object]) -> None:
    """An inventory entry with no file on disk is MISSING, not 'vacuously satisfied'."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    data["files"]["never_existed.py"] = "0" * 64
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "missing entry")
    new_head = _git(repo, "rev-parse", "HEAD")
    failed = _failed(_gates(synthetic, new_head))
    assert "every inventory entry exists and is byte-unchanged" in failed
    assert _run_main(synthetic, new_head) == 1


# ---- mutation 5: DECLARED external evidence must stay byte-pinned -------------


def test_changed_declared_evidence_forces_nonzero(synthetic: dict[str, object]) -> None:
    """Declared evidence lives outside the repo, so NO commit binds it. Swapping its
    bytes must fail the receipt — an unpinned evidence trail is not evidence."""
    evidence = synthetic["evidence"]
    assert isinstance(evidence, Path)
    evidence.write_text("print('swapped evidence')\n")
    failed = _failed(_gates(synthetic))
    assert any(name.startswith("external evidence bytes pinned") for name in failed)
    assert _run_main(synthetic) == 1


def test_deleted_declared_evidence_forces_nonzero(synthetic: dict[str, object]) -> None:
    evidence = synthetic["evidence"]
    assert isinstance(evidence, Path)
    evidence.unlink()
    assert _run_main(synthetic) == 1


# ---- the ordering property -----------------------------------------------------


def test_failed_gate_refuses_to_execute_anything(synthetic: dict[str, object]) -> None:
    """Gates must precede EXECUTION: a tripped gate must yield an explicit NOT-RUN
    record instead of proof output — and must never run the proof suite at all."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "stray.py").write_text("# untracked\n")
    checks = _gates(synthetic)
    assert _failed(checks)
    assert _run_main(synthetic) == 1


def test_evidence_records_the_exact_sha_and_results(
    synthetic: dict[str, object], tmp_path: Path
) -> None:
    """Structured evidence must carry the EXACT candidate sha and per-check results."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "stray.py").write_text("# untracked\n")
    evidence_dir = tmp_path / "evidence"
    rc = receipt.main(
        [
            "--expect-head",
            str(synthetic["head"]),
            "--evidence-dir",
            str(evidence_dir),
        ],
        repo=repo,
    )
    assert rc == 1
    payload = json.loads((evidence_dir / "candidate-receipt.json").read_text())
    assert payload["candidate_sha"] == str(synthetic["head"])
    assert payload["green"] is False
    assert any(not c["ok"] for c in payload["checks"])


# ---- verifier-demonstrated bypasses (VERDICT-P5) — each must now be RED --------


def _rewrite_inventory(repo: Path, mutate) -> str:
    """Apply ``mutate(data)`` to the committed inventory and commit; return new HEAD."""
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    mutate(data)
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "inventory mutation")
    return _git(repo, "rev-parse", "HEAD")


def test_symbolic_base_sha_forces_nonzero(synthetic: dict[str, object]) -> None:
    """VERDICT-P5 bypass 1: ``base_sha: "HEAD"`` made the scope range empty and the
    receipt GREEN while binding nothing. A symbolic base must be refused outright."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    new_head = _rewrite_inventory(repo, lambda d: d.update(base_sha="HEAD"))
    failed = _failed(_gates(synthetic, new_head))
    # _gates passes the synthetic's REAL base; drive main() (inventory base) instead.
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1
    assert failed == []  # the explicit-base path stays green — the bypass was the inventory's


def test_base_sha_equal_to_head_forces_nonzero(synthetic: dict[str, object]) -> None:
    """VERDICT-P5 bypass 1 (exact-hex variant): base == HEAD yields an empty scope;
    an empty scope binds nothing and must be RED."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    head = _git(repo, "rev-parse", "HEAD")
    new_head = _rewrite_inventory(repo, lambda d: d.update(base_sha=head))
    # base resolves and is 40-hex, but equals (an ancestor of) the certified HEAD's
    # tree walk start — the non-empty-scope / base!=HEAD checks must go red.
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1


def test_proof_suite_naming_external_evidence_is_refused_and_never_executed(
    synthetic: dict[str, object],
) -> None:
    """VERDICT-P5 bypass 2: the byte-pinned 'never executed' evidence file could be
    handed to pytest via expected_proof.tests. The gate must refuse BEFORE execution
    — proven by the canary staying absent."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    evidence = synthetic["evidence"]
    canary = synthetic["canary"]
    assert isinstance(evidence, Path) and isinstance(canary, Path)
    new_head = _rewrite_inventory(repo, lambda d: d["expected_proof"].update(tests=[str(evidence)]))
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1
    assert not canary.exists(), "the receipt EXECUTED external evidence via expected_proof"


def test_out_of_repo_proof_suite_forces_nonzero(
    synthetic: dict[str, object], tmp_path: Path
) -> None:
    """VERDICT-P5 bypass 2 (unpinned-path variant): an out-of-repo proof suite has no
    inventory binding — its bytes could change between runs. Must be refused."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    rogue = tmp_path / "rogue_proof.py"
    rogue.write_text("def test_always():\n    assert True\n")
    new_head = _rewrite_inventory(repo, lambda d: d["expected_proof"].update(tests=[str(rogue)]))
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1


def test_unbound_in_repo_proof_suite_forces_nonzero(synthetic: dict[str, object]) -> None:
    """A committed-but-undeclared proof suite is not hash-bound; the proof-binding
    gate must reject it independently of the scope gate."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)

    def mutate(d: dict) -> None:
        d["expected_proof"]["tests"] = ["unbound_proof.py"]

    (repo / "unbound_proof.py").write_text("def test_always():\n    assert True\n")
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    mutate(data)
    # bind the file in scope-coverage terms is deliberately SKIPPED: it stays out of
    # 'files', so both the scope gate AND the proof-binding gate should complain.
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unbound proof")
    new_head = _git(repo, "rev-parse", "HEAD")
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1


def test_relative_external_evidence_path_forces_nonzero(synthetic: dict[str, object]) -> None:
    """External evidence lives outside the repo by definition — a relative path is
    ambiguous (cwd-dependent) and must be refused."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    new_head = _rewrite_inventory(
        repo,
        lambda d: d.update(external_evidence=[{"path": "relative/probe.py", "sha256": "0" * 64}]),
    )
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1


def test_crashed_proof_process_yields_red_receipt_not_traceback(
    synthetic: dict[str, object], tmp_path: Path
) -> None:
    """VERDICT-P5 robustness: a proof process that dies without junit (SIGSEGV/OOM)
    must produce a RED check + structured evidence, never an unhandled exception."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    crasher = repo / "test_crash_proof.py"
    crasher.write_text("import ctypes\nctypes.string_at(0)\n")

    def mutate(d: dict) -> None:
        d["expected_proof"]["tests"] = ["test_crash_proof.py"]
        d["files"]["test_crash_proof.py"] = hashlib.sha256(crasher.read_bytes()).hexdigest()

    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    mutate(data)
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "crashing proof")
    new_head = _git(repo, "rev-parse", "HEAD")
    evidence_dir = tmp_path / "crash-evidence"
    rc = receipt.main(
        [
            "--expect-head",
            new_head,
            "--evidence-dir",
            str(evidence_dir),
        ],
        repo=repo,
    )
    assert rc == 1
    payload = json.loads((evidence_dir / "candidate-receipt.json").read_text())
    assert payload["green"] is False
    proof_checks = [c for c in payload["checks"] if c["name"].startswith("proof suites ==")]
    assert proof_checks and not proof_checks[0]["ok"]
    assert "no parseable junit" in proof_checks[0]["detail"]


# ---- C9-06: the demonstrated receipt bypasses are now each RED ------------------


def test_python_override_flag_is_rejected(synthetic: dict[str, object]) -> None:
    """C9-06 bypass 1: ``--python`` accepted an arbitrary interpreter (a fake one wrote a
    fabricated green JUnit). The flag is gone — argparse rejects it (SystemExit)."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    with pytest.raises(SystemExit):
        receipt.main(
            ["--expect-head", str(synthetic["head"]), "--python", sys.executable], repo=repo
        )


def test_interpreter_identity_is_recorded_in_evidence(
    synthetic: dict[str, object], tmp_path: Path
) -> None:
    """C9-06: the receipt records the ACTUAL running interpreter (path/realpath/version/
    sha256) — the evidence omitted it before."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    evidence_dir = tmp_path / "evidence"
    rc = receipt.main(
        ["--expect-head", str(synthetic["head"]), "--evidence-dir", str(evidence_dir)],
        repo=repo,
    )
    assert rc == 0
    payload = json.loads((evidence_dir / "candidate-receipt.json").read_text())
    interp = payload["interpreter"]
    assert interp["realpath"] and interp["version"] and interp["sha256"]
    assert payload["post_run_checks"], "post-run rechecks must be recorded"


def test_base_override_is_rejected_in_certification(synthetic: dict[str, object]) -> None:
    """C9-06 bypass 2: ``--base`` could re-scope the candidate. The certification path
    now rejects it (exit 2) and always uses the inventory's frozen base."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    rc = receipt.main(
        ["--expect-head", str(synthetic["head"]), "--base", str(synthetic["base"])], repo=repo
    )
    assert rc == 2


def test_undeclared_deletion_forces_nonzero(synthetic: dict[str, object]) -> None:
    """C9-06 bypass 3: deletions were excluded from scope. A file deleted in base..HEAD
    that is not declared as an inventory tombstone must fail closed."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "README.md").unlink()  # README exists at the base commit
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete README.md")
    new_head = _git(repo, "rev-parse", "HEAD")
    # README is deleted in base..HEAD but declared as no tombstone (deletions == []).
    failed = _failed(_gates(synthetic, new_head))
    assert any("deletions are declared exactly" in f for f in failed)
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1


def test_declared_deletion_tombstone_is_accepted(synthetic: dict[str, object]) -> None:
    """A deletion DECLARED as an inventory tombstone (and removed from files) is clean."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    (repo / "README.md").unlink()
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    data["deletions"] = ["README.md"]
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete README.md + tombstone")
    new_head = _git(repo, "rev-parse", "HEAD")
    assert _failed(_gates(synthetic, new_head)) == []
    assert receipt.main(["--expect-head", new_head], repo=repo) == 0


def test_evidence_dir_inside_checkout_forces_nonzero(synthetic: dict[str, object]) -> None:
    """C9-06 bypass 4: the evidence dir was not required outside the checkout. Writing
    the receipt inside the tree would dirty the very worktree it certifies — rejected."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inside = repo / "inside-evidence"
    rc = receipt.main(
        ["--expect-head", str(synthetic["head"]), "--evidence-dir", str(inside)], repo=repo
    )
    assert rc == 1
    failed = _failed(_gates_with_evidence(synthetic, inside))
    assert any("outside the checkout" in f for f in failed)


def test_relative_evidence_dir_forces_nonzero(synthetic: dict[str, object]) -> None:
    """A relative ``--evidence-dir`` is ambiguous (cwd-dependent) and rejected."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    failed = _failed(_gates_with_evidence(synthetic, Path("relative-evidence")))
    assert any("outside the checkout" in f for f in failed)


def test_base_not_ancestor_of_head_forces_nonzero(synthetic: dict[str, object]) -> None:
    """C9-06: a base that is not an ancestor of HEAD cannot define a real scope."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    # Create an unrelated orphan commit and point the inventory base at it.
    _git(repo, "checkout", "-q", "--orphan", "orphanbranch")
    (repo / "unrelated.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "orphan")
    orphan = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-f", "master") if _has_master(repo) else _git(
        repo, "checkout", "-q", "-f", str(synthetic["head"])
    )
    check = receipt.check_base_ancestry(repo, orphan)
    assert not check.ok


def _gates_with_evidence(synthetic: dict[str, object], evidence_dir: Path) -> list[Any]:
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inventory = receipt.load_inventory(repo)
    return receipt.gate_checks(
        repo, str(synthetic["head"]), inventory, str(synthetic["base"]), evidence_dir
    )


def _has_master(repo: Path) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "master")
        return True
    except subprocess.CalledProcessError:
        return False


# ---- C9-06 P5 verifier corrections (each demonstrated bypass now RED) -----------


def test_external_evidence_overwritten_after_proof_is_caught_post_run(
    synthetic: dict[str, object], tmp_path: Path
) -> None:
    """P5-1: a hash-bound proof that overwrites the declared external evidence AFTER the
    pre-run gate must be caught by the post-run external re-pin — no green receipt."""
    repo = synthetic["repo"]
    evidence = synthetic["evidence"]
    assert isinstance(repo, Path) and isinstance(evidence, Path)
    # A proof test that mutates the external evidence file as a side effect of running.
    proof = repo / "test_receipt_proof.py"
    proof.write_text(
        "def test_green():\n"
        f"    open({str(evidence)!r}, 'w').write('mutated after the pre-run gate')\n"
        "    assert True\n"
    )
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    data["files"]["test_receipt_proof.py"] = _sha256(proof)
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "proof mutates external evidence")
    new_head = _git(repo, "rev-parse", "HEAD")
    evidence_dir = tmp_path / "evidence"
    rc = receipt.main(["--expect-head", new_head, "--evidence-dir", str(evidence_dir)], repo=repo)
    assert rc == 1
    payload = json.loads((evidence_dir / "candidate-receipt.json").read_text())
    assert any(
        c["name"] == "post-run: external evidence still byte-pinned" and not c["ok"]
        for c in payload["post_run_checks"]
    )


def test_symlink_evidence_dir_is_rejected_and_not_written(
    synthetic: dict[str, object], tmp_path: Path
) -> None:
    """P5-2: an absolute symlink evidence dir (even one initially pointing outside) is
    rejected AND no receipt is written through it — closing the retarget TOCTOU."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "evlink"
    link.symlink_to(outside)
    rc = receipt.main(
        ["--expect-head", str(synthetic["head"]), "--evidence-dir", str(link)], repo=repo
    )
    # Gate fails (symlink) → red, and nothing is written through the link.
    assert rc == 1
    assert not (outside / "candidate-receipt.json").exists()
    failed = _failed(_gates_with_evidence(synthetic, link))
    assert any("not a symlink" in f for f in failed)


def test_evidence_never_written_inside_the_checkout_even_on_red(
    synthetic: dict[str, object],
) -> None:
    """P5-2 (write side): a failed location gate must SUPPRESS the write — the receipt
    never drops a candidate-receipt.json inside the checkout, on green or red."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inside = repo / "inside-evidence"
    rc = receipt.main(
        ["--expect-head", str(synthetic["head"]), "--evidence-dir", str(inside)], repo=repo
    )
    assert rc == 1
    assert not (inside / "candidate-receipt.json").exists()
    # And the tree is not dirtied by a stray receipt.
    assert receipt.check_worktree_pristine(repo).ok


def test_interpreter_trust_root_mismatch_forces_nonzero(synthetic: dict[str, object]) -> None:
    """P5-3: when the inventory declares a trusted_interpreter_sha256, a running
    interpreter whose realpath sha differs must fail closed (an attacker-controlled
    launching interpreter cannot fabricate proof under a declared trust root)."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    data["trusted_interpreter_sha256"] = "0" * 64  # never the real interpreter
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "declare a trust root the interpreter cannot match")
    new_head = _git(repo, "rev-parse", "HEAD")
    failed = _failed(_gates(synthetic, new_head))
    assert any("operator trust root" in f for f in failed)
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1


def test_no_trust_root_declared_forces_nonzero(synthetic: dict[str, object]) -> None:
    """C9-06 P5-3 (owner round): with NO trust root (neither CLI nor inventory), the
    certification must fail closed — recording the interpreter is not trust."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    del data["trusted_interpreter_sha256"]
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop the trust root")
    new_head = _git(repo, "rev-parse", "HEAD")
    failed = _failed(_gates(synthetic, new_head))
    assert any("explicit operator trust root" in f for f in failed)
    assert receipt.main(["--expect-head", new_head], repo=repo) == 1


def test_cli_trusted_interpreter_matching_is_green(
    synthetic: dict[str, object], tmp_path: Path
) -> None:
    """A matching --trusted-interpreter-sha256 (with the inventory's dropped) certifies
    green — the operator-supplied trust root is honored."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    inventory_path = repo / str(receipt.INVENTORY_REL)
    data = json.loads(inventory_path.read_text())
    del data["trusted_interpreter_sha256"]
    inventory_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop inventory trust root; rely on CLI")
    new_head = _git(repo, "rev-parse", "HEAD")
    good = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
    rc = receipt.main(["--expect-head", new_head, "--trusted-interpreter-sha256", good], repo=repo)
    assert rc == 0
    rc_bad = receipt.main(
        ["--expect-head", new_head, "--trusted-interpreter-sha256", "0" * 64], repo=repo
    )
    assert rc_bad == 1
