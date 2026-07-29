"""Inspect-aggregation trace fixtures shared by BF2A cases."""

from __future__ import annotations

from ._shared import _CID, Any, _disco_mod
from .helpers_01 import _inspect_snapshot


def _honest_gate_trace() -> dict[str, Any]:
    aggregation = _disco_mod._InspectTraceAggregation(_CID)
    aggregation.add_snapshot(
        _inspect_snapshot(
            [
                {"seq": 1, "kind": "routing", "role": "agent_driver", "chosen_model": "m"},
                {"seq": 2, "kind": "span", "span": "agent.step", "event": "end"},
                {"seq": 3, "kind": "tool_scope", "mode": "execution"},
            ]
        )
    )
    aggregation.finish()
    return aggregation.render()


def _forged_lossless_trace() -> dict[str, Any]:
    """Zero canonical events with projections asserted from thin air, wearing
    caller-owned lossless/finalized/dropped fields that all assert green."""

    return {
        "conversation_id": _CID,
        "event_count": 0,
        "dropped_event_count": 0,
        "source_dropped_event_count": 0,
        "events": [],
        "routing_decisions": [{"role": "agent_driver", "chosen_model": "forged"}],
        "spans": [{"span": "agent.step", "event": "end", "request_id": "forged"}],
        "tool_scopes": [],
        "progress_shadows": [],
        "aggregation": {
            "schema_version": 1,
            "sample_count": 1,
            "accepted_sample_count": 1,
            "event_bearing_sample_count": 0,
            "pretrace_unavailable_count": 0,
            "unavailable_sample_count": 0,
            "malformed_sample_count": 0,
            "overlap_sample_count": 0,
            "source_max_dropped_count": 0,
            "unique_event_count": 0,
            "first_retained_seq": None,
            "last_retained_seq": None,
            "continuity": "complete",
            "continuity_reason": "no_source_eviction_observed",
            "conflict_count": 0,
            "failure_reasons": [],
            "lossless": True,
            "finalized": True,
        },
    }
