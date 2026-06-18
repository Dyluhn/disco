"""Phase 0 — build-loop regression harness (the §10.1 metric gate).

This pins the build-loop health metrics so the read-thrash death is PROVABLE
WITHOUT a live model. It has two jobs:

  1. BASELINE (this file, now): validate the `harness/build_loop_metrics`
     extractor against the golden trace `docs/evidence/macos-build-trace-6-17-26.txt`
     (the 374-event `build a simple macosx clone` run that looped to noop death),
     and assert the baseline VIOLATES every §10.1 target — i.e. the harness
     genuinely captures the bug. These assertions PASS today (the bug is present),
     so the required CI job stays green while the fix lands wave by wave.

  2. LIVE ACCEPTANCE (post W1–W6): the disco-live e2e re-runs the same build,
     renders a fresh trace, and calls `assert_meets_targets(...)`. The fix is
     proven only when the NEW trace meets every target (file_read < 20, max
     reads/path ≤ 3, re-read thoughts < 10, browser 30s timeouts == 0).

The smoking gun the extractor surfaces: `js/windows.js` (8,047 B) was read 20×
because it exceeds BOTH the snapshot per-file cap (6000, view_render.py) AND the
file_read page budget (7000, files.py) → the harness told the model it was never
"fully seen" → the model obeyed. W2 (stale-aware snapshot + windowed view) is the
fix; this test will flip to GREEN-on-target once the live re-run lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Single-source the extractor from harness/ (also used by the live-acceptance
# script). The repo root is parents[3]: tests → core → packages → <repo>.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "harness") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "harness"))

from build_loop_metrics import (  # noqa: E402
    TARGETS,
    assert_meets_targets,
    compute_build_metrics,
    target_violations,
)

_GOLDEN_TRACE = _REPO_ROOT / "docs" / "evidence" / "macos-build-trace-6-17-26.txt"


def _golden_metrics():
    text = _GOLDEN_TRACE.read_text(encoding="utf-8")
    return compute_build_metrics(text)


# ---------------------------------------------------------------------------
# (1) The extractor reproduces the known-bad baseline (validates the instrument)
# ---------------------------------------------------------------------------


def test_golden_trace_exists():
    assert _GOLDEN_TRACE.is_file(), (
        f"golden trace missing at {_GOLDEN_TRACE}; the regression harness needs it"
    )


def test_baseline_metrics_match_the_documented_loop():
    """The extractor must reproduce the documented baseline so we trust it as the
    live-acceptance instrument. These are the exact counts the loop produced."""
    m = _golden_metrics()
    assert m.file_read_count == 82, m.as_row()
    # windows.js (8,047 B > both caps) was re-read 20× — the dominant signal.
    assert m.max_reads_per_path == 20, m.reads_by_path
    assert m.reads_by_path["/workspace/macos-clone/js/windows.js"] == 20
    # 4 Playwright 30s click timeouts (the broken dock-click path).
    assert m.browser_30s_timeouts == 4
    # The model narrated re-read pressure dozens of times (obeying the harness).
    assert m.reread_pressure_thoughts >= 50, m.reread_pressure_thoughts


# ---------------------------------------------------------------------------
# (2) The baseline VIOLATES every target (the harness captures the failure)
# ---------------------------------------------------------------------------


def test_baseline_violates_all_targets():
    """Each §10.1 target is violated by the golden (pre-fix) trace. This is the
    proof that the metric gate is meaningful — a passing gate genuinely means the
    loop got healthy, not that the gate is vacuous."""
    m = _golden_metrics()
    violations = target_violations(m)
    # All four targets are violated by the baseline.
    assert len(violations) == 4, violations
    joined = " | ".join(violations)
    assert "file_read_count" in joined
    assert "max_reads_per_path" in joined
    assert "reread_pressure_thoughts" in joined
    assert "browser_30s_timeouts" in joined


def test_assert_meets_targets_rejects_the_baseline():
    """The live-acceptance gate (`assert_meets_targets`) must FAIL on the pre-fix
    trace (else it would green-light an unfixed loop)."""
    m = _golden_metrics()
    raised = False
    try:
        assert_meets_targets(m)
    except AssertionError as e:
        raised = True
        assert "§10.1" in str(e)
    assert raised, "assert_meets_targets must reject the known-bad baseline trace"


# ---------------------------------------------------------------------------
# (3) The gate ACCEPTS a synthetic healthy trace (the post-fix shape)
# ---------------------------------------------------------------------------


def test_assert_meets_targets_accepts_a_healthy_trace():
    """A synthetic trace that reads each of 13 files ≤3×, narrates no re-read
    pressure, and has zero browser timeouts MEETS every target — proving the gate
    is satisfiable by the intended post-fix behavior (not impossibly strict)."""
    lines = ["[1] MSG[user] build a simple macosx clone", "[2] STATUS RUNNING "]
    seq = 3
    files = [f"/workspace/app/file{i}.js" for i in range(13)]
    for f in files:  # read each file once (13 reads total — well under 20)
        lines.append(f"[{seq}] ACTION file_read(path='{f}')")
        lines.append("        thought: reading this file once to build it")
        lines.append("        → OBS file_read ok=True: <content>")
        seq += 2
    lines.append(f"[{seq}] ACTION finish(summary='done, verified once')")
    lines.append(f"[{seq + 1}] STATUS FINISHED ")
    healthy = "\n".join(lines)

    m = compute_build_metrics(healthy)
    assert m.file_read_count == 13
    assert m.max_reads_per_path == 1
    assert m.reread_pressure_thoughts == 0
    assert m.browser_30s_timeouts == 0
    assert target_violations(m) == []
    assert_meets_targets(m)  # must not raise


# ---------------------------------------------------------------------------
# (4) The target table is the one in §10.1 (single source of truth guard)
# ---------------------------------------------------------------------------


def test_targets_match_the_spec():
    assert TARGETS == {
        "file_read_count_max": 20,
        "max_reads_per_path_max": 3,
        "reread_pressure_thoughts_max": 10,
        "browser_30s_timeouts_max": 0,
    }
