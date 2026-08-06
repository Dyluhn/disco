"""The per-scenario driver context-window pin (F20's engineered pressure).

The pin exists because `require_compaction` is a claim about the product under
CONTEXT PRESSURE, and the product derives both the condenser trigger and the
read/snip caps from the driver's configured window. On a 1,000,000-token driver
the derived caps outrun anything these scenarios generate, so the assertion
became unfalsifiable-by-configuration rather than false.

These tests pin the two halves that make the mechanism honest: the scenario's
declaration SELECTS the pinned driver, and the run's own spans PROVE the window
it actually used. The second half is the load-bearing one — the product resolves
an unknown `model_override` by falling back to the default driver silently.
"""

from __future__ import annotations

import pytest

from harness.build_soak._runner.scenario_io import (
    ContextWindowPinError,
    _driver_context_window_pin,
    _observed_driver_context_windows,
    _verify_driver_context_window_pin,
    load_scenarios,
)

_SCENARIOS_PHASE4 = "harness/build_soak/scenarios_phase4.yaml"


def _trace(*windows: int) -> dict:
    return {
        "spans": [
            {"span": "agent.step", "event": "start", "driver_context_window": w} for w in windows
        ]
    }


# ---- declaration -------------------------------------------------------------


def test_absent_declaration_is_no_pin() -> None:
    assert _driver_context_window_pin({"id": "s"}) is None


def test_declaration_returns_model_and_window() -> None:
    scenario = {"driver_context_window": {"model": "pinned-key", "window": 32768}}
    assert _driver_context_window_pin(scenario) == ("pinned-key", 32768)


@pytest.mark.parametrize(
    "declared",
    [
        32768,  # bare int: no catalogue key to select, so nothing would be pinned
        {"window": 32768},  # no model
        {"model": "pinned-key"},  # no window to check the run against
        {"model": "", "window": 32768},
        {"model": "pinned-key", "window": 0},
        {"model": "pinned-key", "window": -1},
        {"model": "pinned-key", "window": "32768"},
        {"model": "pinned-key", "window": True},  # bool is an int subclass
    ],
)
def test_malformed_declaration_raises_rather_than_dropping_the_pin(declared: object) -> None:
    """A dropped pin is the exact failure the pin prevents: the run would still
    complete, on the unpinned default driver, and report an honest-looking red."""
    with pytest.raises(ValueError):
        _driver_context_window_pin({"driver_context_window": declared})


# ---- observation -------------------------------------------------------------


def test_observed_windows_read_only_agent_step_spans() -> None:
    trace = {
        "spans": [
            {"span": "context_pack", "driver_context_window": 999},
            {"span": "agent.step", "driver_context_window": 32768},
            {"span": "agent.step", "driver_context_window": 32768},
            {"span": "agent.step"},  # no window recorded
            "not-a-span",
        ]
    }
    assert _observed_driver_context_windows(trace) == [32768, 32768]


def test_observed_windows_tolerate_a_traceless_shape() -> None:
    assert _observed_driver_context_windows({}) == []
    assert _observed_driver_context_windows({"spans": "nope"}) == []


# ---- verification ------------------------------------------------------------


def test_no_declaration_verifies_to_none() -> None:
    assert _verify_driver_context_window_pin({"id": "s"}, _trace(1_000_000)) is None


def test_honoured_pin_records_the_facts() -> None:
    scenario = {"driver_context_window": {"model": "pinned-key", "window": 32768}}
    facts = _verify_driver_context_window_pin(scenario, _trace(32768, 32768, 32768))
    assert facts == {
        "model": "pinned-key",
        "declared_window": 32768,
        "steps_observed": 3,
        "distinct_windows": [32768],
    }


def test_silent_fallback_to_the_default_driver_is_caught() -> None:
    """The regression this exists for: `model_override` naming a catalogue key the
    stack does not carry resolves to the configured default, so the run measures
    the 1M window while looking pinned."""
    scenario = {"driver_context_window": {"model": "absent-key", "window": 32768}}
    with pytest.raises(ContextWindowPinError) as excinfo:
        _verify_driver_context_window_pin(scenario, _trace(1_000_000, 1_000_000))
    assert excinfo.value.facts["distinct_windows"] == [1_000_000]
    assert excinfo.value.facts["declared_window"] == 32768


def test_a_window_that_changes_mid_run_is_caught() -> None:
    scenario = {"driver_context_window": {"model": "pinned-key", "window": 32768}}
    with pytest.raises(ContextWindowPinError):
        _verify_driver_context_window_pin(scenario, _trace(32768, 1_000_000))


def test_missing_trace_with_a_declared_pin_raises() -> None:
    scenario = {"driver_context_window": {"model": "pinned-key", "window": 32768}}
    with pytest.raises(ContextWindowPinError):
        _verify_driver_context_window_pin(scenario, None)


def test_a_run_with_no_driver_calls_is_not_a_pin_error() -> None:
    """No driver call means no measurement to be fooled by; any assertion that
    needed the pressure fails on its own terms instead of being masked."""
    scenario = {"driver_context_window": {"model": "pinned-key", "window": 32768}}
    facts = _verify_driver_context_window_pin(scenario, {"spans": []})
    assert facts is not None
    assert facts["steps_observed"] == 0
    assert facts["distinct_windows"] == []


# ---- the governed scenarios that carry it ------------------------------------


def test_both_context_pressure_scenarios_declare_the_pin() -> None:
    """`require_compaction` and the pin are one claim: the scenarios that assert
    compaction are exactly the scenarios that engineer the pressure for it."""
    scenarios = load_scenarios(_SCENARIOS_PHASE4)
    asserting = {
        sid
        for sid, s in scenarios.items()
        if ((s.get("assertions") or {}).get("context_pressure") or {}).get("require_compaction")
        is True
    }
    assert asserting == {"p4_ff_context_catalog", "p4_ff_context_ledger"}
    for sid in sorted(asserting):
        pin = _driver_context_window_pin(scenarios[sid])
        assert pin is not None, f"{sid} asserts compaction without engineering pressure"
        _model, window = pin
        assert window == 32768
