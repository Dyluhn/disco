"""Moved declared stack restart the outage collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    _disco_mod,
)
from .helpers_01 import _inspect_snapshot
from .helpers_02 import _restart_span


def _impl_test_declared_stack_restart_does_not_score_its_own_outage_as_evidence_loss():
    """A restart_after_terminal scenario kills the agent-server on purpose.

    The poller then cannot reach it, and before the trace was durable that
    unreachability was indistinguishable from evidence loss. It is not: the
    stream continues monotonically across the restart, so the declared window
    must not taint continuity — while still being counted, so it stays visible.
    """
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1)]))

    aggregation.note_expected_stack_restart()
    aggregation.note_unavailable()
    aggregation.note_trace_disappeared()

    assert aggregation.reasons == set(), "the declared outage is not evidence loss"
    assert aggregation.unavailable_sample_count == 2, "but it stays counted/auditable"
    assert aggregation.declared_restart_count == 1

    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1), _restart_span(2)]))
    aggregation.finish()
    assert aggregation.render()["aggregation"]["lossless"] is True


def _impl_test_declared_restart_license_is_consumed_by_the_first_sample_back():
    """Narrowness is the whole point: once the trace answers again, a later
    outage is real and must taint continuity."""
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1)]))
    aggregation.note_expected_stack_restart()
    aggregation.note_unavailable()
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1), _restart_span(2)]))

    aggregation.note_unavailable()  # a SECOND outage, undeclared

    assert "inspect_unavailable" in aggregation.reasons
    aggregation.finish()
    assert aggregation.render()["aggregation"]["lossless"] is False


def _impl_test_declared_restart_never_excuses_a_content_conflict():
    """The license covers unreachability only. If trace durability regresses and
    the post-restart stream renumbers over the retained prefix, that is still a
    conflict and still fails the run."""
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1, "req_a")]))
    aggregation.note_expected_stack_restart()

    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1, "req_b")]))

    assert "event_content_conflict" in aggregation.reasons
    aggregation.finish()
    assert aggregation.render()["aggregation"]["lossless"] is False


def _impl_test_undeclared_outage_still_taints_continuity():
    """Regression guard: without a declaration nothing changes."""
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1)]))
    aggregation.note_unavailable()
    assert "inspect_unavailable" in aggregation.reasons


def _impl_test_declared_restart_license_survives_samples_taken_before_the_server_dies():
    """The drive arms the license BEFORE issuing the restart, and the old server
    takes seconds to die — so the poller keeps taking accepted samples in
    between. Consuming the license on any accepted sample closed the window
    before the outage even began, and the real 13-sample outage still tainted
    continuity (observed live at seed 406426)."""
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1)]))

    aggregation.note_expected_stack_restart()
    # ... the server is still up; several accepted samples land.
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1), _restart_span(2)]))
    aggregation.add_snapshot(
        _inspect_snapshot([_restart_span(1), _restart_span(2), _restart_span(3)])
    )
    assert aggregation.restart_pending is True, "license must still be armed"

    # ... now the server actually dies.
    for _ in range(13):
        aggregation.note_unavailable()
    assert aggregation.reasons == set(), "the declared outage is still exempt"

    # ... and comes back, closing the window.
    aggregation.add_snapshot(
        _inspect_snapshot([_restart_span(1), _restart_span(2), _restart_span(3), _restart_span(4)])
    )
    assert aggregation.restart_pending is False
    aggregation.note_unavailable()
    assert "inspect_unavailable" in aggregation.reasons, "a later outage is real"


def _impl_test_silent_unavailable_samples_still_fail_without_a_declared_restart():
    """The counter<->reason coherence rule is what stops a forged aggregate from
    wearing green around disclosed evidence loss. The declared-restart exemption
    must widen it by exactly one case and no more."""
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1)]))
    aggregation.finish()
    rendered = aggregation.render()

    # Hand-forge the shape the exemption could otherwise hide: unavailable
    # samples, no taint, no declared restart. Sample accounting is kept honest so
    # the ONLY thing under test is the counter<->reason rule.
    rendered["aggregation"]["unavailable_sample_count"] = 3
    rendered["aggregation"]["sample_count"] += 3
    assert "unavailable_reason_incoherent" in _disco_mod.inspect_aggregate_violations(
        rendered, conversation_id=_CID
    )

    # The same shape WITH a declared restart is the legitimate window.
    rendered["aggregation"]["declared_restart_count"] = 1
    assert _disco_mod.inspect_aggregate_violations(rendered, conversation_id=_CID) == []


def _impl_test_declared_restart_aggregate_is_internally_consistent_end_to_end():
    """render() -> inspect_aggregate_violations() must agree across the whole
    declared-restart lifecycle; producer/gate drift here is what invalidated
    seeds 406427/406428 with 'internally consistent inspect aggregate'."""
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1)]))
    aggregation.note_expected_stack_restart()
    for _ in range(13):
        aggregation.note_unavailable()
    aggregation.add_snapshot(_inspect_snapshot([_restart_span(1), _restart_span(2)]))
    aggregation.finish()

    rendered = aggregation.render()
    assert rendered["aggregation"]["lossless"] is True
    assert rendered["aggregation"]["declared_restart_count"] == 1
    assert rendered["aggregation"]["unavailable_sample_count"] == 13
    assert _disco_mod.inspect_aggregate_violations(rendered, conversation_id=_CID) == []
