"""EPIC K bake-off scorecard gate logic (§K3)."""
from build_soak.bakeoff import evaluate_gate, tally


def _runs(kernel, scenario, statuses, severities=None):
    severities = severities or [None] * len(statuses)
    return [
        {"kernel": kernel, "scenario": scenario, "status": s, "severity": sev,
         "run_id": f"{kernel}-{i}"}
        for i, (s, sev) in enumerate(zip(statuses, severities, strict=True))
    ]


def test_pi_ties_or_beats_disco_is_eligible():
    runs = _runs("pi", "bare", ["PASS", "PASS", "FAIL"]) + _runs("disco", "bare", ["PASS", "FAIL", "FAIL"])
    assert evaluate_gate(tally(runs))["eligible"] is True  # pi 2/3 >= disco 1/3, no P0/P1


def test_pi_p0_blocks_eligibility():
    runs = _runs("pi", "bare", ["PASS", "FAIL"], [None, "P0"]) + _runs("disco", "bare", ["PASS", "PASS"])
    assert evaluate_gate(tally(runs))["eligible"] is False


def test_pi_worse_pass_rate_blocks():
    runs = _runs("pi", "bare", ["PASS", "FAIL", "FAIL"]) + _runs("disco", "bare", ["PASS", "PASS", "PASS"])
    assert evaluate_gate(tally(runs))["eligible"] is False


def test_invalid_infra_excluded_from_rate():
    t = tally(_runs("pi", "bare", ["PASS", "INVALID_RUN", "INFRA_FAILURE"]))["pi"]
    assert t.pass_rate == 1.0  # only the PASS counts; INVALID/INFRA inconclusive
    assert t.total == 3


def test_missing_kernel_blocks():
    assert evaluate_gate(tally(_runs("disco", "bare", ["PASS"])))["eligible"] is False
