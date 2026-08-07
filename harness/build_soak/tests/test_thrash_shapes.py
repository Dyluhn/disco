"""Thrash shape classification — regression fixtures and no-weakening proofs (A11 §2/§4).

# The defect these tests lock down

`ThrashOracle` reported FOUR structurally different failures under one code,
`TOOL_CALL_THRASH`, separable only by reading the free-text `first_broken_link`.
That is not a hypothetical hazard: the campaign's own design record,
`A6-REGISTER-5-INVARIANT-DESIGN-2026-08-02.md` §1, states

    "`first_broken_link` names it exactly: `tool_call -> identical_tool_call_streak`"

over a table of all three of register #5's historical firings. Measured from the
frozen `classification.json` files, that holds for **two of the three**: 10-C
(`6606503c`, seqs 148/155/176) is `repeated_semantic_shell_verification`. A
classification a careful reader gets wrong is not a classification, and the
fixtures below are the standing refutation.

# What is proved here

* `test_historical_*` — the three real firings still classify, with the shape
  each ACTUALLY had, on distilled copies of their own event logs.
* `test_every_shape_code_has_the_same_severity_as_the_parent` — the split moved
  no severity. A11 §4 forbids weakening anything, and a severity that drifted in
  either direction would be a weakening dressed as a refactor.
* `test_live_thrash_stop_boundary_recognises_every_shape` — the live-monitor
  stop boundary still recognises every shape. This is the control that would
  have silently narrowed: it gates on a set of codes, and a shape missing from
  that set is a thrash the monitor stops catching, with nothing going red.
* `test_thresholds_are_unchanged` — the oracle's defaults are still 2, pinned
  against the amendment's own text.
* `test_loop_and_oracle_share_one_identity_function` — A6.3 §3.1/§6.1. The
  freshness memo and the oracle must agree on "is this the same call?", or the
  memo fires on an equivalence class the oracle never grades.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from disco.core import script_identity
from disco.core.tool_fingerprint import tool_call_fingerprint

from harness.build_soak import failure_codes as fc
from harness.build_soak.oracles import _thrash_shapes as shapes
from harness.build_soak.oracles import _thrash_shell
from harness.build_soak.oracles._thrash_checks import _fingerprint
from harness.build_soak.oracles._thrash_shell import largest_semantic_shell_repeat_group
from harness.build_soak.oracles.thrash import ThrashOracle

FIXTURES = Path(__file__).parent / "fixtures" / "thrash_shapes"

# (fixture stem, epic, the shape it ACTUALLY had — not the one A6.3 §1 asserted)
HISTORICAL = [
    ("10c_6606503c", "10-C", shapes.SHAPE_SEMANTIC_SHELL),
    ("10d_65a9f46a", "10-D", shapes.SHAPE_IDENTICAL_STREAK),
    ("11a_488741db", "11-A", shapes.SHAPE_IDENTICAL_STREAK),
]


def _load(stem: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{stem}.json").read_text())


def _check(fixture: dict[str, Any]):
    return ThrashOracle().check(
        fixture["events"],
        scenario=fixture["scenario"],
        inspect_trace=fixture["inspect_trace"],
    )[0]


# ---------------------------------------------------------------------------
# The three historical firings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("stem", "epic", "expected_shape"), HISTORICAL)
def test_historical_firing_classifies_with_its_actual_shape(stem, epic, expected_shape):
    fixture = _load(stem)
    result = _check(fixture)
    expected = fixture["expected"]

    assert result.failed, f"{epic} must still be a red"
    assert result.shape == expected_shape
    assert result.code == shapes.code_for(expected_shape)
    assert result.first_broken_link == shapes.link_for(expected_shape)
    # The recorded fingerprint and the seqs of every occurrence, unchanged from
    # the frozen artifact.
    assert result.facts["fingerprint"] == expected["fingerprint"]
    assert result.facts["action_seqs"] == expected["action_seqs"]
    assert result.facts["count"] == expected["count"]
    assert result.facts["allowed"] == 2


@pytest.mark.parametrize(("stem", "epic", "expected_shape"), HISTORICAL)
def test_historical_firing_carries_the_ignored_prior_result(stem, epic, expected_shape):
    """A11 §3 — the red carries the answer the model already had.

    Register #5's invariant is stated in prose as *"the model authors a
    verification command, runs it, GETS A USABLE ANSWER, and then re-issues"*.
    Before this boundary that clause could only be confirmed by reading the
    transcript. It is now a field, and in all three firings the ignored prior
    result is a SUCCESS — the invariant, proved from the artifact.
    """
    result = _check(_load(stem))

    occurrences = result.facts["occurrences"]
    assert [o["seq"] for o in occurrences] == result.facts["action_seqs"]
    assert all(o["action_id"] for o in occurrences), "each occurrence is addressable in the log"

    prior = result.facts["ignored_prior_result"]
    assert prior is not None, f"{epic}: the repetition ignored something — say what"
    assert prior["action_seq"] == result.facts["action_seqs"][0]
    assert prior["success"] is True, "the answer the model already had was a usable one"


def test_the_three_firings_are_not_all_one_shape():
    """The standing refutation of A6.3 §1's "all three" attribution."""
    found = {shape for _, _, shape in HISTORICAL}
    assert found == {shapes.SHAPE_SEMANTIC_SHELL, shapes.SHAPE_IDENTICAL_STREAK}
    results = {epic: _check(_load(stem)).shape for stem, epic, _ in HISTORICAL}
    assert results == {
        "10-C": shapes.SHAPE_SEMANTIC_SHELL,
        "10-D": shapes.SHAPE_IDENTICAL_STREAK,
        "11-A": shapes.SHAPE_IDENTICAL_STREAK,
    }


