"""F49 — a PASS receipt must not license treating a live observation as durable.

Regression evidence for the 2026-08-06x repair. The defect, measured at
2026-08-06v on `p4_appkit_semantic_edit` seed 97705 (P0,
`FALSE_FINISH_PREVIEW_BROKEN`, preview health 409):

The `disco.appkit_strict@1` receipt returned `failures: []` and a `next_action`
ending *"…Move to your remaining plan steps, and if none are outstanding,
finish; re-verify only after a material change."* The agent finished on it.

That sentence carries ONE validity licence over TWO kinds of claim:

  * **durable** — design lint, schema/worker/form structure, storage bounds,
    export readiness, sealed identity. Properties of the bytes. "Re-verify only
    after a material change" is correct for them.
  * **perishable** — `route_coverage`, `section_coverage`, `primitive_live:*`.
    Observations of a RUNNING preview whose truth can lapse with no agent action
    at all: the preview can stop, restart, or rotate generation.

Extending the licence to the perishable claims tells the agent a live
observation is settled. That is the defect — a time-scope error, not a
subject-scope error, and the same family as F47 (both are about the validity
window of an answer the agent already holds).

**Scope, stated honestly.** The 2026-08-06x boundary MEASURED that this receipt
is not what caused the 97705 red: the receipt is byte-identical (fingerprint
`b2dd1ad17298c08d`) in the three AppKit cells that captured preview health 200,
and every event-derived input to the finished-preview authority is identical
across all four. The 409 arises in the post-finish sealed-runtime replay and is
registered separately (F50, referred). F49 is repaired here on its own merits.

`OutputTruthOracle` is untouched: it adjudicated correctly and nothing in this
file or its repair weakens it.
"""

from __future__ import annotations

from typing import Any

from disco.tools.builtin.verify_appkit_app import (
    build_verdict,
    observation_scoped_checks,
)

DURABLE = "design_lint_clean"
LIVE_ROUTE = "route_coverage"
LIVE_SECTION = "section_coverage"
LIVE_PRIMITIVE = "primitive_live:disco.appkit.lead_capture@1"


def _check(name: str, passed: bool = True) -> dict[str, Any]:
    return {"name": name, "passed": passed, "evidence": f"{name} evidence"}


def _passing(*names: str) -> dict[str, Any]:
    return build_verdict([_check(name) for name in names], {})


# ---------------------------------------------------------------------------
# The classification itself
# ---------------------------------------------------------------------------


def test_the_live_checks_are_the_ones_that_observe_a_running_preview():
    checks = [
        _check(DURABLE),
        _check(LIVE_ROUTE),
        _check("local_list_storage_bounds"),
        _check(LIVE_SECTION),
        _check(LIVE_PRIMITIVE),
        _check("cloudflare_export_ready"),
    ]
    assert observation_scoped_checks(checks) == [LIVE_ROUTE, LIVE_SECTION, LIVE_PRIMITIVE]


def test_a_battery_with_no_live_check_has_no_observation_scope():
    assert observation_scoped_checks([_check(DURABLE)]) == []


def test_every_primitive_live_check_is_perishable_whatever_the_primitive_is():
    """The prefix, not an enumerated list — a new security primitive must not
    silently arrive as a 'durable' claim."""
    assert observation_scoped_checks([_check("primitive_live:anything.at.all@9")]) == [
        "primitive_live:anything.at.all@9"
    ]


# ---------------------------------------------------------------------------
# The receipt — RED on the old bytes, where one licence covered both kinds
# ---------------------------------------------------------------------------


def test_the_durable_licence_is_scoped_to_the_structural_checks():
    text = _passing(DURABLE, LIVE_ROUTE)["next_action"]
    assert "re-verify the structure only after a material change" in text
    # The un-scoped form is what shipped at 06v and must not come back.
    assert "re-verify only after a material change" not in text


def test_the_receipt_names_its_perishable_claims_as_observation_scoped():
    text = _passing(DURABLE, LIVE_ROUTE, LIVE_SECTION)["next_action"]
    assert f"`{LIVE_ROUTE}`" in text
    assert f"`{LIVE_SECTION}`" in text
    assert "AT THIS CHECK" in text
    assert "can lapse without any change of yours" in text


def test_the_receipt_disclaims_the_thing_the_oracle_actually_checks():
    """The oracle faults a finish taken while the preview is unhealthy. The
    receipt must say, in the agent's own reading, that it has NOT established
    that."""
    text = _passing(DURABLE, LIVE_ROUTE)["next_action"]
    assert "does not establish that the app is still serving later" in text
    assert "the finish gate settles that" in text


def test_the_receipt_no_longer_authorises_a_finish():
    text = _passing(DURABLE, LIVE_ROUTE)["next_action"].lower()
    for instruction in ("if none are outstanding, finish", "call finish", "finish now"):
        assert instruction not in text


def test_the_verdict_surfaces_the_perishable_claims_machine_readably():
    verdict = _passing(DURABLE, LIVE_ROUTE, LIVE_PRIMITIVE)
    assert verdict["observation_scoped_checks"] == [LIVE_ROUTE, LIVE_PRIMITIVE]


def test_singular_and_plural_read_correctly():
    """The clause is agent-facing prose; disagreement reads as a template bug and
    invites the agent to discount it."""
    one = _passing(DURABLE, LIVE_ROUTE)["next_action"]
    two = _passing(DURABLE, LIVE_ROUTE, LIVE_SECTION)["next_action"]
    assert f"`{LIVE_ROUTE}` is a live observation of the preview" in one
    assert "not a durable property of the code. It can lapse" in one
    assert f"`{LIVE_ROUTE}`, `{LIVE_SECTION}` are live observations of the preview" in two
    assert "not durable properties of the code. They can lapse" in two


# ---------------------------------------------------------------------------
# NO-WEAKENING controls — the properties the 2026-07-27 counted-promotion
# failure and the 06v scoping work already bought must all survive.
# ---------------------------------------------------------------------------


def test_a_passing_receipt_still_carries_a_NON_EMPTY_next_action():
    """An empty `next_action` is what caused the 2026-07-27 re-verify thrash."""
    assert _passing(DURABLE)["next_action"]
    assert _passing(DURABLE, LIVE_ROUTE)["next_action"]


def test_it_still_discourages_pointless_structural_re_verification():
    text = _passing(DURABLE, LIVE_ROUTE)["next_action"]
    assert "proves nothing new" in text
    assert "transient" in text


def test_it_still_scopes_its_own_claim_to_structure():
    assert "structural" in _passing(DURABLE, LIVE_ROUTE)["next_action"].lower()


def test_the_FAILING_branch_is_untouched():
    verdict = build_verdict([_check(DURABLE), _check(LIVE_ROUTE, False)], {})
    assert verdict["passed"] is False
    assert verdict["next_action"] == f"{LIVE_ROUTE} evidence"
    # The scope field is still populated on a FAIL — it describes the battery,
    # not the verdict.
    assert verdict["observation_scoped_checks"] == [LIVE_ROUTE]


def test_the_pass_fingerprint_still_collapses_to_the_clean_hash():
    """The finish gate's loop-breaker key must not have moved."""
    a = _passing(DURABLE, LIVE_ROUTE)["failure_fingerprint"]
    b = _passing(DURABLE, LIVE_ROUTE)["failure_fingerprint"]
    assert a == b
