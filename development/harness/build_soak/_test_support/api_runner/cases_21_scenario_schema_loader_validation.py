"""Moved scenario schema loader validation collection implementations."""

from __future__ import annotations

from ._shared import (
    _ASSERTION_KEYS,
    _EVENT_CHAIN_KEYS,
    _FOLLOWUP_TRIGGERS,
    _TERMINAL_VOCAB,
    Path,
    load_scenarios,
)


def _assert_scenario_identity(sid, scenario) -> None:
    assert scenario.get("id") == sid
    assert isinstance(scenario.get("prompt"), str) and scenario["prompt"].strip(), sid
    assert scenario.get("mode") == "api", sid
    assertions = scenario.get("assertions") or {}
    assert isinstance(assertions, dict) and assertions, f"{sid}: assertions missing"
    assert set(assertions) <= _ASSERTION_KEYS, (
        f"{sid}: unknown keys {set(assertions) - _ASSERTION_KEYS}"
    )


def _assert_scenario_policy(sid, scenario, assertions) -> None:
    thrash = assertions.get("thrash") or {}
    assert set(thrash) == {
        "max_identical_action_repeats",
        "max_same_tool_error_repeats",
        "max_actionless_pauses",
        "max_same_model_repair_repeats",
        "max_total_model_repairs",
    }, f"{sid}: malformed thrash policy"
    assert all(isinstance(value, int) and value >= 0 for value in thrash.values()), sid
    assert scenario.get("requires_inspect_trace") is True, sid

    if sid == "static_html_minimal":
        scope = assertions.get("tool_scope") or {}
        assert scope.get("planning_disallows"), scope
    else:
        assert "tool_scope" not in assertions, f"{sid}: unexpected tool_scope assertion"


def _assert_scenario_contract(sid, assertions) -> None:
    event_chain = assertions.get("event_chain") or {}
    assert set(event_chain) <= _EVENT_CHAIN_KEYS, f"{sid}: bad event_chain keys"

    terminal = assertions.get("terminal_status_in")
    assert isinstance(terminal, list) and terminal, f"{sid}: terminal_status_in required"
    assert set(terminal) <= _TERMINAL_VOCAB, (
        f"{sid}: bad terminal {set(terminal) - _TERMINAL_VOCAB}"
    )

    for spec in (assertions.get("workspace") or {}).get("files") or []:
        assert isinstance(spec.get("path"), str) and spec["path"], f"{sid}: file needs a path"
        must_contain = spec.get("must_contain") or []
        assert isinstance(must_contain, list) and all(
            isinstance(item, str) for item in must_contain
        ), sid


def _assert_scenario_preview(sid, scenario, assertions) -> None:
    preview = assertions.get("preview") or {}
    if preview:
        assert isinstance(preview.get("required"), bool), f"{sid}: preview.required must be bool"
        assert all(isinstance(item, str) for item in (preview.get("must_contain") or [])), sid

    browser_verification = assertions.get("browser_verification")
    if browser_verification is not None:
        assert browser_verification == {"required": True}, sid
        prompt = scenario["prompt"].lower()
        assert "browser" in prompt and "verif" in prompt, (
            f"{sid}: browser-verification assertion must be disclosed in the prompt"
        )


def _assert_scenario_followups(sid, scenario, assertions) -> None:
    followups = scenario.get("followups") or []
    for followup in followups:
        assert isinstance(followup.get("text"), str) and followup["text"], (
            f"{sid}: followup needs text"
        )
        assert followup.get("trigger") in _FOLLOWUP_TRIGGERS, (
            f"{sid}: bad trigger {followup.get('trigger')}"
        )
    if any(followup.get("requires_plan_revision") for followup in followups):
        assert "expected_final_plan_revision" in (assertions.get("revisions") or {}), (
            f"{sid}: revision followups need revisions.expected_final_plan_revision"
        )


def _impl_test_every_scenario_loads_with_a_valid_schema():
    # Every scenario in scenarios.yaml must load AND be well-formed against the shape the
    # oracles consume — so a typo'd key / missing terminal vocab / unsatisfiable contract
    # can't slip in. Mirrors the ContractOracle / OutputTruthOracle / RevisionOracle fields.
    scen = load_scenarios()
    assert scen, "no scenarios loaded"
    # all four originals + the three new ones are present
    expected = {
        "static_html_minimal",
        "must_plan_before_tool",
        "revise_after_finish",
        "steer_while_running_requires_plan_update_or_clear_execution_note",
        "multifile_static_site",
        "revise_twice_complex",
        "verify_catches_broken_then_fixed",
    }
    assert expected <= set(scen), sorted(set(scen) ^ expected)

    for sid, s in scen.items():
        a = s.get("assertions") or {}
        _assert_scenario_identity(sid, s)
        _assert_scenario_policy(sid, s, a)
        _assert_scenario_contract(sid, a)
        _assert_scenario_preview(sid, s, a)
        _assert_scenario_followups(sid, s, a)

    assert {
        sid for sid, scenario in scen.items() if "browser_verification" in scenario["assertions"]
    } == {
        "static_html_minimal",
        "verify_catches_broken_then_fixed",
        "diag_form_verify",
        "diag_devserver",
    }


def _impl_test_agent_general_task_prompt_discloses_literal_source_assertion():
    scenario = load_scenarios()["agent_general_task"]
    prompt = scenario["prompt"]
    inventory = next(
        spec
        for spec in scenario["assertions"]["workspace"]["files"]
        if spec["path"] == "inventory.py"
    )

    assert "source file itself must include" in prompt
    assert "rather than constructing the required output dynamically" in prompt
    assert all(marker in prompt for marker in inventory["must_contain"])


def _impl_test_rel6_draft_cancel_at_uses_followup_trigger_vocabulary():
    scen = load_scenarios(Path(__file__).resolve().parents[2] / "scenarios_rel6_draft.yaml")
    s = scen["disconnect_cancel_recovery"]
    assert s["cancel_at"]["trigger"] in _FOLLOWUP_TRIGGERS
    assert s["cancel_at"]["trigger"] == "after_first_file_write"
    assert [f["trigger"] for f in s["followups"]] == ["after_terminal"]


def _impl_test_new_scenarios_assert_deterministic_oracle_checkable_output():
    # The three new scenarios must each carry a deterministically-checkable output oracle
    # expectation (specific files with must_contain markers and/or a required preview) — no
    # vague asserts that the OutputTruthOracle could not verify.
    scen = load_scenarios()
    new_ids = ("multifile_static_site", "revise_twice_complex", "verify_catches_broken_then_fixed")
    for sid in new_ids:
        a = scen[sid]["assertions"]
        files = (a.get("workspace") or {}).get("files") or []
        # at least one declared file with concrete must_contain markers
        assert files, f"{sid}: must declare workspace files"
        assert any(spec.get("must_contain") for spec in files), f"{sid}: needs must_contain markers"
        assert a.get("terminal_status_in"), sid

    # multifile: three distinct files, each with markers; preview required.
    ms = scen["multifile_static_site"]["assertions"]
    paths = {spec["path"] for spec in ms["workspace"]["files"]}
    assert {"index.html", "about.html", "style.css"} <= paths
    assert ms["preview"]["required"] is True

    # revise_twice: two revision-requiring followups → expected_final_plan_revision == 3.
    rt = scen["revise_twice_complex"]
    assert sum(bool(f.get("requires_plan_revision")) for f in rt["followups"]) == 2
    assert rt["assertions"]["revisions"]["expected_final_plan_revision"] == 3