# ---------------------------------------------------------------------------
# Nothing weakened
# ---------------------------------------------------------------------------


def test_every_shape_code_has_the_same_severity_as_the_parent():
    assert fc.severity_for(fc.TOOL_CALL_THRASH) == fc.P1
    for shape in shapes.SHAPES:
        code = shapes.code_for(shape)
        assert fc.severity_for(code) == fc.P1, f"{shape} moved severity"
        assert fc.is_known_code(code), f"{shape} emits a code the taxonomy does not know"


def test_historical_parent_code_stays_known():
    """Frozen artifacts carry `TOOL_CALL_THRASH`. Retiring the constant would
    make every one of them read as an unclassified failure (severity P0 by
    `severity_for`'s unknown-code rule), rewriting history by omission."""
    assert fc.is_known_code(fc.TOOL_CALL_THRASH)
    assert fc.severity_for(fc.TOOL_CALL_THRASH) == fc.P1


def test_live_thrash_stop_boundary_recognises_every_shape():
    """The control that would otherwise have narrowed silently.

    `_strict_live_thrash_stop_boundary` accepts an ambiguous `IDLE(killed)`
    terminal only when the retained monitor record proves a strict ThrashOracle
    failure — and it decides that by CODE MEMBERSHIP. A shape absent from that
    set is a thrash the monitor no longer recognises, with no test going red.
    """
    from harness.build_soak._runner.thrash import STRICT_LIVE_THRASH_CODES

    # Asserted against the set the boundary ACTUALLY uses, not a copy of it —
    # a test that keeps its own list drifts with the bug instead of catching it.
    for shape in shapes.SHAPES:
        assert shapes.code_for(shape) in STRICT_LIVE_THRASH_CODES
    assert fc.TOOL_CALL_THRASH in STRICT_LIVE_THRASH_CODES
    # The non-repetition thrash codes were already recognised and must stay so.
    assert fc.TOOL_ERROR_THRASH in STRICT_LIVE_THRASH_CODES
    assert fc.ACTIONLESS_THRASH in STRICT_LIVE_THRASH_CODES
    assert fc.MODEL_REPAIR_THRASH in STRICT_LIVE_THRASH_CODES


