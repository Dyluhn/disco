"""The per-scenario driver context-window pin (F20's engineered pressure).

The pin exists because `require_compaction` is a claim about the product under
CONTEXT PRESSURE, and the product derives both the condenser trigger and the
read/snip caps from the driver's configured window. On a 1,000,000-token driver
the derived caps outrun anything these scenarios generate, so the assertion
became unfalsifiable-by-configuration rather than false.

These tests pin the two halves that make the mechanism honest: the scenario's
declaration SELECTS the pinned driver, and the run's own spans PROVE the window
it actually used. The second half is the essential one — the product resolves
an unknown `model_override` by falling back to the default driver silently.
"""

from __future__ import annotations

import pytest

from harness.build_soak._runner.drive_start import start_scenario
from harness.build_soak._runner.scenario_io import (
    ContextWindowPinError,
    _driver_context_window_pin,
    _observed_driver_context_windows,
    _verify_driver_context_window_pin,
    load_scenarios,
)
from harness.build_soak._test_support.api_runner.helpers_core import _client
from harness.build_soak._test_support.api_runner.helpers_transport import FakeTransport

_SCENARIOS_PHASE4 = "development/harness/build_soak/scenarios_phase4.yaml"


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


# ---- selection: the pin must actually reach the product ----------------------


@pytest.mark.asyncio
async def test_imported_conversation_gets_the_model_override_before_the_kick(tmp_path) -> None:
    """`/api/projects/import` takes no model field and creates a PRISTINE cid, so
    the requested driver used to be dropped on this path entirely — every
    imported-fixture scenario ran on the stack's configured default while the run
    recorded the model it had asked for. The pick now goes through the product's
    own pre-kick settings route, BEFORE the message that kicks the loop."""
    transport = FakeTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)

    await client.create_build_conversation(
        "Audit the imported catalog.",
        model="pinned-key",
        import_fixture={"filename": "sample.zip", "files": {"catalog.txt": "seed"}},
    )

    assert transport.patches == [
        (f"/conversations/{transport.cid}/settings", {"model_override": "pinned-key"})
    ]
    # ordering matters: settings are cached at first kick, so a PATCH after the
    # message would be applied to a loop that had already composed.
    assert transport.posts[0][0] == "/api/projects/import"
    assert transport.posts[1][0] == f"/conversations/{transport.cid}/messages"


@pytest.mark.asyncio
async def test_a_refused_model_override_is_fatal_not_silent(tmp_path) -> None:
    transport = FakeTransport(tmp_path / "disco.db", states=["RUNNING"])

    async def _refuse(path, body):
        transport.patches.append((path, body))
        return 409, {"reason": "imported_read_only"}

    transport.patch_json = _refuse
    client = _client(transport, tmp_path)

    with pytest.raises(RuntimeError, match="model override failed"):
        await client.create_build_conversation(
            "Audit the imported catalog.",
            model="pinned-key",
            import_fixture={"filename": "sample.zip", "files": {"catalog.txt": "seed"}},
        )


@pytest.mark.asyncio
async def test_a_pinned_scenario_overrides_the_run_level_model(tmp_path) -> None:
    """The pin is per-SCENARIO: the rest of the matrix keeps the run's driver."""
    transport = FakeTransport(tmp_path / "disco.db", states=["RUNNING"])
    client = _client(transport, tmp_path)
    scenario = {
        "id": "pinned",
        "prompt": "go",
        "driver_context_window": {"model": "pinned-key", "window": 32768},
        "import_fixture": {"filename": "s.zip", "files": {"catalog.txt": "seed"}},
    }

    from harness.build_soak._runner.bindings import ScenarioBindings
    from harness.build_soak._runner.drive_start import DriveAudit
    from harness.build_soak._runner.ledger import _relay_log_path
    from harness.build_soak._runner.triggers import _drive_to_terminal

    audit = DriveAudit()
    await start_scenario(
        client,
        scenario,
        ScenarioBindings(_drive_to_terminal, _relay_log_path),
        audit,
        model="run-level-key",
        autonomous=False,
        seed=1,
    )

    assert transport.patches == [
        (f"/conversations/{transport.cid}/settings", {"model_override": "pinned-key"})
    ]
    assert any("pinned driver context window: model=pinned-key" in line for line in audit.timeline)


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
