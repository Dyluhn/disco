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
        assert s.get("id") == sid
        assert isinstance(s.get("prompt"), str) and s["prompt"].strip(), sid
        assert s.get("mode") == "api", sid
        a = s.get("assertions") or {}
        assert isinstance(a, dict) and a, f"{sid}: assertions missing"
        assert set(a) <= _ASSERTION_KEYS, f"{sid}: unknown keys {set(a) - _ASSERTION_KEYS}"

        thrash = a.get("thrash") or {}
        assert set(thrash) == {
            "max_identical_action_repeats",
            "max_same_tool_error_repeats",
            "max_actionless_pauses",
            "max_same_model_repair_repeats",
            "max_total_model_repairs",
        }, f"{sid}: malformed thrash policy"
        assert all(isinstance(value, int) and value >= 0 for value in thrash.values()), sid
        assert s.get("requires_inspect_trace") is True, sid

        if sid == "static_html_minimal":
            scope = a.get("tool_scope") or {}
            assert scope.get("planning_disallows"), scope
        else:
            assert "tool_scope" not in a, f"{sid}: unexpected tool_scope assertion"

        ec = a.get("event_chain") or {}
        assert set(ec) <= _EVENT_CHAIN_KEYS, f"{sid}: bad event_chain keys"

        term = a.get("terminal_status_in")
        assert isinstance(term, list) and term, f"{sid}: terminal_status_in required"
        assert set(term) <= _TERMINAL_VOCAB, f"{sid}: bad terminal {set(term) - _TERMINAL_VOCAB}"

        # workspace.files: every file has a path + (optional) list-of-str must_contain.
        for spec in (a.get("workspace") or {}).get("files") or []:
            assert isinstance(spec.get("path"), str) and spec["path"], f"{sid}: file needs a path"
            mc = spec.get("must_contain") or []
            assert isinstance(mc, list) and all(isinstance(x, str) for x in mc), sid

        # preview: required is bool; must_contain (if any) is a list of str.
        prev = a.get("preview") or {}
        if prev:
            assert isinstance(prev.get("required"), bool), f"{sid}: preview.required must be bool"
            assert all(isinstance(x, str) for x in (prev.get("must_contain") or [])), sid

        browser_verification = a.get("browser_verification")
        if browser_verification is not None:
            assert browser_verification == {"required": True}, sid
            prompt = s["prompt"].lower()
            assert "browser" in prompt and "verif" in prompt, (
                f"{sid}: browser-verification assertion must be disclosed in the prompt"
            )

        # followups: each has a text + a known trigger; if ANY requires a plan revision the
        # ContractOracle requires assertions.revisions.expected_final_plan_revision.
        followups = s.get("followups") or []
        for f in followups:
            assert isinstance(f.get("text"), str) and f["text"], f"{sid}: followup needs text"
            assert f.get("trigger") in _FOLLOWUP_TRIGGERS, f"{sid}: bad trigger {f.get('trigger')}"
        if any(f.get("requires_plan_revision") for f in followups):
            assert "expected_final_plan_revision" in (a.get("revisions") or {}), (
                f"{sid}: revision followups need revisions.expected_final_plan_revision"
            )

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
    scen = load_scenarios(Path(__file__).resolve().parents[3] / "scenarios_rel6_draft.yaml")
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
