"""Build-loop health metrics — Phase 0 of the Track-A build-harness fix.

This is the REUSABLE acceptance instrument for the §10.1 metric table
(`development/notes/disco-direction-and-decisions-6-17-26.md`). It parses a rendered build
trace (`[seq] ACTION tool(args) / thought / → OBS`) and computes the objective
signals that distinguish a healthy build loop from the read-thrash death the
macOS-clone run exhibited.

Used two ways:
  1. REGRESSION (now): `packages/core/tests/test_build_loop_regression.py` asserts
     the golden trace (`development/notes/evidence/macos-build-trace-6-17-26.txt`) reproduces
     the known-bad BASELINE — validating the extractor AND documenting that the
     bug is real and measurable. The same test asserts the baseline VIOLATES the
     targets (so the harness genuinely captures the failure).
  2. LIVE ACCEPTANCE (post W1–W6): the live e2e re-runs `build a simple macosx
     clone` on the disco-live runner, renders the new trace, and calls
     `assert_meets_targets(compute_build_metrics(new_trace))` — the fix is proven
     only when the new run passes EVERY target.

Pure + dependency-free (stdlib only) so it imports cleanly from a packaged test
and from a standalone live-acceptance script.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# §10.1 — the "re-read pressure" thought keywords. The macOS trace had 91 thoughts
# containing one of these; a healthy loop has < 10. Matched case-insensitively
# against the model's own narration (the `thought:` lines).
_REREAD_KEYWORDS = ("remaining", "haven't seen", "truncated", "full file", "not yet seen")

_ACTION_RE = re.compile(r"^\[(\d+)\]\s+ACTION\s+([a-z_]+)\(", re.MULTILINE)
_FILE_READ_PATH_RE = re.compile(r"^\[\d+\]\s+ACTION\s+file_read\(path='([^']*)'", re.MULTILINE)
_THOUGHT_RE = re.compile(r"^\s*thought:\s*(.*)$", re.MULTILINE)
# Playwright's 30s click timeout — the broken-browser signal (dock built from
# <div>+addEventListener has no [data-pmx-index], so click(index) times out).
_BROWSER_TIMEOUT_RE = re.compile(r"Timeout 30000")
_FINISHED_RE = re.compile(r"\bSTATUS\s+FINISHED\b")
_NOOP_LIMIT_RE = re.compile(r"noop_limit|max_iterations|no_progress", re.IGNORECASE)


@dataclass(frozen=True)
class BuildMetrics:
    """Objective build-loop health signals extracted from a rendered trace."""

    file_read_count: int
    max_reads_per_path: int
    reread_pressure_thoughts: int
    browser_30s_timeouts: int
    action_count: int
    reads_by_path: dict[str, int] = field(default_factory=dict)

    def as_row(self) -> str:
        return (
            f"file_read={self.file_read_count} max_reads/path={self.max_reads_per_path} "
            f"reread_thoughts={self.reread_pressure_thoughts} "
            f"browser_timeouts={self.browser_30s_timeouts} actions={self.action_count}"
        )


# The §10.1 acceptance targets (a healthy build loop must meet ALL).
TARGETS = {
    "file_read_count_max": 20,  # was 82
    "max_reads_per_path_max": 3,  # was 15
    "reread_pressure_thoughts_max": 10,  # was 91
    "browser_30s_timeouts_max": 0,  # was ≥4
}


def compute_build_metrics(trace_text: str) -> BuildMetrics:
    """Parse a rendered build trace into objective health metrics. Pure."""
    reads_by_path: dict[str, int] = {}
    for path in _FILE_READ_PATH_RE.findall(trace_text):
        reads_by_path[path] = reads_by_path.get(path, 0) + 1
    file_read_count = sum(1 for _, tool in _ACTION_RE.findall(trace_text) if tool == "file_read")
    reread_thoughts = 0
    for thought in _THOUGHT_RE.findall(trace_text):
        low = thought.lower()
        if any(kw in low for kw in _REREAD_KEYWORDS):
            reread_thoughts += 1
    return BuildMetrics(
        file_read_count=file_read_count,
        max_reads_per_path=max(reads_by_path.values(), default=0),
        reread_pressure_thoughts=reread_thoughts,
        browser_30s_timeouts=len(_BROWSER_TIMEOUT_RE.findall(trace_text)),
        action_count=len(_ACTION_RE.findall(trace_text)),
        reads_by_path=reads_by_path,
    )


def compute_build_metrics_from_events(events: list[tuple[str, dict]]) -> BuildMetrics:
    """Compute the same BuildMetrics directly from disco.db events — the LIVE
    acceptance path (no text rendering). `events` is [(kind, payload_dict), …]
    in seq order. Pure. Mirrors compute_build_metrics' definitions:
      - file_read_count / reads_by_path: action events with tool_name=='file_read'
      - reread_pressure_thoughts: action.thought carrying a re-read keyword
      - browser_30s_timeouts: 'Timeout 30000' occurrences (live in agent_error)
      - action_count: action events
    """
    reads_by_path: dict[str, int] = {}
    file_read_count = 0
    action_count = 0
    reread_thoughts = 0
    browser_timeouts = 0
    for kind, p in events:
        browser_timeouts += json.dumps(p).count("Timeout 30000")
        if kind != "action":
            continue
        action_count += 1
        tc = p.get("tool_call") or {}
        if tc.get("tool_name") == "file_read":
            file_read_count += 1
            path = (tc.get("arguments") or {}).get("path")
            if path:
                reads_by_path[path] = reads_by_path.get(path, 0) + 1
        thought = (p.get("thought") or "").lower()
        if any(kw in thought for kw in _REREAD_KEYWORDS):
            reread_thoughts += 1
    return BuildMetrics(
        file_read_count=file_read_count,
        max_reads_per_path=max(reads_by_path.values(), default=0),
        reread_pressure_thoughts=reread_thoughts,
        browser_30s_timeouts=browser_timeouts,
        action_count=action_count,
        reads_by_path=reads_by_path,
    )


def target_violations(m: BuildMetrics) -> list[str]:
    """Return a human-readable list of which §10.1 targets `m` violates.
    Empty list ⇒ the trace meets every target (a healthy loop)."""
    out: list[str] = []
    if m.file_read_count > TARGETS["file_read_count_max"]:
        out.append(f"file_read_count {m.file_read_count} > {TARGETS['file_read_count_max']}")
    if m.max_reads_per_path > TARGETS["max_reads_per_path_max"]:
        out.append(
            f"max_reads_per_path {m.max_reads_per_path} > {TARGETS['max_reads_per_path_max']}"
        )
    if m.reread_pressure_thoughts > TARGETS["reread_pressure_thoughts_max"]:
        out.append(
            f"reread_pressure_thoughts {m.reread_pressure_thoughts} > "
            f"{TARGETS['reread_pressure_thoughts_max']}"
        )
    if m.browser_30s_timeouts > TARGETS["browser_30s_timeouts_max"]:
        out.append(
            f"browser_30s_timeouts {m.browser_30s_timeouts} > {TARGETS['browser_30s_timeouts_max']}"
        )
    return out


def assert_meets_targets(m: BuildMetrics) -> None:
    """Raise AssertionError naming every violated §10.1 target. Called by the
    LIVE acceptance run on the post-fix trace; the fix is proven only when this
    passes (no violations)."""
    violations = target_violations(m)
    assert not violations, "build-loop metrics violate §10.1 targets:\n  - " + "\n  - ".join(
        violations
    )


if __name__ == "__main__":  # calibration / ad-hoc
    import sys

    text = open(sys.argv[1], encoding="utf-8").read()
    m = compute_build_metrics(text)
    print(m.as_row())
    print("violations:", target_violations(m) or "NONE (meets all targets)")
    print("top reread paths:", sorted(m.reads_by_path.items(), key=lambda kv: -kv[1])[:5])