def test_thresholds_are_unchanged():
    """A11 §4: "the threshold stays at 2; no run is exempted"."""
    from harness.build_soak.oracles.thrash import _limits

    limits = _limits({"assertions": {"thrash": {}}})
    assert limits is not None
    assert limits["max_identical_action_repeats"] == 2
    assert limits["max_same_tool_error_repeats"] == 2
    assert limits["max_actionless_pauses"] == 0
    for _stem, _epic, _shape in HISTORICAL:
        assert _load(_stem)["expected"]["allowed"] == 2


def test_shape_link_and_code_tables_cannot_drift():
    """Every shape has exactly one link and one code, and the tables agree on
    membership — the property that makes a misclassifying red unconstructible."""
    assert set(shapes.LINK_BY_SHAPE) == set(shapes.CODE_BY_SHAPE) == set(shapes.SHAPES)
    assert len(set(shapes.LINK_BY_SHAPE.values())) == len(shapes.SHAPES)
    assert len(set(shapes.CODE_BY_SHAPE.values())) == len(shapes.SHAPES)
    for shape in shapes.SHAPES:
        assert shapes.link_for(shape) == shapes.LINK_BY_SHAPE[shape]
        assert shapes.code_for(shape) == shapes.CODE_BY_SHAPE[shape]
    with pytest.raises(ValueError):
        shapes.link_for("not_a_shape")
    with pytest.raises(ValueError):
        shapes.code_for("not_a_shape")


# ---------------------------------------------------------------------------
# A6.3 §3.1/§6.1 — one identity function, shared with the loop
# ---------------------------------------------------------------------------


def test_loop_and_oracle_share_one_identity_function():
    """The oracle's `_fingerprint` and the loop's memo key must be the SAME
    function, not two implementations that agree today.

    The corpus includes the cases where a private `arguments ==` comparison —
    what the memo used before this boundary — disagrees with the canonical
    rendering: `1` vs `1.0` compare equal in Python but render differently.
    """
    corpus = [
        ("shell", {"command": "ls -la"}),
        ("shell", {"command": "node render-check.mjs 2>&1"}),
        ("shell", {"b": 2, "a": 1}),
        ("shell", {"a": 1, "b": 2}),
        ("file_read", {}),
        ("file_read", {"path": "src/App.jsx"}),
        ("shell", {"n": 1}),
        ("shell", {"n": 1.0}),
        (None, {"x": None}),
    ]
    for tool, args in corpus:
        event = {"kind": "action", "tool_call": {"tool_name": tool, "arguments": args}}
        assert _fingerprint(event) == tool_call_fingerprint(tool, args)

    # Key ORDER is irrelevant; VALUE TYPE is not.
    assert tool_call_fingerprint("shell", {"a": 1, "b": 2}) == tool_call_fingerprint(
        "shell", {"b": 2, "a": 1}
    )
    assert tool_call_fingerprint("shell", {"n": 1}) != tool_call_fingerprint("shell", {"n": 1.0})


# ---------------------------------------------------------------------------
# F47 (2026-08-06x) — the general invariant, and the coverage duty that makes it
# non-vacuous. The test above proved ONE class shared; these prove ALL of them.
# ---------------------------------------------------------------------------

# Every shape on which this oracle counts repetitions of SUCCESSFUL calls, with
# the `disco.core` owner that computes its equivalence class. The loop's W-39
# freshness memo imports the SAME owners, so it cannot group differently.
#
# `SHAPE_STUCK_VALVE` is deliberately absent: it is not a repetition class at all
# (it reports the product's own no-progress valve), so it carries no notice duty.
ANSWERED_QUESTION_IDENTITY_OWNERS = {
    shapes.SHAPE_IDENTICAL_STREAK: tool_call_fingerprint,
    shapes.SHAPE_SEMANTIC_SHELL: script_identity.direct_script_invocations,
    shapes.SHAPE_BACKGROUND_RESTART: script_identity.direct_script_invocations,
}


