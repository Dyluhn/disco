"""Amendment A6.1 — the scope gauge must be honest in both directions.

These tests exist because the failure mode being guarded is a *quiet* one: a
batch summary that presents a scenario as governed when the run could not
evaluate its claims. Every case below asserts either that an unproven capability
never licenses a claim, or that the gauge cannot drift away from the product's
own definition of which claims need a browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from harness.build_soak.browser_capability import (
    BROWSER_RUNTIME_CLAIM_KINDS,
    CAPABILITY_AVAILABLE,
    CAPABILITY_UNAVAILABLE,
    CAPABILITY_UNPROVEN,
    EVALUATED,
    NOT_EVALUATED,
    BrowserCapability,
    capability_from_mapping,
    declared_browser_claims,
    load_capability,
    scenario_scope,
    scope_declaration,
)

_PHASE4 = Path(__file__).resolve().parents[1] / "scenarios_phase4.yaml"


def _capability(state: str) -> BrowserCapability:
    return BrowserCapability(state=state, engine="firefox", evidence="probe")


def _appkit_scenario() -> dict[str, Any]:
    raw = yaml.safe_load(_PHASE4.read_text(encoding="utf-8"))
    for scenario in raw["scenarios"]:
        if scenario.get("id") == "p4_appkit_restart":
            return scenario
    raise AssertionError("p4_appkit_restart missing from scenarios_phase4.yaml")


def test_claim_kind_set_is_pinned_to_the_products_own_authority() -> None:
    """The gauge must not drift from `disco.core.verification`.

    The harness restates the product's private browser-runtime claim set rather
    than importing it across a package boundary. That is only safe if drift
    fails loudly, which is what this pin does.
    """

    from disco.core.verification import (  # noqa: PLC0415 — pinned at call time
        _STRUCTURED_BROWSER_RUNTIME_CLAIM_KINDS,
    )

    product = {kind.value for kind in _STRUCTURED_BROWSER_RUNTIME_CLAIM_KINDS}
    assert set(BROWSER_RUNTIME_CLAIM_KINDS) == product


def test_http_ready_is_browser_dependent() -> None:
    """Guards a specific misreading that would understate the gap.

    `http_ready` looks like a plain socket check and is not one: the product
    classifies it as a structured-browser-runtime claim. Excluding it from this
    set would let a headless run claim it had evaluated something it had not.
    """

    assert "http_ready" in BROWSER_RUNTIME_CLAIM_KINDS


def test_real_appkit_scenario_declares_four_browser_claims() -> None:
    claims = declared_browser_claims(_appkit_scenario())
    assert set(claims) == {"http_ready", "rendered_content", "console_clean", "network_clean"}


def test_unproven_capability_never_reads_evaluated() -> None:
    """The core fail-closed property: silence is not success."""

    scope = scenario_scope(_appkit_scenario(), _capability(CAPABILITY_UNPROVEN))
    assert scope["browser_dependent_claims_evaluability"] == NOT_EVALUATED
    assert scope["structurally_evaluable_headless"] is False


def test_unavailable_capability_reads_not_evaluated() -> None:
    scope = scenario_scope(_appkit_scenario(), _capability(CAPABILITY_UNAVAILABLE))
    assert scope["browser_dependent_claims_evaluability"] == NOT_EVALUATED


def test_available_capability_reads_evaluated() -> None:
    scope = scenario_scope(_appkit_scenario(), _capability(CAPABILITY_AVAILABLE))
    assert scope["browser_dependent_claims_evaluability"] == EVALUATED
    assert scope["structurally_evaluable_headless"] is True


def test_scenario_without_browser_claims_is_evaluable_under_any_capability() -> None:
    """A scenario that declares no browser claim is not penalised by a missing
    probe — the gauge reports the gap it can prove, not a blanket pessimism."""

    plain: dict[str, Any] = {"assertions": {"workspace": {"files": []}}}
    for state in (CAPABILITY_AVAILABLE, CAPABILITY_UNAVAILABLE, CAPABILITY_UNPROVEN):
        assert scenario_scope(plain, _capability(state))["structurally_evaluable_headless"] is True


def test_scope_block_carries_no_pass_or_fail_verdict() -> None:
    """The gauge declares evaluability only. If it could ever say 'pass' it
    would be a place to launder a red, which is the thing A6 forbids."""

    scope = scope_declaration(
        {"p4_appkit_restart": _appkit_scenario()}, _capability(CAPABILITY_UNAVAILABLE)
    )
    blob = json.dumps(scope).lower()
    assert "pass" not in blob
    assert "exempt" not in blob
    assert scope["full_scope"] is False
    assert scope["not_evaluated_claim_kinds"] == [
        "console_clean",
        "http_ready",
        "network_clean",
        "rendered_content",
    ]


def test_full_scope_true_only_when_nothing_was_skipped() -> None:
    selected = {"p4_appkit_restart": _appkit_scenario()}
    assert scope_declaration(selected, _capability(CAPABILITY_AVAILABLE))["full_scope"] is True
    assert scope_declaration(selected, _capability(CAPABILITY_UNPROVEN))["full_scope"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "definitely-fine"},
        {"state": ""},
        {},
        {"state": "AVAILABLE"},
    ],
)
def test_unknown_probe_state_degrades_to_unproven(payload: dict[str, Any]) -> None:
    """An unrecognised verdict must not be read optimistically."""

    assert capability_from_mapping(payload).state == CAPABILITY_UNPROVEN


def test_missing_probe_path_is_unproven_not_available() -> None:
    assert load_capability(None).state == CAPABILITY_UNPROVEN
    assert load_capability("").state == CAPABILITY_UNPROVEN


def test_unreadable_probe_is_unproven_and_does_not_raise(tmp_path: Path) -> None:
    """A broken probe must degrade the gauge, never fail the batch: the scope
    block reports what is unknown, it does not decide the run's verdict."""

    missing = tmp_path / "nope.json"
    assert load_capability(str(missing)).state == CAPABILITY_UNPROVEN

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json", encoding="utf-8")
    assert load_capability(str(malformed)).state == CAPABILITY_UNPROVEN

    not_object = tmp_path / "list.json"
    not_object.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_capability(str(not_object)).state == CAPABILITY_UNPROVEN


def test_recorded_probe_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "cap.json"
    path.write_text(
        json.dumps({"state": CAPABILITY_UNAVAILABLE, "engine": "chromium", "evidence": "boom"}),
        encoding="utf-8",
    )
    capability = load_capability(str(path))
    assert capability.state == CAPABILITY_UNAVAILABLE
    assert capability.engine == "chromium"
    assert capability.evidence == "boom"
    assert capability.to_dict()["evidence"] == "boom"
