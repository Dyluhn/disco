"""Mutation-regression proof that the closeout verifier's evidence-hygiene gate WORKS.

The acceptance verifier ``development/scripts/verify_export_track1_closeout.py`` grew an
evidence-hygiene gate (WO-B): after every text evidence artifact is written it scans
each for a REGISTERED planted-credential marker and, on any hit, fails acceptance
CLOSED (the hygiene result folds into ``passed`` exactly like ``frozen_ok``/``clean``).
Nothing committed proved that gate actually bites. This file is that proof.

Like the G13 truthfulness test, it IMPORTS the real verifier module and calls its real
functions on a real temporary filesystem — that is the CODE UNDER TEST, not a mock, and
is allowed by the anti-bypass contract (no ``unittest.mock`` / ``MagicMock`` / ``patch``
/ ``monkeypatch``; the filesystem and the G02 subprocess are real). These are GREEN
regression tests: each asserts the gate behaves correctly, so they PASS.

Seven properties are pinned:

1. Per-artifact mutation — for EACH text-artifact class the gate scans
   (``verify._HYGIENE_SCAN_FILES``, imported, never hardcoded), planting the registered
   marker makes ``_scan_evidence_hygiene`` fail with a violation naming that file.
2. Redacted violation shape — a violation carries EXACTLY ``{file, line, sentinel}``,
   none of its values echo the marker or the full credential, and ``sentinel`` is the
   non-secret LABEL, not the marker.
3. Verdict gating — the extracted ``_final_verdict`` helper turns a hygiene failure into
   ``passed: false`` (and a clean hygiene into ``true`` when all else is green), and
   ``_not_passed_reasons`` cites the hygiene failure only when it occurred.
4. Clean evidence accepted — a marker-free evidence dir scans clean.
5. The REAL G02 proving-red evidence is credential-clean — running the six G02 nodes as
   a subprocess leaves ZERO marker/credential occurrences on EVERY captured surface: the
   JUnit XML, the captured stdout, AND the captured stderr (a leak to stderr never reaches
   stdout under ``capture_output=True``). The three surfaces come from the shared
   ``_evidence_surfaces`` helper — the single source of truth for what is scanned.
6. This file never re-emits the credential — the marker and full sentinel are referenced
   ONLY through the imported constants; the literal never appears in this source or in
   any assertion message, and only the NON-secret marker is ever planted.
7. The stderr lane is genuinely scanned — a marker planted ONLY in stderr (clean JUnit XML
   and stdout), built through the SAME ``_evidence_surfaces`` helper, is detected as
   leaking on EXACTLY the ``captured stderr`` surface; dropping stderr from that helper
   fails this test, pinning stderr as an enforced credential-scanned lane.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

# ``development/scripts/`` and this closeout dir are not installed packages. Put them on ``sys.path``
# exactly as the verifier resolves its sibling manifest module and as pytest's prepend
# import mode already exposes sibling test modules. ``importlib.import_module`` (a call,
# not a top-level ``import`` of code-under-test) keeps this lint-clean.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[5]
_SCRIPTS_DIR = _REPO_ROOT / "development" / "scripts"
for _extra in (_SCRIPTS_DIR, _THIS_DIR):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

verify = importlib.import_module("verify_export_track1_closeout")
_g02 = importlib.import_module("test_g02_positional_credential")

pytestmark = pytest.mark.export_track1_closeout

# The registered evidence-hygiene sentinel as (LABEL, MARKER). Both are read from the
# code under test / the G02 module — NEVER inlined here — so this file never itself
# re-emits the credential literal. The MARKER (``G02POSCRED``) is a NON-secret substring
# of the planted token; the full credential lives only behind ``_FULL_SENTINEL``.
_LABEL, _MARKER = next(iter(verify._EVIDENCE_HYGIENE_SENTINELS.items()))
_FULL_SENTINEL = _g02._CRED_SENTINEL


def _evidence_surfaces(junit_text: str, stdout: str, stderr: str) -> dict[str, str]:
    """The SINGLE SOURCE OF TRUTH for the credential-scanned surfaces of a G02 subprocess
    run. ``subprocess.run(..., capture_output=True)`` yields stdout and stderr as SEPARATE
    streams, so a credential leaked to stderr (a traceback, a warning, a stderr logging
    handler) never appears in stdout — scanning only stdout + the JUnit XML would miss it.
    Returning all three as one mapping makes the stderr surface an enforceable,
    independently-scanned lane: the real-G02 test iterates this mapping, and
    ``test_stderr_surface_is_independently_scanned`` pins that the stderr lane is present."""
    return {"JUnit XML": junit_text, "captured stdout": stdout, "captured stderr": stderr}


@pytest.mark.parametrize("artifact", verify._HYGIENE_SCAN_FILES)
def test_marker_in_each_scanned_artifact_is_a_violation(artifact: str, tmp_path: Path) -> None:
    """(1) For EVERY text-artifact class the gate scans, a planted registered marker must
    make ``_scan_evidence_hygiene`` fail CLOSED with a violation naming that artifact.
    Parametrized over the real ``_HYGIENE_SCAN_FILES`` tuple (imported), so a class added
    to the gate without a matching regression makes this test fail to enumerate it."""
    (tmp_path / artifact).write_text(
        f"evidence line planting the registered non-secret marker {_MARKER} here\n",
        encoding="utf-8",
    )

    ok, violations = verify._scan_evidence_hygiene(tmp_path)

    assert ok is False, (
        f"a planted registered sentinel in {artifact!r} must fail the hygiene scan, "
        "but the scan reported clean"
    )
    named = [v for v in violations if v["file"] == artifact]
    assert named, (
        f"the hygiene scan flagged a violation but none names the mutated artifact "
        f"{artifact!r}; violations named {[v['file'] for v in violations]!r}"
    )


def test_violation_shape_is_redacted_and_labelled(tmp_path: Path) -> None:
    """(2) Each violation is a REDACTED locator: exactly ``{file, line, sentinel}``, no
    value echoes the marker or the full credential, and ``sentinel`` is the non-secret
    LABEL (not the marker) — so a leak is located without the report re-emitting it."""
    artifact = verify._HYGIENE_SCAN_FILES[0]
    (tmp_path / artifact).write_text(f"a line carrying {_MARKER}\n", encoding="utf-8")

    ok, violations = verify._scan_evidence_hygiene(tmp_path)

    assert ok is False and violations, "expected a hygiene violation to inspect"
    for v in violations:
        assert set(v.keys()) == {"file", "line", "sentinel"}, (
            f"a violation must carry exactly file/line/sentinel; got keys {sorted(v.keys())!r}"
        )
        for key, value in v.items():
            text = str(value)
            assert _MARKER not in text and _FULL_SENTINEL not in text, (
                f"violation field {key!r} echoed credential material into the report"
            )
        assert v["sentinel"] == _LABEL, (
            "the sentinel field must be the non-secret hygiene LABEL, never the marker "
            "or the credential value"
        )


def test_hygiene_violation_forces_verdict_false() -> None:
    """(3) The extracted ``_final_verdict`` conjunction gates on hygiene: with every other
    input green, ``hygiene_ok=False`` forces the verdict False and flipping ONLY
    ``hygiene_ok`` True yields True; ``_not_passed_reasons`` cites the hygiene failure iff
    it occurred."""
    green = {"frozen_ok": True, "all_lanes_green": True, "clean": True, "author": False}
    assert verify._final_verdict(**green, hygiene_ok=False) is False, (
        "a hygiene violation (hygiene_ok=False) must force the final verdict False"
    )
    assert verify._final_verdict(**green, hygiene_ok=True) is True, (
        "with every other input green, flipping only hygiene_ok True must yield True"
    )

    reasons_bad = verify._not_passed_reasons(
        author=False, clean=True, frozen_ok=True, lanes=[], hygiene_ok=False
    )
    assert any("evidence hygiene" in r for r in reasons_bad), (
        f"not_passed_reasons must cite the evidence-hygiene failure; got {reasons_bad!r}"
    )
    reasons_ok = verify._not_passed_reasons(
        author=False, clean=True, frozen_ok=True, lanes=[], hygiene_ok=True
    )
    assert reasons_ok == [], (
        f"a clean hygiene result must add no hygiene reason (nor any other here); "
        f"got {reasons_ok!r}"
    )


def test_clean_evidence_dir_passes_hygiene(tmp_path: Path) -> None:
    """(4) A dir holding marker-free versions of every scanned artifact class scans clean:
    ``_scan_evidence_hygiene`` returns ``(True, [])``. Guards against a gate that fails
    everything (which would trivially satisfy the mutation tests)."""
    for name in verify._HYGIENE_SCAN_FILES:
        (tmp_path / name).write_text(
            f"clean evidence artifact {name} with no planted sentinel\n",
            encoding="utf-8",
        )

    ok, violations = verify._scan_evidence_hygiene(tmp_path)

    assert ok is True and violations == [], (
        "a marker-free evidence dir must pass the hygiene scan; got violations naming "
        f"{[v['file'] for v in violations]!r}"
    )


def test_real_g02_proving_reds_leave_no_credential_in_evidence(tmp_path: Path) -> None:
    """(5) The REAL G02 proving-red nodes, run as a subprocess exactly as the verifier's
    closeout lane runs pytest, must leave ZERO marker/credential occurrences on EVERY
    captured surface — the produced JUnit XML, the captured stdout, AND the captured
    stderr — the hygiene property the gate enforces, proven end-to-end against the actual
    planted credential rather than a stand-in. The three surfaces come from the shared
    ``_evidence_surfaces`` helper so stderr is scanned as its own independent lane (a leak
    to stderr never reaches stdout under ``capture_output=True``)."""
    g02_file = _THIS_DIR / "test_g02_positional_credential.py"
    junit = tmp_path / "g02-junit.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(g02_file),
            "-o",
            "addopts=",
            "-m",
            "export_track1_closeout and not integration",
            f"--junitxml={junit}",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    junit_text = junit.read_text(encoding="utf-8") if junit.is_file() else ""

    # Sanity: the six G02 proving-red nodes actually executed. A vacuous zero-node run
    # would satisfy credential-absence trivially, so pin the real node count.
    counts = verify._parse_junit(junit)
    assert counts["tests"] == 6, (
        f"expected the six G02 proving-red nodes to run; JUnit recorded {counts!r} "
        f"(pytest exit={proc.returncode})"
    )

    # Scan ALL THREE captured surfaces INDEPENDENTLY (JUnit XML, stdout, stderr) via the
    # shared single-source-of-truth helper. For each, assert ZERO occurrences of BOTH the
    # registered marker and the full planted sentinel — printing only the COUNT, never the
    # value, so no message can re-emit the credential.
    for surface, blob in _evidence_surfaces(junit_text, proc.stdout, proc.stderr).items():
        assert blob.count(_MARKER) == 0, (
            f"the registered credential marker leaked into the G02 {surface} "
            f"({blob.count(_MARKER)} occurrences); acceptance evidence must be credential-clean"
        )
        assert blob.count(_FULL_SENTINEL) == 0, (
            f"the full planted credential leaked into the G02 {surface} "
            f"({blob.count(_FULL_SENTINEL)} occurrences); it must never reach evidence"
        )


def test_stderr_surface_is_independently_scanned() -> None:
    """(7) The captured-stderr lane is GENUINELY one of the independently-scanned surfaces:
    a marker planted ONLY in stderr — with a clean JUnit XML and clean stdout — must be
    detected as leaking on EXACTLY the ``captured stderr`` surface. Built through the SAME
    ``_evidence_surfaces`` helper the real-G02 test uses, so if a future edit drops stderr
    from that mapping this test fails: it pins stderr as an enforced, credential-scanned
    lane rather than a stream the scan silently ignores. Only the non-secret marker is
    planted — never the full sentinel."""
    clean_junit = '<?xml version="1.0"?><testsuite tests="6" failures="0" errors="0"/>'
    clean_stdout = "6 passed in 1.23s\nno credential material on this stream\n"
    # A synthetic stderr blob standing in for a traceback / warning / stderr log handler
    # that carried the registered marker — the exact leak the stdout-only scan would miss.
    planted_stderr = f"Traceback (most recent call last):\n  RuntimeError: {_MARKER} slipped out\n"

    surfaces = _evidence_surfaces(clean_junit, clean_stdout, planted_stderr)

    assert "captured stderr" in surfaces, (
        "the evidence-surface mapping must expose the captured stderr stream as its own "
        "independently-scanned lane; it is absent, so a credential leaked only to stderr "
        "would sail past the hygiene scan"
    )
    leaking = sorted(name for name, blob in surfaces.items() if _MARKER in blob)
    assert leaking == ["captured stderr"], (
        "a marker planted ONLY in stderr (JUnit XML and stdout clean) must be detected as "
        f"leaking on exactly the captured-stderr surface; surfaces flagged as leaking were "
        f"{leaking!r} — if stderr were dropped from the scanned surfaces this would be empty"
    )
