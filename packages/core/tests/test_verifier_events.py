"""REL-1b verifier events are audit-only event-log records."""

from __future__ import annotations

from disco.core import EventAdapter, event_from_json_dict, event_to_json_dict
from disco.core.events import (
    LLMConvertible,
    VerifierShadowEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
)
from disco.core.view import View


def test_verifier_events_roundtrip_and_are_not_llm_convertible() -> None:
    events = (
        VerifierStartedEvent(
            artifact_path="index.html",
            artifact_kind="app",
            requested_by_event_id="evt_finish",
        ),
        VerifierVerdictEvent(
            artifact_path="index.html",
            artifact_kind="app",
            verified=True,
            verdict="passed",
            detail="host verifier passed",
        ),
        VerifierShadowEvent(
            artifact_path="index.html",
            artifact_kind="app",
            inline_verdict="passed",
            host_verdict="passed",
            agreement=True,
        ),
    )

    for event in events:
        raw = event_to_json_dict(event)
        assert "schema_version" in raw
        assert "seq" in raw
        assert EventAdapter.validate_python(raw) == event
        assert event_from_json_dict(raw) == event
        assert not isinstance(event, LLMConvertible)

    assert View.of(list(events)).messages == []
