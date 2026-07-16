#!/usr/bin/env python3
"""Final receipt — enforceable, re-runnable certification of the CLOSED Python
release-detection hardening (Track-1), re-baselined at Round-5 to the fully-closed
end-state (Batches 1 + 3 + 4 + 5).

Originally the Batch-1 receipt; re-baselined once Batches 3/4/5 remediated the 10 non-Python
defects the Batch-1 matrix still reproduced. It (1) fingerprints the full worktree, (2) derives
HEAD from Git, (3) runs the 563 proof tests, and (4) runs the 104-case independent adversarial
matrix — which now reproduces ONLY the 3 LEX positive controls — then EXITS NONZERO unless every
expected result is exact. No network, no edits, read-only. (Re-baseline ledgered for owner
ratification at commit.)

Run:  .venv/bin/python3 scripts/batch1_final_receipt.py
Exit: 0 iff all checks pass; 1 otherwise (with a per-check diff).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python3"

# The frozen, independent 104-case adversarial harness (authored outside this repo).
HARNESS = Path(
    "/var/home/dylan/.local/state/disco/r7-round5-adversary/"
    "20260714T-round5-independent/adversarial_probe.py"
)

# ---- Pinned expected results (the Batch-1 cleanup end-state) ----
EXPECTED_HEAD = "c0c4f728e8669dd27f88a9515383cc9a4a58f0d3"  # nothing committed
# Re-baselined at Round-5 final verification to the FULLY-CLOSED end-state (Batches 1+3+4+5):
# python_proof.py + detect.py legitimately advanced through Batch-3 (certain-eval import
# resolution) and Batch-5 (gunicorn config proof). The Batch-1 pins (python_proof
# 3db06ac3…, detect 509080073…) are superseded by the verified current hashes below. The
# reopen-test pin is unchanged (Batch-3/5 added tests in SEPARATE files, not this one), and
# the 2-file proof count stayed exactly 563/0. Ledgered for owner ratification at commit.
EXPECTED_HASHES = {
    "packages/core/src/disco/core/release/python_proof.py": (
        "bba29fce7ea00a475fa6bd960f052b50d46f1d6ac2a997b884094f83a9c84f99"
    ),
    "packages/core/src/disco/core/release/detect.py": (
        "db43268b2229d591ef623211b75c86229ae9abc307dd62bf6aa48ae4a69a9e67"
    ),
    "packages/core/tests/release_remediation/test_r7_python_proof_reopen.py": (
        "277798fdeb3692dfeebd5edb55c03205b7d97b42494cd353740536e9abcbe2ce"
    ),
}
PROOF_TESTS = [
    "packages/core/tests/release_remediation/test_r7_python_proof.py",
    "packages/core/tests/release_remediation/test_r7_python_proof_reopen.py",
]
EXPECTED_PROOF_PASSED = 563
EXPECTED_PROOF_FAILED = 0
# The FULLY-CLOSED end-state: Batches 3/4/5 remediated the 10 non-Python defects the Batch-1
# matrix still reproduced (EXT-01, HEALTH-01, MIGRATE-01..04, SERVER-06, SPEC-02, SYNTAX-01/02
# — each now grades needs_review), leaving ONLY the 3 LEX positive controls (masked template/
# regex/comment listeners that MUST stay port_contract_unresolved). Re-baselined at Round-5.
EXPECTED_MATRIX = frozenset(
    {
        "LEX-01",
        "LEX-02",
        "LEX-03",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    # (2) Derive HEAD from Git.
    head = _git("rev-parse", "HEAD")
    record("HEAD (derived from git)", head == EXPECTED_HEAD, f"{head} (expected {EXPECTED_HEAD})")

    # (1) Fingerprint the full worktree: complete porcelain status + Batch-1 file hashes.
    porcelain = _git("status", "--porcelain")
    worktree_fp = hashlib.sha256(porcelain.encode()).hexdigest()
    record(
        "worktree fingerprint",
        True,
        f"git-status sha256={worktree_fp} ({len(porcelain.splitlines())} entries)",
    )
    for rel, expected in EXPECTED_HASHES.items():
        actual = _sha256(REPO / rel)
        record(
            f"hash {rel.split('/')[-1]}",
            actual == expected,
            f"{actual[:16]}… (expected {expected[:16]}…)",
        )

    # (3) Run the 563 proof tests — exact count required. This host's `-q` reporter
    # emits ONLY ANSI-wrapped progress dots (no "N passed" summary line reaches
    # stdout/stderr), so scraping terminal text is unreliable. Parse the structured
    # junit XML instead: its <testsuite> carries exact tests/failures/errors/skipped.
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as jf:
        junit = Path(jf.name)
    proc = subprocess.run(
        [
            str(VENV_PY),
            "-m",
            "pytest",
            *PROOF_TESTS,
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    root = ET.parse(junit).getroot()
    ts = root if root.tag == "testsuite" else root.find("testsuite")
    junit.unlink(missing_ok=True)
    total = int(ts.get("tests", "-1"))
    failed = int(ts.get("failures", "-1"))
    errors = int(ts.get("errors", "-1"))
    skipped = int(ts.get("skipped", "-1"))
    passed = total - failed - errors - skipped
    record(
        f"proof tests == {EXPECTED_PROOF_PASSED} passed / {EXPECTED_PROOF_FAILED} failed",
        passed == EXPECTED_PROOF_PASSED
        and failed == EXPECTED_PROOF_FAILED
        and errors == 0
        and skipped == 0
        and proc.returncode == 0,
        f"{passed} passed, {failed} failed, {errors} errors, {skipped} skipped "
        f"(junit tests={total}), pytest exit {proc.returncode}",
    )

    # (4) Run the 104-case independent adversarial matrix — the fully-closed end-state
    # reproduces ONLY the 3 LEX positive controls (all 10 non-Python defects remediated).
    if not HARNESS.exists():
        record("104-case matrix", False, f"harness missing: {HARNESS}")
    else:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            out = Path(tf.name)
        subprocess.run(
            [str(VENV_PY), str(HARNESS), "--output", str(out)],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        data = json.loads(out.read_text())
        out.unlink(missing_ok=True)
        reproduced = {str(r["id"]) for r in data["results"] if r.get("reproduced")}
        src_changed = bool(data.get("source_changed_during_probe"))
        py_req = sorted(i for i in reproduced if i.startswith(("PY", "REQ")))
        matrix_ok = reproduced == EXPECTED_MATRIX
        matrix_diff = sorted(reproduced ^ EXPECTED_MATRIX)
        record(
            "104-matrix == exactly the 3 LEX positive controls reproduced",
            matrix_ok,
            f"reproduced={len(reproduced)} match={matrix_ok} diff={matrix_diff}",
        )
        record("104-matrix no PY*/REQ* reproduced", not py_req, f"PY/REQ={py_req}")
        record(
            "104-matrix source unchanged during probe",
            not src_changed,
            f"source_changed={src_changed}",
        )

    # ---- Receipt ----
    print("=" * 78)
    print("FINAL RECEIPT — Python release-detection hardening, Track-1 (Batches 1+3+4+5)")
    print("=" * 78)
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")
    print("=" * 78)
    verdict = "GREEN — all expected results exact" if all_ok else "RED — one or more checks failed"
    print(f"RECEIPT: {verdict}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
