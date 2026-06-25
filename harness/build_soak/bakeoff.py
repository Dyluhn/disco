"""EPIC K bake-off scorecard — group Build-Soak runs by kernel and evaluate the
PiKernel-vs-DiscoKernel promotion gate (campaign §K3) from frozen run evidence.

Reads every run folder under a runs-root: ``manifest.json`` (``kernel`` +
``scenario_id``, stamped by run.py) + ``classification.json`` (the deterministic
oracle ``status`` ∈ {PASS, FAIL, INVALID_RUN, INFRA_FAILURE} + ``severity``). Pure
analysis over frozen evidence — it NEVER re-adjudicates; it only counts. INVALID_RUN
/ INFRA_FAILURE are inconclusive and excluded from the pass-rate denominator (a soak
infra hiccup is not a product failure — guidelines §3).

Usage:
    python -m build_soak.bakeoff --runs test-record/build-soak
    python -m build_soak.bakeoff --runs test-record/build-soak --scenario static_html_minimal --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Outcomes that COUNT toward a product pass rate (a build either worked or didn't).
# INVALID_RUN / INFRA_FAILURE are inconclusive — excluded from the denominator.
_CONCLUSIVE = ("PASS", "FAIL")


@dataclass
class KernelTally:
    kernel: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    invalid: int = 0
    infra: int = 0
    p0: int = 0
    p1: int = 0
    # scenario -> {"pass": n, "fail": n, "invalid": n, "infra": n}
    by_scenario: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def conclusive(self) -> int:
        return self.passed + self.failed

    @property
    def pass_rate(self) -> float | None:
        return (self.passed / self.conclusive) if self.conclusive else None

    def add(self, scenario: str, status: str, severity: str | None) -> None:
        self.total += 1
        bucket = {"PASS": "passed", "FAIL": "failed", "INVALID_RUN": "invalid"}.get(
            status, "infra"
        )
        setattr(self, bucket, getattr(self, bucket) + 1)
        if status == "FAIL" and severity == "P0":
            self.p0 += 1
        if status == "FAIL" and severity == "P1":
            self.p1 += 1
        sc = self.by_scenario.setdefault(
            scenario, {"pass": 0, "fail": 0, "invalid": 0, "infra": 0}
        )
        sc[{"passed": "pass", "failed": "fail", "invalid": "invalid"}.get(bucket, "infra")] += 1


def _load_run(run_dir: Path) -> dict[str, Any] | None:
    try:
        man = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        cls = json.loads((run_dir / "classification.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        # kernel field added by run.py for EPIC K; pre-K runs default to disco.
        "kernel": str(man.get("kernel") or "disco"),
        "scenario": str(man.get("scenario_id") or "?"),
        "status": str(cls.get("status") or "?"),
        "severity": cls.get("severity"),
        "run_id": str(man.get("run_id") or run_dir.name),
    }


def scan_runs(runs_root: Path, scenario: str | None = None) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return runs
    for d in sorted(runs_root.iterdir()):
        if not (d.is_dir() and (d / "classification.json").exists()):
            continue
        r = _load_run(d)
        if r is None:
            continue
        if scenario and r["scenario"] != scenario:
            continue
        runs.append(r)
    return runs


def tally(runs: list[dict[str, Any]]) -> dict[str, KernelTally]:
    out: dict[str, KernelTally] = {}
    for r in runs:
        t = out.setdefault(r["kernel"], KernelTally(kernel=r["kernel"]))
        t.add(r["scenario"], r["status"], r["severity"])
    return out


def evaluate_gate(tallies: dict[str, KernelTally]) -> dict[str, Any]:
    """The §K3 criteria computable from soak evidence. (Security/no-key/no-user-
    packages/process-backend criteria are SEPARATE audits, not soak counts — listed
    as manual.) PiKernel is promotion-ELIGIBLE on these only if it ties-or-beats
    DiscoKernel on pass rate and carries no P0/P1."""
    pi, disco = tallies.get("pi"), tallies.get("disco")
    checks: list[dict[str, Any]] = []

    def chk(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    if pi is None or disco is None:
        chk(
            "both kernels present",
            False,
            f"need pi AND disco runs; have {sorted(tallies)}",
        )
        return {"eligible": False, "checks": checks}

    chk("pi: zero P0", pi.p0 == 0, f"{pi.p0} P0 failures")
    chk("pi: zero P1", pi.p1 == 0, f"{pi.p1} P1 failures")
    pr_pi, pr_d = pi.pass_rate, disco.pass_rate
    if pr_pi is None or pr_d is None:
        chk("pass-rate comparable", None, "a kernel has no conclusive runs")
    else:
        chk(
            "pi pass-rate >= disco (bare build)",
            pr_pi >= pr_d,
            f"pi {pr_pi:.1%} vs disco {pr_d:.1%}",
        )
    eligible = all(c["ok"] for c in checks if c["ok"] is not None) and not any(
        c["ok"] is False for c in checks
    )
    return {"eligible": eligible, "checks": checks}


def _fmt_rate(t: KernelTally) -> str:
    pr = t.pass_rate
    return f"{pr:.1%}" if pr is not None else "n/a"


def render(tallies: dict[str, KernelTally], gate: dict[str, Any]) -> str:
    lines = ["EPIC K — Build-Soak bake-off scorecard", "=" * 42]
    if not tallies:
        lines.append("no runs found.")
        return "\n".join(lines)
    for k in sorted(tallies):
        t = tallies[k]
        lines.append(
            f"\n[{k}]  pass-rate {_fmt_rate(t)}  "
            f"(PASS {t.passed} / FAIL {t.failed} / INVALID {t.invalid} / INFRA {t.infra}"
            f"  · P0 {t.p0} · P1 {t.p1} · n={t.total})"
        )
        for sc in sorted(t.by_scenario):
            c = t.by_scenario[sc]
            lines.append(f"    {sc:30} P{c['pass']} F{c['fail']} I{c['invalid']} X{c['infra']}")
    lines.append("\n— §K3 promotion gate (soak-computable criteria) —")
    for c in gate["checks"]:
        mark = {True: "PASS", False: "FAIL", None: "n/a "}[c["ok"]]
        lines.append(f"  [{mark}] {c['check']}: {c['detail']}")
    lines.append(
        f"\nPiKernel promotion-eligible (soak criteria): "
        f"{'YES' if gate['eligible'] else 'NO'}"
    )
    lines.append(
        "  (manual/separate: no raw provider keys, no user Pi packages, no process-"
        "backend prod runs, AppKit + preview + event-mapping suites)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EPIC K bake-off scorecard (PiKernel vs DiscoKernel).")
    p.add_argument("--runs", default="test-record/build-soak", help="run-folder root")
    p.add_argument("--scenario", default=None, help="restrict to one scenario id")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = p.parse_args(argv)

    runs = scan_runs(Path(args.runs), args.scenario)
    tallies = tally(runs)
    gate = evaluate_gate(tallies)
    if args.json:
        payload = {
            "runs": len(runs),
            "kernels": {
                k: {
                    "total": t.total,
                    "pass": t.passed,
                    "fail": t.failed,
                    "invalid": t.invalid,
                    "infra": t.infra,
                    "p0": t.p0,
                    "p1": t.p1,
                    "pass_rate": t.pass_rate,
                    "by_scenario": t.by_scenario,
                }
                for k, t in tallies.items()
            },
            "gate": gate,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render(tallies, gate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