def test_every_repetition_shape_declares_a_shared_identity_owner():
    """A new repetition shape cannot be added without declaring its owner.

    This is the enforcement clause of the F47 invariant — *answered-question
    repetition is culpable only after notice* — and it exists because the
    previous, narrower version of this guarantee was FALSE while reading as
    true. `tool_fingerprint.py` and `_thrash_checks._fingerprint` both state
    that the memo "can never fire on a different equivalence class than the
    oracle that grades the run"; that held for the identical-call class only,
    and `diag_script_run` @97903 was faulted on a class the memo had never seen.

    So the table above is exhaustive over the oracle's repetition shapes, and
    this asserts it. Adding a fifth shape without an owner fails here rather
    than silently re-opening the gap.
    """
    repetition_shapes = set(shapes.SHAPES) - {shapes.SHAPE_STUCK_VALVE}
    assert repetition_shapes == set(ANSWERED_QUESTION_IDENTITY_OWNERS), (
        "a repetition shape exists with no declared identity owner — declare which "
        "disco.core function computes its class, and make the loop's memo use it"
    )


def test_identity_owners_all_live_in_the_shared_lowest_layer():
    """Owners must live in `disco.core`, importable by BOTH sides.

    An owner defined in the harness is exactly the shape of the F47 defect: the
    oracle can grade on it and the loop cannot compute it.
    """
    for shape, owner in ANSWERED_QUESTION_IDENTITY_OWNERS.items():
        assert owner.__module__.startswith("disco.core"), (
            f"{shape} resolves identity through {owner.__module__}, which the loop "
            "cannot import — the memo would be blind to this class"
        )


def test_the_harness_re_exports_the_owner_rather_than_copying_it():
    """`_thrash_shell.direct_script_invocations` must BE the core function.

    Identity, not equality: a copy that agrees today is the drift the single-owner
    rule exists to prevent, and it would stay invisible until a cell was graded on
    a class the memo never saw.
    """
    assert _thrash_shell.direct_script_invocations is script_identity.direct_script_invocations
    assert _thrash_shell.shell_segments is script_identity.shell_segments
    assert _thrash_shell.static_tee_sinks is script_identity.static_tee_sinks


def test_loop_and_oracle_group_the_97903_spellings_identically():
    """The measured 06v failure, as a cross-side agreement check.

    `diag_script_run` @97903 issued one script under three spellings. The oracle
    grouped all three and reded the run at a cap of two; the memo grouped none of
    them and said nothing. Both sides must now group all three.
    """
    commands = [
        "cd /workspace && python3 primes.py",
        "cd /workspace && python3 primes.py | wc -l && python3 primes.py | tail -1",
        "python3 primes.py | tee /tmp/primes.out | wc -l && tail -n 1 /tmp/primes.out",
    ]
    events: list[dict[str, Any]] = []
    outcomes: dict[str, tuple[bool, str, str]] = {}
    for index, command in enumerate(commands):
        action_id = f"a{index}"
        events.append(
            {
                "kind": "action",
                "seq": index * 2,
                "id": action_id,
                "action_id": action_id,
                "tool_call": {"tool_name": "shell", "arguments": {"command": command}},
            }
        )
        outcomes[action_id] = (True, "", "")

    count, fingerprint, seqs = largest_semantic_shell_repeat_group(events, outcomes)

    # The oracle's side: three occurrences, one identity — the cell's own value.
    assert (count, fingerprint) == (3, '["python","/workspace/primes.py",[]]')
    assert seqs == [0, 2, 4]
    # The loop's side: the same identity, from the same owner, for every spelling.
    for command in commands:
        assert script_identity.foreground_script_fingerprints(command) == {fingerprint}
