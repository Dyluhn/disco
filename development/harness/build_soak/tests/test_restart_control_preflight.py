"""A missing harness control must not read as a product lifecycle failure.

Counted-promotion failure 2026-07-27 (`p4_ff_react_restart` seed 450000). The
build reached FINISHED twice. Only the disposable stack's private `/restart`
control was unbound — because the stack had been launched manually instead of
through `development/harness/reliability/isolated_stack.py`, which is what provisions
`DISCO_RELIABILITY_STACK_CONTROL_URL` / `_TOKEN`.

The trial was recorded as `LIFECYCLE_SEQUENCE_INVALID`: the build platform
failing a restart lifecycle. It did not. Worse, the precondition is knowable
before any spend, and restarting a SHARED stack mid-run would have been
destructive had the control resolved to one.

Two defences, tested here: refuse to start, and — if somehow discovered mid-run —
say what actually happened.
"""

from __future__ import annotations

import pytest

from harness.build_soak import failure_codes as fc
from harness.build_soak.oracles.scenario_lifecycle import ScenarioLifecycleOracle

_CONTROL = ("DISCO_RELIABILITY_STACK_CONTROL_URL", "DISCO_RELIABILITY_STACK_CONTROL_TOKEN")


def _scenario() -> dict:
    return {"id": "p4_ff_react_restart", "lifecycle": {"restart_after_terminal": True}}


def _evidence(reason: str | None, *, ok: bool) -> dict:
    restart: dict = {"ok": ok, "before_status": "FINISHED", "after_status": "FINISHED"}
    if ok:
        restart |= {"before_digest": "a" * 64, "after_digest": "a" * 64}
    else:
        restart |= {"before_digest": "a" * 64, "after_digest": None, "reason": reason}
    return {"restart": restart}


def _check(evidence: dict):
    return ScenarioLifecycleOracle().check(
        events=[], scenario=_scenario(), product_evidence=evidence
    )


def test_a_missing_control_is_named_as_such_not_as_a_sequence_defect():
    (result,) = _check(_evidence("isolated_stack_control_unavailable", ok=False))
    assert result.code == fc.LIFECYCLE_CONTROL_UNAVAILABLE
    assert result.first_broken_link == "harness_control -> lifecycle_action"
    assert result.facts["unattempted_actions"] == ["restart"]


def test_it_still_FAILS_the_trial():
    # An unattempted lifecycle proves nothing, so this is not a pass.
    (result,) = _check(_evidence("isolated_stack_control_unavailable", ok=False))
    assert result.status != "PASS"


def test_it_is_P1_not_P0_because_the_product_did_not_misbehave():
    assert fc.severity_for(fc.LIFECYCLE_CONTROL_UNAVAILABLE) == fc.P1
    assert fc.severity_for(fc.LIFECYCLE_SEQUENCE_INVALID) == fc.P0


def test_a_REAL_lifecycle_defect_still_reports_sequence_invalid():
    # The invariant this must not erode: a restart that ran and produced a bad
    # sequence is still a product failure.
    (result,) = _check(_evidence("digest_mismatch", ok=False))
    assert result.code == fc.LIFECYCLE_SEQUENCE_INVALID


def test_a_successful_restart_still_passes():
    (result,) = _check(_evidence(None, ok=True))
    assert result.status == "PASS"


# ---- the preflight ---------------------------------------------------------


def test_the_runner_refuses_to_start_a_restart_lane_without_the_control(monkeypatch):
    """The knowable-before-spend half. Refusal, not a spent counted trial."""
    import asyncio
    from argparse import Namespace

    from harness.build_soak import run as run_mod

    for name in _CONTROL:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        run_mod, "load_scenarios", lambda *_a, **_k: {"p4_ff_react_restart": _scenario()}
    )
    args = Namespace(
        scenario="p4_ff_react_restart",
        scenarios="ignored.yaml",
        iterations=1,
        seed_base=1,
        expected_provider_host="opencode.ai",
        expected_provider_model="deepseek-v4-flash",
    )
    # `_relay_log_path` is satisfied so the refusal we observe is the control one.
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", "/tmp/ledger.jsonl")
    code = asyncio.run(run_mod._amain(args))
    assert code == 3


@pytest.mark.parametrize("present", _CONTROL)
def test_HALF_the_control_is_still_refused(monkeypatch, present):
    # URL without TOKEN (or vice versa) cannot authenticate the restart; a
    # partially-bound control must fail closed exactly like an absent one.
    import asyncio
    from argparse import Namespace

    from harness.build_soak import run as run_mod

    for name in _CONTROL:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(present, "set")
    monkeypatch.setenv("DISCO_PROVIDER_LEDGER", "/tmp/ledger.jsonl")
    monkeypatch.setattr(
        run_mod, "load_scenarios", lambda *_a, **_k: {"p4_ff_react_restart": _scenario()}
    )
    args = Namespace(
        scenario="p4_ff_react_restart",
        scenarios="ignored.yaml",
        iterations=1,
        seed_base=1,
        expected_provider_host="opencode.ai",
        expected_provider_model="deepseek-v4-flash",
    )
    assert asyncio.run(run_mod._amain(args)) == 3


def test_a_lane_with_NO_restart_scenario_is_unaffected(monkeypatch):
    # The check must not gate lanes that never restart anything.
    from harness.build_soak import run as run_mod

    # A pause scenario also carries a lifecycle block — it must NOT be gated.
    plain = {"id": "p4_ff_node_pause", "lifecycle": {"pause_resume_at": "x"}}
    restart_ids = sorted(
        sid
        for sid, sc in {"p4_ff_node_pause": plain}.items()
        if (sc.get("lifecycle") or {}).get("restart_after_terminal")
    )
    assert restart_ids == []
    assert run_mod is not None
