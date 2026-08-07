"""The currency predicate has ONE owner, and both consumers use THAT one.

GROUNDED FEEDBACK owner record (2026-08-06 ~21:35 CDT), constraint 2: *"One
currency predicate, two consumers. The feedback echo and the repeat-cap share a
SINGLE staleness predicate derived from the ledger's own receipt-currency rules."*

Stating that is cheap; the F47 lesson is that an agreement nobody enforces drifts
and the drift stays invisible until a canary fires on the gap. `tool_fingerprint`
said the same thing in its docstring for months and was wrong about a whole
equivalence class. So this file asserts the property mechanically, the way
`test_thrash_shapes.py` does for script identity:

* the harness RE-EXPORTS the owner's functions — object identity, not equality,
  so a copy-paste cannot silently reopen the gap;
* the shared classifier really does report every kind in the declared vocabulary;
* the two consumers agree on a run that contains every boundary kind;
* the owner's two named clauses hold as behaviour, not as prose.
"""

from __future__ import annotations

import sys
from pathlib import Path

import disco.core.receipt_currency as rc
import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


_HARNESS_ROOT = Path(__file__).resolve().parents[3] / "harness"


def _harness_oracles():
    """Import the harness oracle modules.

    The harness lives at the repo root, not on `sys.path` for a plain
    `pytest -m "not integration"` run, so a bare `importorskip` SKIPS these
    checks in the certified battery — which is the whole population that matters.
    An enforcement test that silently skips where it is supposed to enforce is
    the vacuous-check failure this campaign keeps finding, so the path is added
    explicitly and the skip is reserved for a genuinely absent harness.
    """
    if _HARNESS_ROOT.is_dir() and str(_HARNESS_ROOT) not in sys.path:
        sys.path.insert(0, str(_HARNESS_ROOT))
    pytest.importorskip("build_soak.oracles.thrash")
    from build_soak.oracles import _thrash_checks, _thrash_recovery, thrash

    return thrash, _thrash_recovery, _thrash_checks


# --- single ownership ------------------------------------------------------


def test_the_harness_re_exports_the_owner_rather_than_copying_it():
    """Object IDENTITY, not equality. Equality would pass for a duplicate."""
    thrash, recovery, _checks = _harness_oracles()
    assert recovery.trusted_mutation_receipt_outcome is rc.trusted_mutation_receipt_outcome
    assert recovery.approved_plan_predicate_scope is rc.approved_plan_predicate_scope
    assert thrash.preview_generation_of is rc.preview_generation_of


def test_the_currency_rules_live_in_the_shared_lowest_layer():
    """Importable by the loop: `disco.core`, stdlib-only, no harness import."""
    for fn in (
        rc.trusted_mutation_receipt_outcome,
        rc.approved_plan_predicate_scope,
        rc.preview_generation_of,
        rc.workspace_mutation_ends_currency,
    ):
        assert fn.__module__ == "disco.core.receipt_currency"


def test_the_loop_echo_computes_its_boundary_through_the_shared_owner():
    """The echo must not keep a private staleness rule."""
    from disco.core.loop import dedup

    assert dedup.latest_currency_boundary_seq is rc.latest_currency_boundary_seq


# --- the vocabulary is real ------------------------------------------------


def _event(kind: str, seq: int, **fields: object) -> dict[str, object]:
    return {"kind": kind, "seq": seq, **fields}


def _user_message(seq: int) -> dict[str, object]:
    return _event("message", seq, source="user")


def _blocking_environment(seq: int) -> dict[str, object]:
    return _event("message", seq, source="environment", meta={"blocking": "ask_user"})


def _plan_approved(seq: int, revision: int = 1) -> dict[str, object]:
    return _event(
        "status",
        seq,
        source="system",
        detail="plan_approved",
        plan_verification_transition={
            "new_authority": "plan",
            "reason": "approved_initial_plan",
            "new_plan_revision": revision,
            "new_plan_event_id": f"plan{revision}",
            "new_predicate_fingerprints": [f"sha256:p{revision}"],
        },
    )


def _trusted_receipt(seq: int, action_id: str = "a1") -> dict[str, object]:
    return _event(
        "observation",
        seq,
        action_id=action_id,
        tool_result={
            "success": True,
            "tool_name": "file_write",
            "structured": {"path": "src/app.py", "sha256": "a" * 64},
        },
    )


def _preview_receipt(seq: int, generation: str) -> dict[str, object]:
    return _event(
        "observation",
        seq,
        action_id=f"p{seq}",
        tool_result={
            "success": True,
            "tool_name": "preview_start",
            "structured": {"generation": generation, "projection_id": generation},
        },
    )


def _mutation_action(seq: int, action_id: str = "m1") -> dict[str, object]:
    return _event(
        "action",
        seq,
        id=action_id,
        tool_call={"tool_name": "file_write", "arguments": {"path": "src/App.jsx"}},
    )


