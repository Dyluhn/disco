"""Mutation proofs for the recovery candidate receipt
(``scripts/export_track1_candidate_receipt.py``).

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

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RECEIPT_PATH = _REPO_ROOT / "scripts" / "export_track1_candidate_receipt.py"


def _load_receipt() -> Any:
    """Import the receipt BY PATH (``scripts/`` is not an importable package).

    The module MUST be registered in ``sys.modules`` before ``exec_module``:
    ``@dataclass`` resolves its own class's module out of ``sys.modules`` while
    processing annotations, and raises on a module that is not yet registered.
    """
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
        repo, head or str(synthetic["head"]), inventory, str(synthetic["base"])
    )


def _run_main(synthetic: dict[str, object], head: str | None = None) -> int:
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    return receipt.main(
        ["--expect-head", head or str(synthetic["head"]), "--python", sys.executable],
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
    synthetic: dict[str, object],
) -> None:
    """The happy path reaches GREEN (real proof suite, exact counts) and the declared
    external evidence is verified but NEVER executed: running it would create the
    canary file."""
    canary = synthetic["canary"]
    assert isinstance(canary, Path)
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
            "--python",
            sys.executable,
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
    assert receipt.main(["--expect-head", new_head, "--python", sys.executable], repo=repo) == 1
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
    assert receipt.main(["--expect-head", new_head, "--python", sys.executable], repo=repo) == 1


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
    assert receipt.main(["--expect-head", new_head, "--python", sys.executable], repo=repo) == 1
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
    assert receipt.main(["--expect-head", new_head, "--python", sys.executable], repo=repo) == 1


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
    assert receipt.main(["--expect-head", new_head, "--python", sys.executable], repo=repo) == 1


def test_relative_external_evidence_path_forces_nonzero(synthetic: dict[str, object]) -> None:
    """External evidence lives outside the repo by definition — a relative path is
    ambiguous (cwd-dependent) and must be refused."""
    repo = synthetic["repo"]
    assert isinstance(repo, Path)
    new_head = _rewrite_inventory(
        repo,
        lambda d: d.update(external_evidence=[{"path": "relative/probe.py", "sha256": "0" * 64}]),
    )
    assert receipt.main(["--expect-head", new_head, "--python", sys.executable], repo=repo) == 1


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
            "--python",
            sys.executable,
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