def test_every_declared_boundary_kind_is_actually_reachable():
    """A vocabulary entry no event can produce is a lie in a frozenset."""
    gen_a, gen_b = "pv_" + "1" * 32, "pv_" + "2" * 32
    boundaries = rc.CurrencyBoundaries()
    seen: set[str] = set()
    stream = [
        _user_message(1),
        _blocking_environment(2),
        _plan_approved(3),
        _trusted_receipt(4),
        _preview_receipt(5, gen_a),
        _preview_receipt(6, gen_b),
        _mutation_action(7),
    ]
    for event in stream:
        kind = boundaries.crossing(event, failed_action_ids=frozenset())
        if kind is not None:
            seen.add(kind)
    assert seen == rc.CURRENCY_BOUNDARY_KINDS
    assert boundaries.kinds == rc.CURRENCY_BOUNDARY_KINDS


def test_a_mutation_PROVEN_to_have_failed_is_not_a_boundary():
    """A write that FAILED changed nothing — it must not end currency.

    The oracle held this rule and the loop did not; the unification adopts it for
    both. Without it a failed write silences the echo, taking the agent's answer
    away and giving nothing back.
    """
    boundaries = rc.CurrencyBoundaries()
    assert boundaries.crossing(_mutation_action(1), failed_action_ids=frozenset({"m1"})) is None


def test_a_mutation_of_UNKNOWN_outcome_is_a_boundary():
    """Unknown is not the same as failed, and the difference is a false assurance.

    An action with no observation yet has not been shown to be a no-op. Reading
    it as one would let the echo tell the agent nothing had changed when
    something might have — the single failure mode W-39's contract rules out. So
    currency fails to the safe side and the memo goes quiet.
    """
    boundaries = rc.CurrencyBoundaries()
    # no recorded failures at all
    assert (
        boundaries.crossing(_mutation_action(1), failed_action_ids=frozenset())
        == rc.BOUNDARY_WORKSPACE_MUTATION
    )
    # outcome map unavailable entirely
    assert (
        rc.CurrencyBoundaries().crossing(_mutation_action(1), failed_action_ids=None)
        == rc.BOUNDARY_WORKSPACE_MUTATION
    )


# --- the owner's two clauses, as behaviour ---------------------------------


def test_the_echo_fires_only_while_the_prior_result_is_still_current():
    """Clause one. Every boundary kind must make a prior result stale."""
    for label, event in (
        ("user", _user_message(5)),
        ("blocking", _blocking_environment(5)),
        ("plan", _plan_approved(5)),
        ("receipt", _trusted_receipt(5)),
        ("mutation", _mutation_action(5)),
    ):
        assert not rc.prior_result_is_current(
            [event], prior_seq=3, failed_action_ids=frozenset()
        ), label
    # …and with nothing intervening, the answer stays current.
    assert rc.prior_result_is_current(
        [_event("action", 5, id="x", tool_call={"tool_name": "shell", "arguments": {}})],
        prior_seq=3,
    )


def test_the_cap_does_not_count_across_a_trusted_mutation_receipt():
    """Clause two, proven rather than assumed.

    *"The cap must not count a re-verification made legitimate by intervening
    workspace change."* The exact-repeat streak must break at the receipt.
    """
    thrash, _recovery, _checks = _harness_oracles()
    call = {"tool_name": "shell", "arguments": {"command": "pytest -q"}}
    events = [
        _event("action", 1, id="r1", tool_call=call),
        _event("action", 2, id="r2", tool_call=call),
        _trusted_receipt(3, action_id="w1"),
        _event("action", 4, id="r3", tool_call=call),
        _event("action", 5, id="r4", tool_call=call),
    ]
    streak, _fp, _seqs = thrash._longest_identical_streak(
        events, action_ids=frozenset({"w1"}), failed_action_ids=frozenset()
    )
    assert streak == 2, "the receipt must break the streak, not be counted through"


def test_the_two_consumers_agree_on_the_same_run():
    """The property the whole module exists for.

    Whatever ends the echo's currency must also break the cap's streak. A run
    where one moves and the other does not is the F47 gap.
    """
    thrash, _recovery, _checks = _harness_oracles()
    call = {"tool_name": "shell", "arguments": {"command": "pytest -q"}}
    for label, boundary in (
        ("user", _user_message(3)),
        ("blocking", _blocking_environment(3)),
        ("plan", _plan_approved(3)),
        ("receipt", _trusted_receipt(3, action_id="w1")),
        ("mutation", _mutation_action(3, action_id="m1")),
    ):
        events = [
            _event("action", 1, id="r1", tool_call=call),
            _event("action", 2, id="r2", tool_call=call),
            boundary,
            _event("action", 4, id="r3", tool_call=call),
            _event("action", 5, id="r4", tool_call=call),
        ]
        failed: frozenset[str] = frozenset()
        echo_stale = not rc.prior_result_is_current(
            events, prior_seq=2, failed_action_ids=failed
        )
        streak, _fp, _seqs = thrash._longest_identical_streak(
            events, action_ids=frozenset({"w1"}), failed_action_ids=failed
        )
        cap_broke = streak == 2
        assert echo_stale == cap_broke is True, (
            f"{label}: echo_stale={echo_stale} cap_broke={cap_broke} — "
            "the two consumers disagree, which is the F47 gap"
        )
